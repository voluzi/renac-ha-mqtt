"""Entry point for the RENAC Home Assistant bridge (multi-device)."""

import asyncio
import os
import signal
import logging
import time
from typing import Any, Callable, Awaitable, Optional, Dict, Iterable

from renac_ble import RenacWallboxBLE, RenacInverterBLE, WorkMode
from renac_ha_mqtt import RenacInverterDevice, RenacWallboxDevice

# --------------------------------------------------------------------------- #
# Config & logging
# --------------------------------------------------------------------------- #

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER", "renacble")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "renacble")

# Backward-compatible singles:
INVERTER_ADDR_LEGACY = os.getenv("RENAC_INVERTER_ADDR")
WALLBOX_ADDR_LEGACY = os.getenv("RENAC_WALLBOX_ADDR")

# New multi-device envs (comma/space separated)
INVERTER_ADDRS = os.getenv("RENAC_INVERTER_ADDRS", "")
WALLBOX_ADDRS = os.getenv("RENAC_WALLBOX_ADDRS", "")

POLL_INTERVAL_S = float(os.getenv("RENAC_POLL_INTERVAL_S", "5"))
# Separate interval for refreshing actuator states
ACTUATOR_POLL_INTERVAL_S = float(os.getenv("RENAC_ACTUATOR_POLL_INTERVAL_S", "30"))

shutdown_event = asyncio.Event()

# Keep MQTT device objects per BLE address
inverter_mqtt_by_addr: Dict[str, RenacInverterDevice] = {}
wallbox_mqtt_by_addr: Dict[str, RenacWallboxDevice] = {}

# filter some fields out of wallbox telemetry
WALLBOX_EXCLUDED_KEYS = {"sn", "model", "manufacturer", "version", "update_time"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _split_addrs(value: str) -> list[str]:
    """Split comma/space-separated address list, trim, dedupe, preserve order."""
    if not value:
        return []
    raw = [p.strip() for p in value.replace(",", " ").split()]
    seen = set()
    out: list[str] = []
    for a in raw:
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _resolve_inverter_addrs() -> list[str]:
    addrs = _split_addrs(INVERTER_ADDRS)
    if INVERTER_ADDR_LEGACY:
        addrs = [INVERTER_ADDR_LEGACY] + [a for a in addrs if a != INVERTER_ADDR_LEGACY]
    return addrs


def _resolve_wallbox_addrs() -> list[str]:
    addrs = _split_addrs(WALLBOX_ADDRS)
    if WALLBOX_ADDR_LEGACY:
        addrs = [WALLBOX_ADDR_LEGACY] + [a for a in addrs if a != WALLBOX_ADDR_LEGACY]
    return addrs


def wrap_async_callback(loop: asyncio.AbstractEventLoop,
                        coro_func: Callable[[Any], Awaitable[Optional[bool]]]
                        ) -> Callable[[Any], Optional[bool]]:
    """Wrap an async setter so it can be called by sync actuator callbacks."""
    def wrapper(value: Any) -> Optional[bool]:
        fut = asyncio.run_coroutine_threadsafe(coro_func(value), loop)
        try:
            return fut.result()
        except Exception as exc:  # pragma: no cover - best-effort logging
            logging.error("Error executing %s: %s", getattr(coro_func, "__name__", coro_func), exc)
            return False
    return wrapper


# --------------------------------------------------------------------------- #
# Wallbox pipeline (one task per device)
# --------------------------------------------------------------------------- #

def make_wallbox_callback(ble_addr: str) -> Callable[[Dict[str, Any]], None]:
    """Create a per-wallbox callback that forwards telemetry to its MQTT device."""
    def _callback(parsed: Dict[str, Any]) -> None:
        dev = wallbox_mqtt_by_addr.get(ble_addr)
        if dev is None:
            # Build device on first telemetry (we need serial/model)
            dev = RenacWallboxDevice(
                device_name=f"RENAC Wallbox",
                serial_number=parsed.get("sn"),
                model=parsed.get("model"),
                mqtt_host=MQTT_HOST,
                mqtt_port=MQTT_PORT,
                mqtt_user=MQTT_USER,
                mqtt_password=MQTT_PASSWORD,
            )
            dev.connect()
            wallbox_mqtt_by_addr[ble_addr] = dev
            logging.info("🔌 MQTT device created for wallbox %s (sn=%s model=%s)",
                         ble_addr, parsed.get("sn"), parsed.get("model"))
        dev.set_sensor_value({k: v for k, v in parsed.items() if k not in WALLBOX_EXCLUDED_KEYS})
    return _callback


async def run_wallbox_task(ble_addr: str) -> None:
    """Loop to keep a wallbox connected and forwarding notifications."""
    wallbox = RenacWallboxBLE(ble_addr, on_notification=make_wallbox_callback(ble_addr))

    while not shutdown_event.is_set():
        try:
            await wallbox.connect()
            logging.info("⚡️ Connected to wallbox %s", ble_addr)

            # Keep connection alive; all data flows via notifications
            while not shutdown_event.is_set():
                if not wallbox.is_connected():
                    raise ConnectionError(f"Wallbox {ble_addr} disconnected")
                await asyncio.sleep(POLL_INTERVAL_S)

        except Exception:
            logging.exception("Wallbox loop error (%s). Reconnecting in 5s...", ble_addr)
            await asyncio.sleep(5)
        finally:
            try:
                await wallbox.disconnect()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Inverter pipeline (one task per device)
# --------------------------------------------------------------------------- #

async def run_inverter_task(ble_addr: str) -> None:
    """Loop to keep an inverter connected, publish telemetry and wire actuators."""
    inverter = RenacInverterBLE(ble_addr)
    mqtt_dev: Optional[RenacInverterDevice] = None

    while not shutdown_event.is_set():
        try:
            await inverter.connect()
            logging.info("⚡️ Connected to inverter %s", ble_addr)

            info = await inverter.get_info()
            mqtt_dev = RenacInverterDevice(
                device_name=f"RENAC Inverter",
                serial_number=info.get("sn"),
                model=info.get("model"),
                mqtt_host=MQTT_HOST,
                mqtt_port=MQTT_PORT,
                mqtt_user=MQTT_USER,
                mqtt_password=MQTT_PASSWORD,
            )
            mqtt_dev.connect()
            inverter_mqtt_by_addr[ble_addr] = mqtt_dev

            loop = asyncio.get_running_loop()
            mqtt_dev.set_actuator_callback(
                "max_charge_current",
                wrap_async_callback(loop, inverter.set_max_charge_current),
                await inverter.get_max_charge_current(),
            )
            mqtt_dev.set_actuator_callback(
                "max_discharge_current",
                wrap_async_callback(loop, inverter.set_max_discharge_current),
                await inverter.get_max_discharge_current(),
            )
            mqtt_dev.set_actuator_callback(
                "min_soc",
                wrap_async_callback(loop, inverter.set_min_soc),
                await inverter.get_min_soc(),
            )
            mqtt_dev.set_actuator_callback(
                "min_soc_on_grid",
                wrap_async_callback(loop, inverter.set_min_soc_on_grid),
                await inverter.get_min_soc_on_grid(),
            )
            mqtt_dev.set_actuator_callback(
                "export_limit",
                wrap_async_callback(loop, inverter.set_export_limit),
                await inverter.get_export_limit(),
            )
            mqtt_dev.set_actuator_callback(
                "power_limit_percent",
                wrap_async_callback(loop, inverter.set_power_limit_percent),
                await inverter.get_power_limit_percent(),
            )
            async def _set_work_mode(value: str) -> bool:
                try:
                    mode = WorkMode[value.upper()]
                except KeyError:
                    return False
                return await inverter.set_work_mode(mode)

            current_mode = await inverter.get_work_mode()
            mqtt_dev.set_actuator_callback(
                "work_mode",
                wrap_async_callback(loop, _set_work_mode),
                current_mode.name.lower() if current_mode is not None else None,
            )

            last_actuator_poll = time.monotonic()

            # Poll & publish inverter overview periodically
            while not shutdown_event.is_set():
                overview = await inverter.get_power_and_energy_overview()
                mqtt_dev.set_sensor_value(overview)

                now = time.monotonic()
                if now - last_actuator_poll >= ACTUATOR_POLL_INTERVAL_S:
                    last_actuator_poll = now
                    mqtt_dev.set_actuator_value(
                        "max_charge_current",
                        await inverter.get_max_charge_current(),
                    )
                    mqtt_dev.set_actuator_value(
                        "max_discharge_current",
                        await inverter.get_max_discharge_current(),
                    )
                    mqtt_dev.set_actuator_value(
                        "min_soc",
                        await inverter.get_min_soc(),
                    )
                    mqtt_dev.set_actuator_value(
                        "min_soc_on_grid",
                        await inverter.get_min_soc_on_grid(),
                    )
                    mqtt_dev.set_actuator_value(
                        "export_limit",
                        await inverter.get_export_limit(),
                    )
                    mqtt_dev.set_actuator_value(
                        "power_limit_percent",
                        await inverter.get_power_limit_percent(),
                    )
                    work_mode = await inverter.get_work_mode()
                    mqtt_dev.set_actuator_value(
                        "work_mode",
                        work_mode.name.lower() if work_mode is not None else None,
                    )

                if not inverter.is_connected():
                    raise ConnectionError(f"Inverter {ble_addr} disconnected")
                await asyncio.sleep(POLL_INTERVAL_S)

        except Exception:
            logging.exception("Inverter loop error (%s). Reconnecting in 5s...", ble_addr)
            await asyncio.sleep(5)
        finally:
            try:
                await inverter.disconnect()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def _shutdown_handler() -> None:
    shutdown_event.set()


async def _run_all(inverter_addrs: Iterable[str], wallbox_addrs: Iterable[str]) -> None:
    tasks: list[asyncio.Task] = []

    for addr in inverter_addrs:
        tasks.append(asyncio.create_task(run_inverter_task(addr), name=f"inverter:{addr}"))
    for addr in wallbox_addrs:
        tasks.append(asyncio.create_task(run_wallbox_task(addr), name=f"wallbox:{addr}"))

    if not tasks:
        raise SystemExit(
            "No devices configured. Set RENAC_INVERTER_ADDR(S) and/or RENAC_WALLBOX_ADDR(S)."
        )

    # Wait until shutdown, then cancel all tasks
    try:
        await shutdown_event.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    inverter_addrs = _resolve_inverter_addrs()
    wallbox_addrs = _resolve_wallbox_addrs()

    logging.info("Starting RENAC bridge | inverters=%s | wallboxes=%s",
                 inverter_addrs or "[]", wallbox_addrs or "[]")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown_handler)
        except NotImplementedError:
            pass  # Windows

    loop.run_until_complete(_run_all(inverter_addrs, wallbox_addrs))

if __name__ == "__main__":
    main()