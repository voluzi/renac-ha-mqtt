from typing import Optional

from renac_ha_mqtt.mqtt_device import MqttDeviceEntities, RenacMqttDevice, SensorConfig


def _measurement(device_class: str, unit: str) -> SensorConfig:
    return {"device_class": device_class, "unit_of_measurement": unit, "state_class": "measurement"}


INVERTER_ENTITIES: MqttDeviceEntities = {
    "number": {
        "max_charge_current": {
            "unit_of_measurement": "A",
            "min": 0,
            "max": 30,
            "step": 1,
            "mode": "box"
        },
        "max_discharge_current": {
            "unit_of_measurement": "A",
            "min": 0,
            "max": 30,
            "step": 1,
            "mode": "box"
        },
        "min_soc": {
            "unit_of_measurement": "%",
            "min": 5,
            "max": 100,
            "step": 1,
            "mode": "box"
        },
        "min_soc_on_grid": {
            "unit_of_measurement": "%",
            "min": 5,
            "max": 100,
            "step": 1,
            "mode": "box"
        },
        "export_limit": {
            "unit_of_measurement": "W",
            "min": 0,
            "max": 60000,
            "step": 1,
            "mode": "box"
        },
        "power_limit_percent": {
            "unit_of_measurement": "Pn/100",
            "min": -100,
            "max": 100,
            "step": 1,
            "mode": "box"
        },
        # Force Time Use Mode - Period 1
        "force_time_p1_start_hour": {
            "min": 0,
            "max": 23,
            "step": 1,
            "mode": "box"
        },
        "force_time_p1_start_minute": {
            "min": 0,
            "max": 59,
            "step": 1,
            "mode": "box"
        },
        "force_time_p1_end_hour": {
            "min": 0,
            "max": 23,
            "step": 1,
            "mode": "box"
        },
        "force_time_p1_end_minute": {
            "min": 0,
            "max": 59,
            "step": 1,
            "mode": "box"
        },
        # Force Time Use Mode - Period 2
        "force_time_p2_start_hour": {
            "min": 0,
            "max": 23,
            "step": 1,
            "mode": "box"
        },
        "force_time_p2_start_minute": {
            "min": 0,
            "max": 59,
            "step": 1,
            "mode": "box"
        },
        "force_time_p2_end_hour": {
            "min": 0,
            "max": 23,
            "step": 1,
            "mode": "box"
        },
        "force_time_p2_end_minute": {
            "min": 0,
            "max": 59,
            "step": 1,
            "mode": "box"
        },
        # Backup Mode Grid Charge
        "backup_charge_start_hour": {
            "min": 0,
            "max": 23,
            "step": 1,
            "mode": "box"
        },
        "backup_charge_start_minute": {
            "min": 0,
            "max": 59,
            "step": 1,
            "mode": "box"
        },
        "backup_charge_end_hour": {
            "min": 0,
            "max": 23,
            "step": 1,
            "mode": "box"
        },
        "backup_charge_end_minute": {
            "min": 0,
            "max": 59,
            "step": 1,
            "mode": "box"
        },
    },
    "switch": {
        "force_time_p1_enabled": {},
        "force_time_p2_enabled": {},
        "backup_charge_enabled": {},
        "bypass_relay": {"entity_category": "config"},
    },
    "binary_sensor": {
        "forbid_charge": {},
        "forbid_discharge": {},
        "bms_connected": {"device_class": "connectivity", "entity_category": "diagnostic"},
        "meter1_connected": {"device_class": "connectivity", "entity_category": "diagnostic"},
        "fault_active": {"device_class": "problem"},
    },
    "select": {
        "work_mode": {
            "options": [
                "self_use",
                "force_time_use",
                "backup",
                "feed_in_first",
            ],
        }
    },
    "sensor": {
        # Real-time values
        "load_power": {
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
        },
        "pv_power": {
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
        },
        "battery_power": {
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
        },
        "battery_soc": {
            "unit_of_measurement": "%",
            "device_class": "battery",
            "state_class": "measurement",
        },
        "eps_power": {
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
        },
        "inverter_state": {
            "device_class": "enum",
            "options": [
                "Wait", "Check", "Normal", "Standby", "Fault", "Permanent",
                "Update", "EpsCheck", "Eps", "SelfTest", "Idle",
            ],
        },
        "fault_code": {"entity_category": "diagnostic", "has_attributes": True},
        "battery_voltage": _measurement("voltage", "V"),
        "battery_current": _measurement("current", "A"),
        "bms_voltage": _measurement("voltage", "V"),
        "bms_current": _measurement("current", "A"),
        "bms_soh": {"unit_of_measurement": "%", "state_class": "measurement", "entity_category": "diagnostic"},
        "bms_max_charge_a": _measurement("current", "A"),
        "bms_max_discharge_a": _measurement("current", "A"),
        "max_cell_v": _measurement("voltage", "V"),
        "min_cell_v": _measurement("voltage", "V"),
        "bms_cycles": {"state_class": "total_increasing", "entity_category": "diagnostic"},
        **{
            key: _measurement("temperature", "°C")
            for key in ("internal_temp", "charger_temp", "boost_temp", "inverter_temp",
                        "bms_temp", "max_cell_t", "min_cell_t")
        },
        **{
            key: {"enabled_by_default": False, "entity_category": "diagnostic"}
            for key in ("max_cell_v_no", "min_cell_v_no", "max_cell_t_no", "min_cell_t_no", "bms_basic_status")
        },
        **{f"inv_{phase}_current": _measurement("current", "A") for phase in "rst"},
        **{f"inv_{phase}_power": _measurement("power", "W") for phase in "rst"},
        "inv_ac_power": _measurement("power", "W"),
        **{f"eps_{phase}_voltage": _measurement("voltage", "V") for phase in "rst"},
        **{f"eps_{phase}_current": _measurement("current", "A") for phase in "rst"},
        **{f"eps_{phase}_power": _measurement("power", "W") for phase in "rst"},
        "eps_frequency": _measurement("frequency", "Hz"),

        # TOTAL_ENERGY_BLOCK
        "pv_total_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
        "pv_today_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "measurement",
        },
        "battery_total_charge_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
        "battery_today_charge_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "measurement",
        },
        "battery_total_discharge_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
        "battery_today_discharge_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "measurement",
        },
        "feedin_total_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
        "feedin_today_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "measurement",
        },
        "consumption_total_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
        "consumption_today_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "measurement",
        },
        "output_total_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
        "output_today_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "measurement",
        },
        "load_total_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
        "load_today_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "measurement",
        },
        "input_total_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
        "input_today_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "measurement",
        },
        "eps_total_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
        "eps_today_energy": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "measurement",
        },
    }
}


class RenacInverterDevice(RenacMqttDevice):
    def __init__(
            self,
            device_name: str,
            serial_number: Optional[str],
            model: str,
            mqtt_host: str,
            mqtt_port: int = 1883,
            mqtt_user: Optional[str] = None,
            mqtt_password: Optional[str] = None,
            ble_address: Optional[str] = None,
    ):
        if not serial_number and not ble_address:
            raise ValueError("An inverter serial number or BLE address is required")
        super().__init__(
            f"inverter_{serial_number}",
            device_name,
            model,
            INVERTER_ENTITIES,
            mqtt_host,
            mqtt_port,
            mqtt_user,
            mqtt_password,
            client_identifier=f"inverter_{serial_number or ble_address}",
        )
