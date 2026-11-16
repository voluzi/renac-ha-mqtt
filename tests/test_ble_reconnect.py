import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from renac_ha_bridge import __main__ as bridge


@pytest.mark.parametrize("serial_number", [None, "test"])
def test_inverter_ble_reconnect_keeps_one_mqtt_client(
    bridge_devices, mqtt_clients, monkeypatch, serial_number,
):
    async def scenario():
        monkeypatch.setattr(bridge, "shutdown_event", asyncio.Event())
        monkeypatch.setattr(bridge, "POLL_INTERVAL_S", 0)
        inverter = MagicMock(spec=bridge.RenacInverterBLE)
        for name in dir(bridge.RenacInverterBLE):
            if name.startswith("get_"):
                setattr(inverter, name, AsyncMock(return_value=None))
        inverter.get_info.return_value = {"sn": serial_number, "model": "test"}
        inverter.set_min_soc_on_grid = AsyncMock(return_value=True)
        reconnected = asyncio.Event()
        hold = asyncio.Event()
        polls = 0

        async def overview():
            nonlocal polls
            polls += 1
            if polls == 1:
                raise ConnectionError("BLE connection lost")
            reconnected.set()
            await hold.wait()
            return {}

        inverter.get_power_and_energy_overview.side_effect = overview
        monkeypatch.setattr(bridge, "RenacInverterBLE", lambda addr: inverter)
        task = asyncio.create_task(bridge.run_inverter_task("ble-test"))
        try:
            await asyncio.wait_for(reconnected.wait(), 2)
            factory, clients = mqtt_clients
            assert factory.call_count == 1
            clients[0].connect.assert_not_called()
            clients[0].connect_async.assert_called_once()
            clients[0].loop_start.assert_called_once()
            dev = bridge.inverter_mqtt_by_addr["ble-test"]
            published = threading.Event()
            publish_info = dev.client.publish.return_value

            def publish(*args, **kwargs):
                published.set()
                return publish_info

            dev.client.publish.side_effect = publish
            msg = SimpleNamespace(
                topic=f"homeassistant/number/{dev.device_id}/min_soc_on_grid/set", payload=b"100",
            )
            await asyncio.to_thread(dev.on_message, None, None, msg)
            assert await asyncio.to_thread(published.wait, 2)
            inverter.set_min_soc_on_grid.assert_awaited_once_with(100)
            bridge.shutdown_event.set()
            hold.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert inverter.disconnect.await_count == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_wallbox_ble_reconnect_keeps_one_client_while_broker_is_down(
    bridge_devices, mqtt_clients, monkeypatch,
):
    factory, clients = mqtt_clients
    create_client = factory.side_effect

    def broker_down(*args, **kwargs):
        client = create_client(*args, **kwargs)
        client.connect.side_effect = ConnectionRefusedError("Broker offline")
        return client

    factory.side_effect = broker_down

    async def scenario():
        monkeypatch.setattr(bridge, "shutdown_event", asyncio.Event())
        monkeypatch.setattr(bridge, "POLL_INTERVAL_S", 0)
        monkeypatch.setattr(bridge, "WALLBOX_STATUS_INTERVAL_S", 0)
        wallbox = MagicMock(spec=bridge.RenacWallboxBLE)
        wallbox.get_basic_settings.return_value = None
        reconnected = asyncio.Event()
        hold = asyncio.Event()
        polls = 0

        async def status():
            nonlocal polls
            polls += 1
            if polls == 2:
                raise ConnectionError("BLE connection lost")
            if polls == 3:
                reconnected.set()
                await hold.wait()
            return {"sn": "test", "model": "test", "state": "idle"}

        wallbox.get_status.side_effect = status
        monkeypatch.setattr(bridge, "RenacWallboxBLE", lambda *a, **kw: wallbox)
        task = asyncio.create_task(bridge.run_wallbox_task("ble-test"))
        try:
            await asyncio.wait_for(reconnected.wait(), 12)
            assert wallbox.connect.await_count == 2
            assert factory.call_count == 1
            clients[0].connect.assert_not_called()
            clients[0].connect_async.assert_called_once()
            clients[0].loop_start.assert_called_once()
            dev = bridge.wallbox_mqtt_by_addr["ble-test"]
            published = threading.Event()
            publish_info = dev.client.publish.return_value

            def publish(*args, **kwargs):
                published.set()
                return publish_info

            dev.client.publish.side_effect = publish
            msg = SimpleNamespace(
                topic="homeassistant/number/wallbox_test/max_output_current/set", payload=b"16",
            )
            await asyncio.to_thread(dev.on_message, None, None, msg)
            assert await asyncio.to_thread(published.wait, 2)
            wallbox.set_max_output_current.assert_awaited_once_with(16)
            bridge.shutdown_event.set()
            hold.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert wallbox.disconnect.await_count == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
