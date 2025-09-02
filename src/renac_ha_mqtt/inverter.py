from renac_ha_mqtt.mqtt_device import RenacMqttDevice, MqttDeviceEntities
from typing import Optional

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
            serial_number: str,
            model: str,
            mqtt_host: str,
            mqtt_port: int = 1883,
            mqtt_user: Optional[str] = None,
            mqtt_password: Optional[str] = None,
    ):
        super().__init__(
            f"inverter_{serial_number}",
            device_name,
            model,
            INVERTER_ENTITIES,
            mqtt_host,
            mqtt_port,
            mqtt_user,
            mqtt_password,
        )
