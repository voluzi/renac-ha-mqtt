"""MQTT device helpers for RENAC hardware."""

import json
import logging
import os
import socket
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
import paho.mqtt.client as mqtt
from typing import Any, Callable, TypedDict, Optional, Dict, Union, List

ActuatorPayload = Union[int, float, str, bool, Dict[str, Any], List[Any]]
ActuatorCallback = Callable[[ActuatorPayload], Union[Optional[bool], Future[Optional[bool]]]]
COMMAND_DRAIN_TIMEOUT_S = 2.0
OFFLINE_PUBLISH_TIMEOUT_S = 2.0


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


class SelectConfig(TypedDict, total=False):
    options: List[str]


class SwitchConfig(TypedDict, total=False):
    device_class: str


class MqttDeviceEntities(TypedDict, total=False):
    sensor: Dict[str, SensorConfig]
    number: Dict[str, NumberConfig]
    select: Dict[str, SelectConfig]
    switch: Dict[str, SwitchConfig]


class RenacMqttDevice:
    """Base MQTT device used by the bridge to publish telemetry and accept commands."""

    def __init__(self,
                 device_id: str,
                 device_name: str,
                 device_model: str,
                 entities: MqttDeviceEntities,
                 mqtt_host: str,
                 mqtt_port: int = 1883,
                 mqtt_user: Optional[str] = None,
                 mqtt_password: Optional[str] = None,
                 client_identifier: Optional[str] = None):
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
        self._command_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"renac-{device_id}")
        self._command_lock = threading.Lock()
        self._commands: set[Future] = set()
        self._active_write: Optional[Future] = None
        self._stopping = False
        self._cancel_commands = False

        self.logger = logging.getLogger(f"renac.{device_id}")

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"renac_{client_identifier or device_id}_"
                      f"{os.getenv('MQTT_CLIENT_ID_PREFIX') or socket.gethostname()}_{os.getpid()}",
        )
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        if self.mqtt_user:
            self.client.username_pw_set(self.mqtt_user, self.mqtt_password)

        self.client.will_set(f"homeassistant/{self.device_id}/availability", "offline", retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        self._connected = False

    def connect(self) -> None:
        """Connect to the configured MQTT broker and start the network loop."""
        self.logger.info("🚀 Connecting to MQTT broker...")
        self.client.connect_async(self.mqtt_host, self.mqtt_port, 60)
        self.client.loop_start()
        self.logger.info("📡 MQTT loop started")

    def disconnect(self) -> None:
        """Drain commands briefly and flush offline availability before closing MQTT."""
        with self._command_lock:
            self._stopping = True
            commands = list(self._commands)
        self._command_worker.shutdown(wait=False, cancel_futures=True)
        pending = [command for command in commands if not command.done()]
        if pending and wait(pending, timeout=COMMAND_DRAIN_TIMEOUT_S).not_done:
            with self._command_lock:
                self._cancel_commands = True
                if self._active_write is not None:
                    self._active_write.cancel()
            self.logger.warning("Timed out draining MQTT commands")
        try:
            published = self.client.publish(
                f"homeassistant/{self.device_id}/availability", "offline", qos=1, retain=True,
            )
            published.wait_for_publish(timeout=OFFLINE_PUBLISH_TIMEOUT_S)
            if not published.is_published():
                self.logger.warning("Timed out publishing offline availability")
        except (RuntimeError, ValueError) as exc:
            self.logger.warning("Could not publish offline availability: %s", exc)
        finally:
            self.client.disconnect()
            self.client.loop_stop()
            self._connected = False

    def publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        """Publish ``payload`` on ``topic``.

        ``payload`` is JSON-encoded if it is a mapping or sequence.
        """
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        self.client.publish(topic, payload, retain=retain)

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if self._stopping:
            return
        if reason_code == 0:
            self.logger.info("✅ MQTT connected")
            self._connected = True
            self.client.publish(f"homeassistant/{self.device_id}/availability", "online", retain=True)
            self._register_sensors()
            self._register_actuators()
            with self._command_lock:
                for key, value in self.state.copy().items():
                    entity_type = self.get_entity_type(key) or "sensor"
                    topic = f"homeassistant/{entity_type}/{self.device_id}/{key}/state"
                    self.publish(topic, value, retain=True)
        else:
            self.logger.error(f"❌ MQTT connect failed, code {reason_code}")

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.logger.warning("⚠️ MQTT disconnected (reason: %s)", reason_code)
        self._connected = False

    def on_message(self, client, userdata, msg):
        topic_parts = msg.topic.split('/')
        if len(topic_parts) >= 5 and topic_parts[-1] == "set":
            key = topic_parts[-2]
            payload = msg.payload.decode("utf-8").strip()

            if key in self._actuator_callbacks:
                try:
                    value = json.loads(payload)
                except json.JSONDecodeError:
                    value = payload

                callback = self._actuator_callbacks[key]
                self.logger.info(f"🔧 Received command for {key}: {value}")
                with self._command_lock:
                    if self._stopping:
                        return
                    command = self._command_worker.submit(self._execute_command, key, value, callback)
                    self._commands.add(command)
                command.add_done_callback(self._command_finished)
            else:
                self.logger.warning(f"⚠️ No callback found for actuator key: {key}")
        else:
            self.logger.debug(f"📩 Unhandled MQTT message: {msg.topic} => {msg.payload}")

    def _command_finished(self, command: Future) -> None:
        with self._command_lock:
            self._commands.discard(command)

    def _execute_command(self, key: str, value: ActuatorPayload, callback: ActuatorCallback) -> None:
        try:
            with self._command_lock:
                if self._stopping:
                    return
            result = callback(value)
            if isinstance(result, Future):
                with self._command_lock:
                    self._active_write = result
                    if self._cancel_commands:
                        result.cancel()
                result = result.result()
            with self._command_lock:
                if result is False or self._cancel_commands:
                    self.logger.warning(f"⚠️ Command for {key} failed or was rejected")
                    return
                self.logger.info(f"✅ Command for {key} executed")
                self._set_state(key, value)
                topic = f"homeassistant/{self.get_entity_type(key)}/{self.device_id}/{key}/state"
                self.client.publish(topic, value, retain=True)
        except Exception as exc:
            self.logger.warning("⚠️ Error executing callback for %s: %s", key, str(exc) or type(exc).__name__)
        finally:
            with self._command_lock:
                self._active_write = None

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

        for key, select in self.entities.get("select", {}).items():
            base_topic = f"homeassistant/select/{self.device_id}/{key}"
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
            config.update(select)
            self.client.subscribe(f"{base_topic}/set")
            self.client.publish(f"{base_topic}/config", json.dumps(config), retain=True)

        for key, switch in self.entities.get("switch", {}).items():
            base_topic = f"homeassistant/switch/{self.device_id}/{key}"
            config = {
                "name": f"{self.device_name} {key.replace('_', ' ').title()}",
                "state_topic": f"{base_topic}/state",
                "command_topic": f"{base_topic}/set",
                "unique_id": f"{self.device_id}_{key}",
                "availability_topic": f"homeassistant/{self.device_id}/availability",
                "payload_available": "online",
                "payload_not_available": "offline",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                "device": {
                    "identifiers": [self.device_id],
                    "name": self.device_name,
                    "manufacturer": "RENAC",
                    "model": self.device_model,
                },
            }
            config.update(switch)
            self.client.subscribe(f"{base_topic}/set")
            self.client.publish(f"{base_topic}/config", json.dumps(config), retain=True)

    def _set_state(self, key: str, value: Any) -> bool:
        """Store ``value`` in the internal state if it changed."""
        if self.state.get(key) != value:
            self.logger.debug(f"🔄 State updated: {key} = {value}")
            self.state[key] = value
            return True
        return False

    def set_sensor_value(self, key_or_dict: Union[str, Dict[str, Any]], value: Optional[Any] = None) -> bool:
        """Update sensor state and publish MQTT messages.

        ``key_or_dict`` may be either a single sensor key or a mapping of
        keys to values. Only changed values are published.
        """
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
        """Return the entity category for ``key`` if present."""
        for entity_type, entity_dict in self.entities.items():
            if key in entity_dict:
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

    def set_actuator_value(self, key: str, value: ActuatorPayload) -> bool:
        """Update actuator state and publish MQTT message if it changed."""
        if self._set_state(key, value):
            entity_type = self.get_entity_type(key)
            if entity_type is None:
                return False
            topic = f"homeassistant/{entity_type}/{self.device_id}/{key}/state"
            self.client.publish(topic, value, retain=True)
            self.logger.info(f"📤 Published actuator update: {key} = {value}")
            return True
        return False
