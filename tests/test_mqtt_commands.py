import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import call

import pytest
from paho.mqtt.client import CallbackAPIVersion

from renac_ha_bridge import __main__ as bridge


def message(key="min_soc_on_grid", value=100):
    return SimpleNamespace(
        topic=f"homeassistant/number/inverter_test/{key}/set",
        payload=str(value).encode(),
    )


def test_messages_return_before_writes_finish_and_preserve_order(device):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        published = threading.Event()
        order = []

        async def setter(value):
            order.append(("start", value))
            if value == 10:
                started.set()
                await release.wait()
            order.append(("end", value))
            return True

        callback = bridge.wrap_async_callback(asyncio.get_running_loop(), setter)
        device.set_actuator_callback("min_soc", callback)
        device.set_actuator_callback("min_soc_on_grid", callback)
        device.client.publish.side_effect = lambda *a, **kw: published.set() if a[1] == 20 else None
        returned = threading.Event()

        def receive():
            device.on_message(None, None, message("min_soc", 10))
            device.on_message(None, None, message(value=20))
            returned.set()

        network_thread = threading.Thread(target=receive)
        network_thread.start()
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert await asyncio.to_thread(returned.wait, 1), "MQTT callback blocked on BLE"
            device.client.publish.assert_not_called()
        finally:
            release.set()
            await asyncio.to_thread(network_thread.join, 2)
        assert await asyncio.to_thread(published.wait, 2)
        assert order == [("start", 10), ("end", 10), ("start", 20), ("end", 20)]
        assert device.state == {"min_soc": 10, "min_soc_on_grid": 20}
        assert device.client.publish.call_args_list == [
            call("homeassistant/number/inverter_test/min_soc/state", 10, retain=True),
            call("homeassistant/number/inverter_test/min_soc_on_grid/state", 20, retain=True),
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, RuntimeError("BLE write failed")])
def test_failed_write_does_not_change_or_publish_state(device, failure, caplog):
    async def scenario():
        def setter(value):
            if isinstance(failure, Exception):
                raise failure
            return failure

        device.set_actuator_callback("min_soc_on_grid", setter, 50)
        device.client.publish.reset_mock()
        await asyncio.to_thread(device.on_message, None, None, message())
        await asyncio.to_thread(device.disconnect)
        assert device.state["min_soc_on_grid"] == 50
        device.client.publish.assert_not_called()
        assert any(record.levelname == "WARNING" for record in caplog.records)

    asyncio.run(scenario())


def test_timed_out_write_is_cancelled_before_next_command(device, monkeypatch):
    monkeypatch.setattr(bridge, "BLE_WRITE_TIMEOUT_S", 0.05)

    async def scenario():
        cancelled = asyncio.Event()
        published = threading.Event()

        async def setter(value):
            if value == 100:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            assert cancelled.is_set()
            return True

        device.set_actuator_callback(
            "min_soc_on_grid", bridge.wrap_async_callback(asyncio.get_running_loop(), setter), 50,
        )
        device.client.publish.reset_mock()
        device.client.publish.side_effect = lambda *a, **kw: published.set()
        await asyncio.wait_for(asyncio.to_thread(device.on_message, None, None, message()), 1)
        await asyncio.wait_for(asyncio.to_thread(device.on_message, None, None, message(value=80)), 1)
        assert await asyncio.to_thread(published.wait, 1), "Timed-out write blocked the queue"
        assert cancelled.is_set()
        assert device.state["min_soc_on_grid"] == 80
        device.client.publish.assert_called_once_with(
            "homeassistant/number/inverter_test/min_soc_on_grid/state", 80, retain=True,
        )

    asyncio.run(scenario())


def test_broker_reconnect_restores_availability_and_subscriptions(device, mqtt_clients):
    factory, _ = mqtt_clients
    assert factory.call_args.kwargs == {
        "callback_api_version": CallbackAPIVersion.VERSION2,
        "client_id": "renac_inverter_test",
    }
    device.client.reconnect_delay_set.assert_called_once_with(min_delay=1, max_delay=60)
    device.on_connect(device.client, None, {}, 0, None)
    assert device._connected
    device.client.publish.reset_mock()
    device.client.subscribe.reset_mock()
    device.on_disconnect(device.client, None, {}, 1, None)
    assert not device._connected
    device.client.reconnect.assert_not_called()
    device.on_connect(device.client, None, {}, 0, None)
    assert device._connected
    device.client.publish.assert_any_call(
        "homeassistant/inverter_test/availability", "online", retain=True,
    )
    assert device.client.subscribe.call_args_list == [
        call("homeassistant/number/inverter_test/min_soc/set"),
        call("homeassistant/number/inverter_test/min_soc_on_grid/set"),
    ]


def test_bridge_shutdown_drains_active_command_without_blocking_asyncio(device, monkeypatch):
    async def scenario():
        started = threading.Event()
        heartbeat = threading.Event()

        def setter(value):
            started.set()
            return heartbeat.wait(1)

        async def inverter_task(addr):
            await asyncio.Event().wait()

        monkeypatch.setattr(bridge, "shutdown_event", asyncio.Event())
        monkeypatch.setattr(bridge, "inverter_mqtt_by_addr", {"ble-test": device})
        monkeypatch.setattr(bridge, "wallbox_mqtt_by_addr", {})
        monkeypatch.setattr(bridge, "run_inverter_task", inverter_task)
        device.set_actuator_callback("min_soc_on_grid", setter)
        await asyncio.to_thread(device.on_message, None, None, message())
        assert await asyncio.to_thread(started.wait, 2)
        bridge.shutdown_event.set()
        asyncio.get_running_loop().call_later(0.01, heartbeat.set)
        await asyncio.wait_for(bridge._run_all(["ble-test"], []), 2)
        device.client.disconnect.assert_called_once()
        device.client.loop_stop.assert_called_once()
        assert device.state["min_soc_on_grid"] == 100

    asyncio.run(scenario())
