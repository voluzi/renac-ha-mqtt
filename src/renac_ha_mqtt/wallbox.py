from renac_ha_mqtt.mqtt_device import RenacMqttDevice, MqttDeviceEntities
from typing import Optional

WALLBOX_ENTITIES: MqttDeviceEntities = {
    "sensor": {
        "phase_a_voltage": {
            "unit_of_measurement": "V",
            "device_class": "voltage",
            "state_class": "measurement",
        },
        "phase_b_voltage": {
            "unit_of_measurement": "V",
            "device_class": "voltage",
            "state_class": "measurement",
        },
        "phase_c_voltage": {
            "unit_of_measurement": "V",
            "device_class": "voltage",
            "state_class": "measurement",
        },
        "phase_a_current": {
            "unit_of_measurement": "A",
            "device_class": "current",
            "state_class": "measurement",
        },
        "phase_b_current": {
            "unit_of_measurement": "A",
            "device_class": "current",
            "state_class": "measurement",
        },
        "phase_c_current": {
            "unit_of_measurement": "A",
            "device_class": "current",
            "state_class": "measurement",
        },
        "power": {
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
        },
        "temperature": {
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
        },
        "current_charging_amount": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "measurement",
        },
        "current_charging_time": {
            "unit_of_measurement": "min",
            "state_class": "measurement",
        },
        "total_charge": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
        "state": {
            "device_class": "enum",
            "options": ["idle", "charging", "paused", "disconnected", "error", "completed", "scheduled"]
        },
    }
}


class RenacWallboxDevice(RenacMqttDevice):
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
            f"wallbox_{serial_number}",
            device_name,
            model,
            WALLBOX_ENTITIES,
            mqtt_host,
            mqtt_port,
            mqtt_user,
            mqtt_password,
        )
