import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from renac_ha_bridge import __main__ as bridge


def test_inverter_ble_reconnect_keeps_one_mqtt_client(
    bridge_devices, mqtt_clients, monkeypatch,
):
    async def scenario():
        monkeypatch.setattr(bridge, "shutdown_event", asyncio.Event())
        monkeypatch.setattr(bridge, "POLL_INTERVAL_S", 0)
        inverter = MagicMock(spec=bridge.RenacInverterBLE)
        for name in dir(bridge.RenacInverterBLE):
            if name.startswith("get_"):
                setattr(inverter, name, AsyncMock(return_value=None))
        inverter.get_info.return_value = {"sn": "test", "model": "test"}
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
            await asyncio.wait_for(reconnected.wait(), 7)
            factory, clients = mqtt_clients
            assert factory.call_count == 1
            clients[0].connect.assert_called_once()
            clients[0].loop_start.assert_called_once()
            dev = bridge.inverter_mqtt_by_addr["ble-test"]
            published = threading.Event()
            dev.client.publish.side_effect = lambda *a, **kw: published.set()
            msg = SimpleNamespace(
                topic="homeassistant/number/inverter_test/min_soc_on_grid/set", payload=b"100",
            )
            await asyncio.to_thread(dev.on_message, None, None, msg)
            assert await asyncio.to_thread(published.wait, 2)
            inverter.set_min_soc_on_grid.assert_awaited_once_with(100)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_wallbox_notifications_reuse_mqtt_client(bridge_devices, mqtt_clients):
    status = {"sn": "test", "model": "test", "state": "idle"}
    bridge.make_wallbox_callback("ble-test")(status)
    bridge.make_wallbox_callback("ble-test")(status)
    factory, clients = mqtt_clients
    assert factory.call_count == 1
    clients[0].connect.assert_called_once()
    clients[0].loop_start.assert_called_once()
