import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import call

import pytest

from renac_ha_bridge import __main__ as bridge
from renac_ha_mqtt import mqtt_device


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
        publish_info = device.client.publish.return_value

        def publish(*args, **kwargs):
            if args[1] == 20:
                published.set()
            return publish_info

        device.client.publish.side_effect = publish
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
        attempted = threading.Event()

        def setter(value):
            attempted.set()
            if isinstance(failure, Exception):
                raise failure
            return failure

        device.set_actuator_callback("min_soc_on_grid", setter, 50)
        device.client.publish.reset_mock()
        await asyncio.to_thread(device.on_message, None, None, message())
        assert await asyncio.to_thread(attempted.wait, 2)
        await asyncio.to_thread(device.disconnect)
        assert device.state["min_soc_on_grid"] == 50
        assert not any(c.args[0].endswith("/state") for c in device.client.publish.call_args_list)
        assert any(record.levelname == "WARNING" for record in caplog.records)

    asyncio.run(scenario())


def test_timed_out_write_is_cancelled_before_next_command(device, monkeypatch, caplog):
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
        publish_info = device.client.publish.return_value

        def publish(*args, **kwargs):
            published.set()
            return publish_info

        device.client.publish.side_effect = publish
        await asyncio.wait_for(asyncio.to_thread(device.on_message, None, None, message()), 1)
        await asyncio.wait_for(asyncio.to_thread(device.on_message, None, None, message(value=80)), 1)
        assert await asyncio.to_thread(published.wait, 1), "Timed-out write blocked the queue"
        assert cancelled.is_set()
        assert device.state["min_soc_on_grid"] == 80
        device.client.publish.assert_called_once_with(
            "homeassistant/number/inverter_test/min_soc_on_grid/state", 80, retain=True,
        )
        assert "TimeoutError" in caplog.text

    asyncio.run(scenario())


def test_broker_reconnect_restores_availability_and_subscriptions(device):
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


@pytest.mark.parametrize("reconnecting", [False, True])
def test_connect_republishes_state_cached_while_disconnected(device, reconnecting):
    if reconnecting:
        device.on_connect(device.client, None, {}, 0, None)
        device.on_disconnect(device.client, None, {}, 1, None)
    assert not device._connected
    device.entities.update({
        "sensor": {"battery_soc": {}},
        "switch": {"charging": {}},
        "select": {"charging_mode": {}},
    })
    device.set_sensor_value({"battery_soc": 0, "extra_sensor": 42})
    device.set_sensor_value("nullable_sensor", 123)
    device.set_sensor_value("nullable_sensor", None)
    device.set_actuator_value("min_soc_on_grid", 80)
    device.set_actuator_value("charging", "OFF")
    device.set_actuator_value("charging_mode", "eco")
    device.client.publish.reset_mock()

    device.on_connect(device.client, None, {}, 0, None)

    device.client.publish.assert_has_calls([
        call("homeassistant/sensor/inverter_test/battery_soc/state", 0, retain=True),
        call("homeassistant/sensor/inverter_test/extra_sensor/state", 42, retain=True),
        call("homeassistant/number/inverter_test/min_soc_on_grid/state", 80, retain=True),
        call("homeassistant/switch/inverter_test/charging/state", "OFF", retain=True),
        call("homeassistant/select/inverter_test/charging_mode/state", "eco", retain=True),
    ], any_order=True)
    assert all(published.args[1] is not None for published in device.client.publish.call_args_list)


def test_unknown_actuator_is_not_cached_or_republished(device):
    assert not device.set_actuator_value("unknown_actuator", 42)
    assert "unknown_actuator" not in device.state
    device.on_connect(device.client, None, {}, 0, None)
    assert not any(
        "unknown_actuator" in published.args[0]
        for published in device.client.publish.call_args_list
    )


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


def test_disconnect_flushes_retained_offline_before_closing(device):
    device.disconnect()
    device.on_connect(device.client, None, {}, 0, None)
    calls = device.client.mock_calls
    offline = call.publish("homeassistant/inverter_test/availability", "offline", qos=1, retain=True)
    assert offline in calls
    assert device.client.publish.call_count == 1
    wait_call = next(c for c in calls if c[0] == "publish().wait_for_publish")
    assert 0 < wait_call.kwargs["timeout"] <= 5
    assert calls.index(offline) < calls.index(wait_call) < calls.index(call.disconnect())


def test_connect_during_shutdown_publishes_nothing(device):
    device._stopping = True
    device.on_connect(device.client, None, {}, 0, None)
    device.client.publish.assert_not_called()
    device.client.subscribe.assert_not_called()
    assert not device._connected


def test_disconnect_still_closes_when_offline_publish_fails(device):
    device.client.publish.return_value.wait_for_publish.side_effect = RuntimeError("Broker offline")
    device.disconnect()
    device.client.disconnect.assert_called_once()
    device.client.loop_stop.assert_called_once()


def test_shutdown_cancels_hung_write_before_stopping_ble(device, monkeypatch):
    monkeypatch.setattr(mqtt_device, "COMMAND_DRAIN_TIMEOUT_S", 0.02)

    async def scenario():
        ble_started = asyncio.Event()
        write_started = asyncio.Event()
        write_cancelled = asyncio.Event()
        order = []
        writes = []

        async def setter(value):
            writes.append(value)
            write_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                write_cancelled.set()

        async def inverter_task(addr):
            ble_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                order.append("ble_stopped")

        def fail_cleanup():
            raise RuntimeError("Cleanup failed")

        failed_device = SimpleNamespace(disconnect=fail_cleanup)
        monkeypatch.setattr(bridge, "shutdown_event", asyncio.Event())
        monkeypatch.setattr(bridge, "inverter_mqtt_by_addr", {"test": device})
        monkeypatch.setattr(bridge, "wallbox_mqtt_by_addr", {"failed": failed_device})
        monkeypatch.setattr(bridge, "run_inverter_task", inverter_task)
        device.client.disconnect.side_effect = lambda: order.append("mqtt_stopped")
        device.set_actuator_callback(
            "min_soc_on_grid", bridge.wrap_async_callback(asyncio.get_running_loop(), setter),
        )
        task = asyncio.create_task(bridge._run_all(["test"], []))
        try:
            await asyncio.wait_for(ble_started.wait(), 1)
            await asyncio.to_thread(device.on_message, None, None, message())
            await asyncio.wait_for(write_started.wait(), 1)
            await asyncio.to_thread(device.on_message, None, None, message(value=80))
            bridge.shutdown_event.set()
            await asyncio.wait_for(task, 1)
            await asyncio.wait_for(write_cancelled.wait(), 1)
            assert order == ["mqtt_stopped", "ble_stopped"]
            assert writes == [100]
            assert "min_soc_on_grid" not in device.state
            device.client.publish.assert_called_once_with(
                "homeassistant/inverter_test/availability", "offline", qos=1, retain=True,
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def wait_for_debounce(device):
    with device._command_lock:
        timers = list(device._debounce_timers.values())
    for timer in timers:
        timer.join(timeout=2)
        assert not timer.is_alive(), "Debounce timer did not finish"
    device._command_worker.submit(lambda: None).result(timeout=2)


def test_rapid_number_commands_only_write_last_value(device):
    device.command_debounce_s = 0.05
    values = []
    device.set_actuator_callback("min_soc_on_grid", lambda value: values.append(value) or True)
    for value in (10, 11, 12, 13):
        device.on_message(None, None, message(value=value))
    wait_for_debounce(device)
    assert values == [13]
    device.client.publish.assert_called_once_with(
        "homeassistant/number/inverter_test/min_soc_on_grid/state", 13, retain=True,
    )


def test_debounce_is_per_entity(device):
    device.command_debounce_s = 0.05
    values = []
    for key in ("min_soc", "min_soc_on_grid"):
        device.set_actuator_callback(key, lambda value, key=key: values.append((key, value)) or True)
    device.on_message(None, None, message("min_soc", 10))
    device.on_message(None, None, message(value=20))
    device.on_message(None, None, message("min_soc", 11))
    wait_for_debounce(device)
    assert sorted(values) == [("min_soc", 11), ("min_soc_on_grid", 20)]


def test_value_arriving_during_write_runs_once_after_it(device):
    device.command_debounce_s = 0.05
    started, release = threading.Event(), threading.Event()
    values = []

    def setter(value):
        values.append(value)
        if value == 10:
            started.set()
            assert release.wait(2)
        return True

    device.set_actuator_callback("min_soc_on_grid", setter)
    device.on_message(None, None, message(value=10))
    try:
        assert started.wait(2)
        for value in (11, 12):
            device.on_message(None, None, message(value=value))
        threading.Event().wait(0.12)
        device.on_message(None, None, message(value=13))
    finally:
        release.set()
    wait_for_debounce(device)
    assert values == [10, 13]


def test_switch_commands_are_not_debounced(device):
    device.command_debounce_s = 10
    device.entities["switch"] = {"charging": {}}
    applied = threading.Event()
    device.set_actuator_callback("charging", lambda value: applied.set() or True)
    device.on_message(None, None, message("charging", "ON"))
    assert applied.wait(1)


@pytest.mark.parametrize("failure", [False, None, RuntimeError("link lost")])
def test_failed_command_republishes_fresh_read(device, failure, caplog):
    def setter(value):
        if isinstance(failure, Exception):
            raise failure
        return failure

    device.command_debounce_s = 0
    device.set_actuator_callback("min_soc_on_grid", setter, 42, refresh=lambda: 42)
    device.client.publish.reset_mock()
    device.on_message(None, None, message(value=100))
    wait_for_debounce(device)
    assert device.state["min_soc_on_grid"] == 42
    device.client.publish.assert_called_once_with(
        "homeassistant/number/inverter_test/min_soc_on_grid/state", 42, retain=True,
    )
    assert "min_soc_on_grid=100" in caplog.text


def test_unconfirmed_write_is_not_published(device):
    device.command_debounce_s = 0
    device.set_actuator_callback("min_soc_on_grid", lambda value: None, 50)
    device.client.publish.reset_mock()
    device.on_message(None, None, message())
    wait_for_debounce(device)
    assert device.state["min_soc_on_grid"] == 50
    device.client.publish.assert_not_called()


def test_disconnect_drops_pending_debounced_command(device, caplog):
    device.command_debounce_s = 10
    values = []
    device.set_actuator_callback("min_soc_on_grid", lambda value: values.append(value) or True)
    device.on_message(None, None, message(value=10))
    device.disconnect()
    assert values == []
    assert "min_soc_on_grid" in caplog.text
    assert not any(c.args[0].endswith("/state") for c in device.client.publish.call_args_list)


def test_set_actuator_value_ignores_none(device):
    device.set_actuator_value("min_soc_on_grid", 42)
    device.client.publish.reset_mock()
    assert device.set_actuator_value("min_soc_on_grid", None) is False
    assert device.state["min_soc_on_grid"] == 42
    device.client.publish.assert_not_called()


def test_failed_command_skips_refresh_when_newer_value_is_pending(device):
    device.command_debounce_s = 0.05
    started, release = threading.Event(), threading.Event()
    refreshed = []
    values = []

    def setter(value):
        values.append(value)
        if value == 10:
            started.set()
            assert release.wait(2)
            return False
        return True

    device.set_actuator_callback("min_soc_on_grid", setter, refresh=lambda: refreshed.append(True) or 42)
    device.on_message(None, None, message(value=10))
    try:
        assert started.wait(2)
        device.on_message(None, None, message(value=13))
        threading.Event().wait(0.12)
    finally:
        release.set()
    wait_for_debounce(device)
    assert values == [10, 13]
    assert refreshed == []
    assert device.state["min_soc_on_grid"] == 13


def test_debounced_command_uses_callback_registered_after_reconnect(device):
    device.command_debounce_s = 0.05
    values = []
    device.set_actuator_callback("min_soc_on_grid", lambda value: values.append(("old", value)) or True)
    device.on_message(None, None, message(value=13))
    device.set_actuator_callback("min_soc_on_grid", lambda value: values.append(("new", value)) or True)
    wait_for_debounce(device)
    assert values == [("new", 13)]


def test_failed_command_waits_for_async_refresh(device):
    async def scenario():
        refreshed = asyncio.Event()
        published = threading.Event()

        async def refresh():
            await asyncio.sleep(0)
            refreshed.set()
            return 0

        device.set_actuator_callback(
            "min_soc_on_grid", lambda value: None, 50,
            refresh=bridge.wrap_async_refresh(asyncio.get_running_loop(), refresh),
        )
        device.client.publish.reset_mock()
        publish_info = device.client.publish.return_value

        def publish(*args, **kwargs):
            published.set()
            return publish_info

        device.client.publish.side_effect = publish
        await asyncio.to_thread(device.on_message, None, None, message())
        assert await asyncio.to_thread(published.wait, 2)
        assert refreshed.is_set()
        assert device.state["min_soc_on_grid"] == 0
        device.client.publish.assert_called_once_with(
            "homeassistant/number/inverter_test/min_soc_on_grid/state", 0, retain=True,
        )
    asyncio.run(scenario())
