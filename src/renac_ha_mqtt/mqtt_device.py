import time
import json
import logging
import paho.mqtt.client as mqtt
from typing import Callable, TypedDict, Optional, Dict, Union

# Setup base logger configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

ActuatorCallback = Callable[[Union[int, float, str, bool, dict, list]], Optional[bool]]


class SensorConfig(TypedDict, total=False):
    unit_of_measurement: str
    device_class: str
    state_class: str


class NumberConfig(TypedDict, total=False):
    unit_of_measurement: str
    min: int
    max: int
    step: int
    mode: str


class MqttDeviceEntities(TypedDict, total=False):
    sensor: Dict[str, SensorConfig]
    number: Dict[str, NumberConfig]


class RenacMqttDevice:
    def __init__(self,
                 device_id: str,
                 device_name: str,
                 device_model: str,
                 entities: MqttDeviceEntities,
                 mqtt_host: str,
                 mqtt_port: int = 1883,
                 mqtt_user: Optional[str] = None,
                 mqtt_password: Optional[str] = None):
        self.device_id = device_id
        self.device_name = device_name
        self.device_model = device_model
        self.entities = entities
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_user = mqtt_user
        self.mqtt_password = mqtt_password
        self.state = {}
        self._actuator_callbacks: Dict[str, ActuatorCallback] = {}

        self.logger = logging.getLogger(f"renac.{device_id}")

        self.client = mqtt.Client()
        if self.mqtt_user:
            self.client.username_pw_set(self.mqtt_user, self.mqtt_password)

        self.client.will_set(f"homeassistant/{self.device_id}/availability", "offline", retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        self._connected = False

    def connect(self):
        self.logger.info("🚀 Connecting to MQTT broker...")
        self.client.connect(self.mqtt_host, self.mqtt_port, 60)
        self.client.loop_start()
        self.logger.info("📡 MQTT loop started")

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()

    def publish(self, topic: str, payload, retain: bool = False):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        self.client.publish(topic, payload, retain=retain)

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.logger.info("✅ MQTT connected")
            self._connected = True
            self.client.publish(f"homeassistant/{self.device_id}/availability", "online", retain=True)
            self._register_sensors()
            self._register_actuators()
        else:
            self.logger.error(f"❌ MQTT connect failed, code {rc}")

    def on_disconnect(self, client, userdata, rc):
        self.logger.warning("⚠️ MQTT disconnected. Reconnecting...")
        self._connected = False
        while not self._connected:
            try:
                client.reconnect()
                self.logger.info("✅ MQTT reconnected")
                return
            except Exception as e:
                self.logger.error(f"❌ Reconnect failed: {e}")
                time.sleep(5)

    def on_message(self, client, userdata, msg):
        topic_parts = msg.topic.split('/')
        if len(topic_parts) >= 5 and topic_parts[-1] == "set":
            key = topic_parts[-2]
            payload = msg.payload.decode("utf-8").strip()

            if key in self._actuator_callbacks:
                try:
                    value = json.loads(payload)
                except json.JSONDecodeError as e:
                    self.logger.warning(f"❌ Failed to parse JSON payload for {key}: {payload} ({e})")
                    return

                callback = self._actuator_callbacks[key]
                self.logger.info(f"🔧 Received command for {key}: {value}")
                try:
                    result = callback(value)
                    if isinstance(result, bool) and result is False:
                        self.logger.warning(f"⚠️ Command for {key} failed or was rejected")
                    else:
                        self.logger.info(f"✅ Command for {key} executed")
                        self._set_state(key, value)
                        topic = f"homeassistant/{self.get_entity_type(key)}/{self.device_id}/{key}/state"
                        self.client.publish(topic, value, retain=True)
                        self.logger.debug(f"🔄 State updated: {key} = {value}")

                except Exception as e:
                    self.logger.error(f"❌ Error executing callback for {key}: {e}")
            else:
                self.logger.warning(f"⚠️ No callback found for actuator key: {key}")
        else:
            self.logger.debug(f"📩 Unhandled MQTT message: {msg.topic} => {msg.payload}")

    def _register_sensors(self):
        for key, sensor in self.entities.get("sensor", {}).items():
            base_topic = f"homeassistant/sensor/{self.device_id}/{key}"
            config = {
                "name": f"{self.device_name} {key.replace('_', ' ').title()}",
                "state_topic": f"{base_topic}/state",
                "unique_id": f"{self.device_id}_{key}",
                "availability_topic": f"homeassistant/{self.device_id}/availability",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": {
                    "identifiers": [self.device_id],
                    "name": self.device_name,
                    "manufacturer": "RENAC",
                    "model": self.device_model,
                },
            }
            config.update(sensor)
            self.client.publish(f"{base_topic}/config", json.dumps(config), retain=True)

    def _register_actuators(self):
        for key, number in self.entities.get("number", {}).items():
            base_topic = f"homeassistant/number/{self.device_id}/{key}"
            config = {
                "name": f"{self.device_name} {key.replace('_', ' ').title()}",
                "state_topic": f"{base_topic}/state",
                "command_topic": f"{base_topic}/set",
                "unique_id": f"{self.device_id}_{key}",
                "availability_topic": f"homeassistant/{self.device_id}/availability",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": {
                    "identifiers": [self.device_id],
                    "name": self.device_name,
                    "manufacturer": "RENAC",
                    "model": self.device_model,
                },
            }
            config.update(number)
            self.client.subscribe(f"{base_topic}/set")
            self.client.publish(f"{base_topic}/config", json.dumps(config), retain=True)

    def _set_state(self, key: str, value: any):
        if self.state.get(key) != value:
            self.logger.debug(f"🔄 State updated: {key} = {value}")
            self.state[key] = value
            return True
        return False

    def set_sensor_value(self, key_or_dict: Union[str, Dict[str, any]], value: Optional[any] = None):
        updates = {}
        if isinstance(key_or_dict, dict):
            for k, v in key_or_dict.items():
                if self._set_state(k, v):
                    updates[k] = v
        else:
            if self._set_state(key_or_dict, value):
                updates[key_or_dict] = value

        for k, v in updates.items():
            topic = f"homeassistant/sensor/{self.device_id}/{k}/state"
            self.client.publish(topic, v, retain=True)

        if updates:
            self.logger.info(f"📤 Published sensor updates: {updates}")

        return bool(updates)

    def get_entity_type(self, key: str) -> Optional[str]:
        for entity_type, entity_dict in self.entities.items():
            if isinstance(entity_type, str) and key in entity_dict:
                return entity_type
        return None

    def set_actuator_callback(self, key: str, callback: ActuatorCallback,
                              value: Optional[Union[int, float, str, bool]] = None):
        self._actuator_callbacks[key] = callback
        self.logger.debug(f"✅ Callback registered for actuator: {key}")
        if value is not None:
            self._set_state(key, value)
            topic = f"homeassistant/{self.get_entity_type(key)}/{self.device_id}/{key}/state"
            self.client.publish(topic, value, retain=True)
            self.logger.info(f"📤 Initial state published for actuator {key}: {value}")
