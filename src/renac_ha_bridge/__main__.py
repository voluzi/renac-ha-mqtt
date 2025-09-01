"""Entry point for the RENAC Home Assistant bridge."""

import asyncio
import os
import signal
import logging
from typing import Any, Callable, Awaitable, Optional, Dict

from renac_ble import RenacWallboxBLE, RenacInverterBLE
from renac_ha_mqtt import RenacInverterDevice, RenacWallboxDevice

logging.basicConfig(level=logging.INFO)

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER", "renacble")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "renacble")

INVERTER_ADDR = os.getenv("RENAC_INVERTER_ADDR")
WALLBOX_ADDR = os.getenv("RENAC_WALLBOX_ADDR")

shutdown_event = asyncio.Event()

inverter_mqtt: Optional[RenacInverterDevice] = None
wallbox_mqtt: Optional[RenacWallboxDevice] = None

wallbox_excluded_keys = {"sn", "model", "manufacturer", "version", "update_time"}


def wallbox_callback(parsed: Dict[str, Any]) -> None:
    """Forward wallbox telemetry to MQTT."""
    global wallbox_mqtt
    if wallbox_mqtt is None:
        wallbox_mqtt = RenacWallboxDevice(
            "RENAC Wallbox",
            parsed.get("sn"),
            parsed.get("model"),
            MQTT_HOST,
            MQTT_PORT,
            MQTT_USER,
            MQTT_PASSWORD,
        )
        wallbox_mqtt.connect()
    wallbox_mqtt.set_sensor_value({k: v for k, v in parsed.items() if k not in wallbox_excluded_keys})


def wrap_async_callback(loop: asyncio.AbstractEventLoop,
                        coro_func: Callable[[Any], Awaitable[Optional[bool]]]) -> Callable[[Any], Optional[bool]]:
    """Wrap an ``async`` function so it can be used as a synchronous callback."""

    def wrapper(value: Any) -> Optional[bool]:
        future = asyncio.run_coroutine_threadsafe(coro_func(value), loop)
        try:
            return future.result()
        except Exception as exc:  # pragma: no cover - best effort logging
            logging.error("Error executing %s: %s", coro_func.__name__, exc)
            return False

    return wrapper


async def run_bridge(inverter_addr: str, wallbox_addr: str) -> None:
    """Connect to RENAC devices and forward telemetry to MQTT."""

    global inverter_mqtt

    inverter = RenacInverterBLE(inverter_addr)
    wallbox = RenacWallboxBLE(wallbox_addr, on_notification=wallbox_callback)

    while not shutdown_event.is_set():
        try:
            await inverter.connect()
            logging.info("⚡️ Connected to inverter")
            info = await inverter.get_info()
            inverter_mqtt = RenacInverterDevice(
                "RENAC Inverter",
                info.get("sn"),
                info.get("model"),
                MQTT_HOST,
                MQTT_PORT,
                MQTT_USER,
                MQTT_PASSWORD,
            )
            inverter_mqtt.connect()

            loop = asyncio.get_running_loop()
            inverter_mqtt.set_actuator_callback(
                "max_charge_current",
                wrap_async_callback(loop, inverter.set_max_charge_current),
                await inverter.get_max_charge_current(),
            )
            inverter_mqtt.set_actuator_callback(
                "max_discharge_current",
                wrap_async_callback(loop, inverter.set_max_discharge_current),
                await inverter.get_max_discharge_current(),
            )
            inverter_mqtt.set_actuator_callback(
                "min_soc",
                wrap_async_callback(loop, inverter.set_min_soc),
                await inverter.get_min_soc(),
            )
            inverter_mqtt.set_actuator_callback(
                "min_soc_on_grid",
                wrap_async_callback(loop, inverter.set_min_soc_on_grid),
                await inverter.get_min_soc_on_grid(),
            )
            inverter_mqtt.set_actuator_callback(
                "export_limit",
                wrap_async_callback(loop, inverter.set_export_limit),
                await inverter.get_export_limit(),
            )
            inverter_mqtt.set_actuator_callback(
                "power_limit_percent",
                wrap_async_callback(loop, inverter.set_power_limit_percent),
                await inverter.get_power_limit_percent(),
            )

            await wallbox.connect()
            logging.info("⚡️ Connected to wallbox")

            while not shutdown_event.is_set():
                result = await inverter.get_power_and_energy_overview()
                inverter_mqtt.set_sensor_value(result)
                if not wallbox.is_connected():
                    raise ConnectionError("Wallbox is disconnected")
                await asyncio.sleep(5)

        except Exception:  # pragma: no cover - best effort logging
            logging.exception("Error in loop. Retrying in 5s...")
            await asyncio.sleep(5)
        finally:
            await inverter.disconnect()
            await wallbox.disconnect()


def _shutdown_handler() -> None:
    """Signal handler to stop the bridge."""

    shutdown_event.set()


def main() -> None:
    """CLI entry point."""

    inv = INVERTER_ADDR
    wb = WALLBOX_ADDR
    if not inv or not wb:
        raise SystemExit("RENAC_INVERTER_ADDR and RENAC_WALLBOX_ADDR must be set")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown_handler)
        except NotImplementedError:
            pass  # Windows
    loop.run_until_complete(run_bridge(inv, wb))
