import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from renac_ha_bridge import __main__ as bridge
from renac_ha_mqtt import RenacInverterDevice


def test_inverter_discovery_describes_measurements_diagnostics_and_bypass(mqtt_clients):
    device = RenacInverterDevice("RENAC Inverter", "test", "N3-HV-10.0", "localhost")
    try:
        device.on_connect(device.client, None, {}, 0, None)
        config = {
            (call.args[0].split("/")[1], call.args[0].split("/")[-2]): json.loads(call.args[1])
            for call in device.client.publish.call_args_list
            if call.args[0].endswith("/config") and call.kwargs["retain"]
        }
        for key, cls, unit in (
            ("internal_temp", "temperature", "°C"),
            ("battery_voltage", "voltage", "V"),
            ("bms_current", "current", "A"),
            ("min_cell_v", "voltage", "V"),
            ("inv_ac_power", "power", "W"),
            ("eps_r_voltage", "voltage", "V"),
            ("eps_s_current", "current", "A"),
            ("eps_t_power", "power", "W"),
            ("eps_frequency", "frequency", "Hz"),
        ):
            sensor = config["sensor", key]
            assert (sensor["device_class"], sensor["state_class"], sensor["unit_of_measurement"]) == (
                cls, "measurement", unit,
            )
        for key in ("battery_power", "battery_soc", "eps_power"):
            sensor = config["sensor", key]
            assert sensor["unique_id"] == f"inverter_test_{key}"
            assert sensor["state_topic"] == f"homeassistant/sensor/inverter_test/{key}/state"
        assert ("sensor", "bms_soc") not in config

        state = config["sensor", "inverter_state"]
        assert state["device_class"] == "enum"
        assert state["options"] == [
            "Wait", "Check", "Normal", "Standby", "Fault", "Permanent",
            "Update", "EpsCheck", "Eps", "SelfTest", "Idle",
        ]
        assert "state_class" not in state and "unit_of_measurement" not in state
        code = config["sensor", "fault_code"]
        assert code["entity_category"] == "diagnostic"
        assert code["json_attributes_topic"] == "homeassistant/sensor/inverter_test/fault_code/attributes"
        assert config["sensor", "bms_basic_status"]["enabled_by_default"] is False
        assert config["sensor", "max_cell_v_no"]["entity_category"] == "diagnostic"
        assert config["sensor", "bms_cycles"]["state_class"] == "total_increasing"
        assert config["binary_sensor", "fault_active"]["device_class"] == "problem"
        assert config["binary_sensor", "bms_connected"]["device_class"] == "connectivity"
        assert config["binary_sensor", "meter1_connected"]["entity_category"] == "diagnostic"
        assert config["binary_sensor", "forbid_charge"]["payload_on"] == "ON"

        relay = config["switch", "bypass_relay"]
        assert relay["entity_category"] == "config"
        assert relay["command_topic"] == "homeassistant/switch/inverter_test/bypass_relay/set"
        assert relay["state_topic"] == "homeassistant/switch/inverter_test/bypass_relay/state"
        assert (relay["payload_on"], relay["payload_off"]) == ("ON", "OFF")
        device.client.subscribe.assert_any_call(relay["command_topic"])
    finally:
        device.disconnect()


def test_inverter_poll_separates_fault_code_from_alarm_and_bypass_is_confirmed(
    bridge_devices, monkeypatch,
):
    async def scenario():
        monkeypatch.setattr(bridge, "shutdown_event", asyncio.Event())
        monkeypatch.setattr(bridge, "POLL_INTERVAL_S", 0)
        inverter = MagicMock(spec=bridge.RenacInverterBLE)
        for name in dir(bridge.RenacInverterBLE):
            if name.startswith("get_"):
                setattr(inverter, name, AsyncMock(return_value=None))
        inverter.get_info.return_value = {"sn": "test", "model": "N3-HV-10.0"}
        inverter.get_bypass_relay_enabled.return_value = False
        inverter.set_bypass_relay_enabled.side_effect = [False, True]
        inverter.get_inverter_status.side_effect = [
            {"inverter_state": "Normal", "fault_active": False, "forbid_charge": True},
            {"inverter_state": "Fault", "fault_active": True, "forbid_charge": False},
        ]
        fault_words = (0, 0, 0, 0, 0, 0, 24, 0, 0, 0, 0, 0, 0, 0, 0, 0x8001)
        inverter.get_inverter_diagnostics.return_value = {"fault_words": fault_words}
        ready = asyncio.Event()
        hold = asyncio.Event()

        async def overview():
            if inverter.get_power_and_energy_overview.await_count == 3:
                ready.set()
                await hold.wait()

        inverter.get_power_and_energy_overview.side_effect = overview
        monkeypatch.setattr(bridge, "RenacInverterBLE", lambda addr: inverter)
        task = asyncio.create_task(bridge.run_inverter_task("ble-test"))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            dev = bridge.inverter_mqtt_by_addr["ble-test"]
            dev.command_debounce_s = 0.1
            client = dev.client
            for key, state in (("fault_active", "OFF"), ("fault_active", "ON"),
                               ("forbid_charge", "ON"), ("forbid_charge", "OFF")):
                client.publish.assert_any_call(
                    f"homeassistant/binary_sensor/inverter_test/{key}/state", state, retain=True,
                )
            client.publish.assert_any_call(
                "homeassistant/sensor/inverter_test/fault_code/state", "07:0x0018,16:0x8001", retain=True,
            )
            client.publish.assert_any_call(
                "homeassistant/switch/inverter_test/bypass_relay/state", "OFF", retain=True,
            )
            assert inverter.get_inverter_status.await_count == 2
            inverter.get_inverter_diagnostics.assert_awaited_once()

            attrs_topic = "homeassistant/sensor/inverter_test/fault_code/attributes"
            client.publish.reset_mock()
            dev.on_connect(client, None, {}, 0, None)
            attrs = next(call for call in client.publish.call_args_list if call.args[0] == attrs_topic)
            assert attrs.kwargs["retain"]
            assert json.loads(attrs.args[1]) == {
                f"fault_{index}": value for index, value in enumerate(fault_words, 1)
            }

            published = threading.Event()
            states = []
            state_topic = "homeassistant/switch/inverter_test/bypass_relay/state"
            publish_info = client.publish.return_value

            def publish(topic, payload, **kwargs):
                if topic == state_topic:
                    states.append(payload)
                    published.set()
                return publish_info

            client.publish.side_effect = publish
            command_topic = "homeassistant/switch/inverter_test/bypass_relay/set"

            def command(payload):
                dev.on_message(client, None, SimpleNamespace(topic=command_topic, payload=payload))

            command(b"OFF")
            command(b"ON")
            assert await asyncio.to_thread(published.wait, 2)
            inverter.set_bypass_relay_enabled.assert_awaited_once_with(True)
            assert inverter.get_bypass_relay_enabled.await_count == 2
            assert states == ["OFF"]

            published.clear()
            command(b"ON")
            assert await asyncio.to_thread(published.wait, 2)
            assert inverter.set_bypass_relay_enabled.await_count == 2
            assert states == ["OFF", "ON"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
