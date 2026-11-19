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
BLE_WRITE_TIMEOUT_S = 30.0
COMMAND_DEBOUNCE_S = float(os.getenv("RENAC_COMMAND_DEBOUNCE_S", "1.0"))
ActuatorRefresh = Callable[[], Union[Optional[ActuatorPayload], Future]]


class SensorConfig(TypedDict, total=False):
    unit_of_measurement: str
    device_class: str
    state_class: str
    options: list[str]
    entity_category: str
    enabled_by_default: bool
    has_attributes: bool


class BinarySensorConfig(TypedDict, total=False):
    device_class: str
    entity_category: str


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
    entity_category: str


class MqttDeviceEntities(TypedDict, total=False):
    sensor: Dict[str, SensorConfig]
    binary_sensor: dict[str, BinarySensorConfig]
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
        self._sensor_attributes: dict[str, dict[str, Any]] = {}
        self._actuator_callbacks: Dict[str, ActuatorCallback] = {}
        self._actuator_refreshers: Dict[str, ActuatorRefresh] = {}
        self._debounced_actuators: set[str] = set()
        self.command_debounce_s = COMMAND_DEBOUNCE_S
        self._pending_values: Dict[str, ActuatorPayload] = {}
        self._debounce_timers: Dict[str, threading.Timer] = {}
        self._superseded: Dict[str, int] = {}
        self._command_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"renac-{device_id}")
        self._command_lock = threading.Lock()
        self._commands: set[Future] = set()
        self._active_write: Optional[Future] = None
        self._stopping = False
        self._cancel_commands = False

        self.logger = logging.getLogger(f"renac_ha_mqtt.{device_id}")

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
            for timer in self._debounce_timers.values():
                timer.cancel()
            for key, value in self._pending_values.items():
                self.logger.warning("Dropping pending command %s=%r on disconnect", key, value)
            self._debounce_timers.clear()
            self._pending_values.clear()
            self._superseded.clear()
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
        with self._command_lock:
            if self._stopping:
                return
            if reason_code == 0:
                self.logger.info("✅ MQTT connected")
                self._connected = True
                self.client.publish(f"homeassistant/{self.device_id}/availability", "online", retain=True)
                self._register_sensors()
                self._register_actuators()
                for key, value in self.state.copy().items():
                    if value is None:
                        continue
                    entity_type = self.get_entity_type(key) or "sensor"
                    topic = f"homeassistant/{entity_type}/{self.device_id}/{key}/state"
                    self.publish(topic, value, retain=True)
                for key, attributes in self._sensor_attributes.items():
                    self.publish(self._attributes_topic(key), attributes, retain=True)
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

                self.logger.debug("Received command for %s: %r", key, value)
                with self._command_lock:
                    if self._stopping:
                        return
                    if self.command_debounce_s > 0 and (
                        key in self._debounced_actuators or self.get_entity_type(key) in ("number", "select")
                    ):
                        if key in self._pending_values:
                            self._superseded[key] = self._superseded.get(key, 0) + 1
                        self._pending_values[key] = value
                        timer = self._debounce_timers.pop(key, None)
                        if timer is not None:
                            timer.cancel()
                        timer = threading.Timer(self.command_debounce_s, self._flush, (key,))
                        timer.daemon = True
                        self._debounce_timers[key] = timer
                        timer.start()
                        return
                    callback = self._actuator_callbacks[key]
                    command = self._command_worker.submit(self._execute_command, key, value, callback)
                    self._commands.add(command)
                command.add_done_callback(self._command_finished)
            else:
                self.logger.warning(f"⚠️ No callback found for actuator key: {key}")
        else:
            self.logger.debug(f"📩 Unhandled MQTT message: {msg.topic} => {msg.payload}")

    def _flush(self, key: str) -> None:
        with self._command_lock:
            # A cancelled timer may already be waiting for this lock.
            if self._debounce_timers.get(key) is not threading.current_thread():
                return
            self._debounce_timers.pop(key, None)
            if self._stopping:
                return
            command = self._command_worker.submit(self._execute_latest, key)
            self._commands.add(command)
        command.add_done_callback(self._command_finished)

    def _execute_latest(self, key: str) -> None:
        with self._command_lock:
            if self._stopping or key not in self._pending_values:
                return
            value = self._pending_values.pop(key)
            superseded = self._superseded.pop(key, 0)
            timer = self._debounce_timers.pop(key, None)
            if timer is not None:
                timer.cancel()
            callback = self._actuator_callbacks[key]
        self.logger.info("Applying %s=%r (%d superseded)", key, value, superseded)
        self._execute_command(key, value, callback)

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
                if result and not self._cancel_commands:
                    self.logger.info("✅ Command for %s executed", key)
                    self._set_state(key, value)
                    topic = f"homeassistant/{self.get_entity_type(key)}/{self.device_id}/{key}/state"
                    self.client.publish(topic, value, retain=True)
                    return
            reason = "not confirmed (no response)" if result is None else "rejected by device"
        except Exception as exc:
            reason = f"failed: {str(exc) or type(exc).__name__}"
        finally:
            with self._command_lock:
                self._active_write = None
        with self._command_lock:
            if self._cancel_commands:
                reason = "cancelled (shutdown)"
        self.logger.warning("Command %s=%r %s", key, value, reason)
        self._refresh_actuator(key)

    def _refresh_actuator(self, key: str) -> None:
        with self._command_lock:
            if key in self._pending_values or self._stopping:
                return
            refresh = self._actuator_refreshers.get(key)
        if refresh is None:
            return
        result = None
        try:
            result = refresh()
            if isinstance(result, Future):
                future = result
                with self._command_lock:
                    self._active_write = future
                    if self._cancel_commands:
                        future.cancel()
                try:
                    result = future.result(timeout=BLE_WRITE_TIMEOUT_S)
                except Exception:
                    future.cancel()
                    raise
            with self._command_lock:
                if key in self._pending_values or self._stopping:
                    return
                if result is not None:
                    self._set_state(key, result)
                    topic = f"homeassistant/{self.get_entity_type(key)}/{self.device_id}/{key}/state"
                    self.client.publish(topic, result, retain=True)
                    self.logger.info("Republished %s=%r from device after failed command", key, result)
                    return
        except Exception as exc:
            self.logger.debug("Refresh %s failed: %s", key, str(exc) or type(exc).__name__)
        finally:
            with self._command_lock:
                self._active_write = None
        self.logger.warning("Could not refresh %s after failed command", key)

    def _register_sensors(self):
        groups = (
            ("sensor", self.entities.get("sensor", {})),
            ("binary_sensor", self.entities.get("binary_sensor", {})),
        )
        for entity_type, sensors in groups:
            for key, sensor in sensors.items():
                base_topic = f"homeassistant/{entity_type}/{self.device_id}/{key}"
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
                    **sensor,
                }
                if config.pop("has_attributes", False):
                    config["json_attributes_topic"] = self._attributes_topic(key)
                if entity_type == "binary_sensor":
                    config.update(payload_on="ON", payload_off="OFF")
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
        values = key_or_dict if isinstance(key_or_dict, dict) else {key_or_dict: value}
        updates = {}
        with self._command_lock:
            for key, reading in values.items():
                entity_type = "sensor"
                if key in self.entities.get("binary_sensor", {}):
                    entity_type = "binary_sensor"
                    if isinstance(reading, bool):
                        reading = "ON" if reading else "OFF"
                if self._set_state(key, reading):
                    topic = f"homeassistant/{entity_type}/{self.device_id}/{key}/state"
                    self.client.publish(topic, reading, retain=True)
                    updates[key] = reading

        if updates:
            self.logger.debug(f"📤 Published sensor updates: {updates}")

        return bool(updates)

    def _attributes_topic(self, key: str) -> str:
        return f"homeassistant/sensor/{self.device_id}/{key}/attributes"

    def set_sensor_attributes(self, key: str, attributes: dict[str, Any]) -> None:
        with self._command_lock:
            if self._sensor_attributes.get(key) != attributes:
                self._sensor_attributes[key] = attributes.copy()
                self.publish(self._attributes_topic(key), attributes, retain=True)

    def get_entity_type(self, key: str) -> Optional[str]:
        """Return the entity category for ``key`` if present."""
        for entity_type, entity_dict in self.entities.items():
            if key in entity_dict:
                return entity_type
        return None

    def set_actuator_callback(self, key: str, callback: ActuatorCallback,
                              value: Optional[Union[int, float, str, bool]] = None,
                              *, refresh: Optional[ActuatorRefresh] = None, debounce: bool = False):
        with self._command_lock:
            self._actuator_callbacks[key] = callback
            if debounce:
                self._debounced_actuators.add(key)
            else:
                self._debounced_actuators.discard(key)
            if refresh is None:
                self._actuator_refreshers.pop(key, None)
            else:
                self._actuator_refreshers[key] = refresh
            self.logger.debug(f"✅ Callback registered for actuator: {key}")
            if value is not None:
                self._set_state(key, value)
                topic = f"homeassistant/{self.get_entity_type(key)}/{self.device_id}/{key}/state"
                self.client.publish(topic, value, retain=True)
                self.logger.info(f"📤 Initial state published for actuator {key}: {value}")

    def set_actuator_value(self, key: str, value: Optional[ActuatorPayload]) -> bool:
        """Update actuator state and publish MQTT message if it changed."""
        entity_type = self.get_entity_type(key)
        if value is None or entity_type is None:
            return False
        with self._command_lock:
            if self._set_state(key, value):
                topic = f"homeassistant/{entity_type}/{self.device_id}/{key}/state"
                self.client.publish(topic, value, retain=True)
                self.logger.info(f"📤 Published actuator update: {key} = {value}")
                return True
        return False
