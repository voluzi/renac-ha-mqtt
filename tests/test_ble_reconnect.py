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
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert inverter.disconnect.await_count == 2

    asyncio.run(scenario())


def test_wallbox_ble_reconnect_keeps_one_client_while_broker_is_down(
    bridge_devices, mqtt_clients, monkeypatch,
):
    factory, clients = mqtt_clients
    create_client = factory.side_effect
    connection_failures = []

    def broker_down(*args, **kwargs):
        client = create_client(*args, **kwargs)

        def fail_connection():
            client.on_connect(client, None, {}, 135, None)
            client.on_disconnect(client, None, {}, 1, None)
            connection_failures.append(135)

        client.loop_start.side_effect = fail_connection
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
            assert connection_failures == [135]
            clients[0].connect.assert_not_called()
            clients[0].connect_async.assert_called_once()
            clients[0].loop_start.assert_called_once()
            dev = bridge.wallbox_mqtt_by_addr["ble-test"]
            assert not dev._connected
            dev.on_connect(dev.client, None, {}, 0, None)
            assert dev._connected
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
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert wallbox.disconnect.await_count == 2

    asyncio.run(scenario())


def test_wallbox_notifications_after_shutdown_are_dropped(bridge_devices, mqtt_clients, monkeypatch):
    monkeypatch.setattr(bridge, "shutdown_event", asyncio.Event())
    callback = bridge.make_wallbox_callback("ble-test")
    callback({"sn": "test", "model": "test", "state": "idle", "power": 100})
    dev = bridge.wallbox_mqtt_by_addr["ble-test"]
    original_state = dev.state.copy()
    dev.disconnect()
    bridge.shutdown_event.set()
    dev.client.publish.reset_mock()

    callback({"sn": "test", "model": "test", "state": "charging", "power": 200})

    dev.client.publish.assert_not_called()
    assert dev.state == original_state


@pytest.mark.parametrize("overview", [{"load_power": 123, "battery_soc": 80}, None])
def test_partial_overview_publishes_only_present_keys(
    bridge_devices, mqtt_clients, monkeypatch, overview,
):
    async def scenario():
        monkeypatch.setattr(bridge, "shutdown_event", asyncio.Event())
        monkeypatch.setattr(bridge, "POLL_INTERVAL_S", 0)
        inverter = MagicMock(spec=bridge.RenacInverterBLE)
        for name in dir(bridge.RenacInverterBLE):
            if name.startswith("get_"):
                setattr(inverter, name, AsyncMock(return_value=None))
        inverter.get_info.return_value = {"sn": "test", "model": "test"}
        polled = asyncio.Event()
        hold = asyncio.Event()
        calls = 0

        async def poll():
            nonlocal calls
            calls += 1
            if calls > 1:
                polled.set()
                await hold.wait()
            return overview

        inverter.get_power_and_energy_overview.side_effect = poll
        monkeypatch.setattr(bridge, "RenacInverterBLE", lambda addr: inverter)
        task = asyncio.create_task(bridge.run_inverter_task("ble-test"))
        try:
            await asyncio.wait_for(polled.wait(), 2)
            dev = bridge.inverter_mqtt_by_addr["ble-test"]
            assert dev.state == (overview or {})
            sensor_values = {
                c.args[0].split("/")[-2]: c.args[1]
                for c in dev.client.publish.call_args_list
                if c.args[0].startswith("homeassistant/sensor/") and c.args[0].endswith("/state")
            }
            assert sensor_values == (overview or {})
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_missing_inverter_info_reconnects_without_creating_device(
    bridge_devices, mqtt_clients, monkeypatch, caplog,
):
    async def scenario():
        monkeypatch.setattr(bridge, "shutdown_event", asyncio.Event())
        inverter = MagicMock(spec=bridge.RenacInverterBLE)
        inverter.get_info.return_value = None
        reconnected = asyncio.Event()
        hold = asyncio.Event()
        attempts = 0

        async def connect():
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                reconnected.set()
                await hold.wait()

        inverter.connect.side_effect = connect
        monkeypatch.setattr(bridge, "RenacInverterBLE", lambda addr: inverter)
        task = asyncio.create_task(bridge.run_inverter_task("ble-test"))
        try:
            await asyncio.wait_for(reconnected.wait(), 2)
            assert not bridge.inverter_mqtt_by_addr
            mqtt_clients[0].assert_not_called()
            assert "could not read inverter info" in caplog.text
            assert not any(record.exc_info for record in caplog.records)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_enabling_period_does_not_zero_window_after_failed_read():
    async def scenario():
        get_period = AsyncMock(return_value=None)
        set_period = AsyncMock(return_value=True)
        setter = bridge._period_setter(get_period, set_period, "enabled")
        assert await setter("ON") is False
        set_period.assert_not_awaited()
    asyncio.run(scenario())
