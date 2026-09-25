"""Entry point for the RENAC Home Assistant bridge (multi-device)."""

import asyncio
import os
import signal
import logging
import time
from typing import Any, Callable, Awaitable, Optional, Dict, Iterable

from renac_ble import RenacWallboxBLE, RenacInverterBLE, WorkMode, GridChargePeriod, ChargingMode
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
# Wallbox status is read on demand this often, on top of its own pushes
WALLBOX_STATUS_INTERVAL_S = float(os.getenv("RENAC_WALLBOX_STATUS_INTERVAL_S", "10"))
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
        if parsed.get("state"):
            dev.set_actuator_value("charging", "ON" if parsed["state"] == "charging" else "OFF")
    return _callback


def _wallbox_actuator_state(settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert wallbox basic settings to MQTT actuator values."""
    if settings is None:
        return {}
    start_h, start_m = (int(p) for p in settings["allow_charging_begin"].split(":"))
    end_h, end_m = (int(p) for p in settings["allow_charging_end"].split(":"))
    return {
        "max_output_current": settings["max_output_current"],
        "charging_mode": ChargingMode(settings["charging_mode"]).name.lower(),
        "allowed_start_hour": start_h,
        "allowed_start_minute": start_m,
        "allowed_end_hour": end_h,
        "allowed_end_minute": end_m,
    }


async def _wire_wallbox_actuators(dev: RenacWallboxDevice, wallbox: RenacWallboxBLE) -> None:
    """Register wallbox setters on the MQTT device with their current values."""
    loop = asyncio.get_running_loop()
    state = _wallbox_actuator_state(await wallbox.get_basic_settings())

    async def _set_charging_mode(value: str) -> bool:
        try:
            mode = ChargingMode[str(value).upper()]
        except KeyError:
            return False
        return await wallbox.set_charging_mode(mode)

    # Each window field is its own entity, so two quick edits must not race
    # on the same read-modify-write of the whole window.
    window_lock = asyncio.Lock()

    def _window_setter(field: str) -> Callable[[Any], Awaitable[bool]]:
        async def _set(value: Any) -> bool:
            async with window_lock:
                window = await wallbox.get_allowed_charging_time()
                if window is None:
                    return False
                setattr(window, field, int(value))
                return await wallbox.set_allowed_charging_time(window)
        return _set

    async def _set_charging(value: str) -> bool:
        if value == "ON":
            return await wallbox.start_charging()
        if value == "OFF":
            return await wallbox.stop_charging()
        return False

    setters: Dict[str, Callable[[Any], Awaitable[Optional[bool]]]] = {
        "charging": _set_charging,
        "max_output_current": wallbox.set_max_output_current,
        "charging_mode": _set_charging_mode,
        "allowed_start_hour": _window_setter("start_hour"),
        "allowed_start_minute": _window_setter("start_minute"),
        "allowed_end_hour": _window_setter("end_hour"),
        "allowed_end_minute": _window_setter("end_minute"),
    }
    for key, setter in setters.items():
        dev.set_actuator_callback(key, wrap_async_callback(loop, setter), state.get(key))


async def run_wallbox_task(ble_addr: str) -> None:
    """Loop to keep a wallbox connected and forwarding notifications."""
    publish_status = make_wallbox_callback(ble_addr)
    wallbox = RenacWallboxBLE(ble_addr, on_notification=publish_status)
    wired_dev: Optional[RenacWallboxDevice] = None

    while not shutdown_event.is_set():
        try:
            await wallbox.connect()
            logging.info("⚡️ Connected to wallbox %s", ble_addr)
            last_actuator_poll = 0.0
            last_status_poll = 0.0

            # The wallbox pushes its status only about once a minute, so it is
            # also polled; settings are polled on the actuator interval.
            while not shutdown_event.is_set():
                if not wallbox.is_connected():
                    raise ConnectionError(f"Wallbox {ble_addr} disconnected")

                if time.monotonic() - last_status_poll >= WALLBOX_STATUS_INTERVAL_S:
                    last_status_poll = time.monotonic()
                    status = await wallbox.get_status()
                    if status is not None:
                        publish_status(status)

                # The MQTT device is created from the first status, which
                # carries the serial number.
                dev = wallbox_mqtt_by_addr.get(ble_addr)
                if dev is not None and dev is not wired_dev:
                    await _wire_wallbox_actuators(dev, wallbox)
                    wired_dev = dev
                    last_actuator_poll = time.monotonic()
                elif dev is not None and time.monotonic() - last_actuator_poll >= ACTUATOR_POLL_INTERVAL_S:
                    last_actuator_poll = time.monotonic()
                    for key, value in _wallbox_actuator_state(await wallbox.get_basic_settings()).items():
                        dev.set_actuator_value(key, value)

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

def _period_to_mqtt_state(period: Optional[GridChargePeriod]) -> Dict[str, Any]:
    """Convert a GridChargePeriod to a dict of MQTT-friendly values."""
    if period is None:
        return {}
    return {
        "enabled": "ON" if period.enabled else "OFF",
        "start_hour": period.start_hour,
        "start_minute": period.start_minute,
        "end_hour": period.end_hour,
        "end_minute": period.end_minute,
    }


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

            # ----------------------------------------------------------------- #
            # Force Time Period 1 callbacks
            # ----------------------------------------------------------------- #
            async def _set_force_time_p1_enabled(value: str) -> bool:
                period = await inverter.get_force_time_period1()
                if period is None:
                    period = GridChargePeriod(False, 0, 0, 0, 0)
                period.enabled = value == "ON"
                return await inverter.set_force_time_period1(period)

            async def _set_force_time_p1_start_hour(value: int) -> bool:
                period = await inverter.get_force_time_period1()
                if period is None:
                    return False
                period.start_hour = int(value)
                return await inverter.set_force_time_period1(period)

            async def _set_force_time_p1_start_minute(value: int) -> bool:
                period = await inverter.get_force_time_period1()
                if period is None:
                    return False
                period.start_minute = int(value)
                return await inverter.set_force_time_period1(period)

            async def _set_force_time_p1_end_hour(value: int) -> bool:
                period = await inverter.get_force_time_period1()
                if period is None:
                    return False
                period.end_hour = int(value)
                return await inverter.set_force_time_period1(period)

            async def _set_force_time_p1_end_minute(value: int) -> bool:
                period = await inverter.get_force_time_period1()
                if period is None:
                    return False
                period.end_minute = int(value)
                return await inverter.set_force_time_period1(period)

            p1 = _period_to_mqtt_state(await inverter.get_force_time_period1())
            mqtt_dev.set_actuator_callback(
                "force_time_p1_enabled",
                wrap_async_callback(loop, _set_force_time_p1_enabled),
                p1.get("enabled"),
            )
            mqtt_dev.set_actuator_callback(
                "force_time_p1_start_hour",
                wrap_async_callback(loop, _set_force_time_p1_start_hour),
                p1.get("start_hour"),
            )
            mqtt_dev.set_actuator_callback(
                "force_time_p1_start_minute",
                wrap_async_callback(loop, _set_force_time_p1_start_minute),
                p1.get("start_minute"),
            )
            mqtt_dev.set_actuator_callback(
                "force_time_p1_end_hour",
                wrap_async_callback(loop, _set_force_time_p1_end_hour),
                p1.get("end_hour"),
            )
            mqtt_dev.set_actuator_callback(
                "force_time_p1_end_minute",
                wrap_async_callback(loop, _set_force_time_p1_end_minute),
                p1.get("end_minute"),
            )

            # ----------------------------------------------------------------- #
            # Force Time Period 2 callbacks
            # ----------------------------------------------------------------- #
            async def _set_force_time_p2_enabled(value: str) -> bool:
                period = await inverter.get_force_time_period2()
                if period is None:
                    period = GridChargePeriod(False, 0, 0, 0, 0)
                period.enabled = value == "ON"
                return await inverter.set_force_time_period2(period)

            async def _set_force_time_p2_start_hour(value: int) -> bool:
                period = await inverter.get_force_time_period2()
                if period is None:
                    return False
                period.start_hour = int(value)
                return await inverter.set_force_time_period2(period)

            async def _set_force_time_p2_start_minute(value: int) -> bool:
                period = await inverter.get_force_time_period2()
                if period is None:
                    return False
                period.start_minute = int(value)
                return await inverter.set_force_time_period2(period)

            async def _set_force_time_p2_end_hour(value: int) -> bool:
                period = await inverter.get_force_time_period2()
                if period is None:
                    return False
                period.end_hour = int(value)
                return await inverter.set_force_time_period2(period)

            async def _set_force_time_p2_end_minute(value: int) -> bool:
                period = await inverter.get_force_time_period2()
                if period is None:
                    return False
                period.end_minute = int(value)
                return await inverter.set_force_time_period2(period)

            p2 = _period_to_mqtt_state(await inverter.get_force_time_period2())
            mqtt_dev.set_actuator_callback(
                "force_time_p2_enabled",
                wrap_async_callback(loop, _set_force_time_p2_enabled),
                p2.get("enabled"),
            )
            mqtt_dev.set_actuator_callback(
                "force_time_p2_start_hour",
                wrap_async_callback(loop, _set_force_time_p2_start_hour),
                p2.get("start_hour"),
            )
            mqtt_dev.set_actuator_callback(
                "force_time_p2_start_minute",
                wrap_async_callback(loop, _set_force_time_p2_start_minute),
                p2.get("start_minute"),
            )
            mqtt_dev.set_actuator_callback(
                "force_time_p2_end_hour",
                wrap_async_callback(loop, _set_force_time_p2_end_hour),
                p2.get("end_hour"),
            )
            mqtt_dev.set_actuator_callback(
                "force_time_p2_end_minute",
                wrap_async_callback(loop, _set_force_time_p2_end_minute),
                p2.get("end_minute"),
            )

            # ----------------------------------------------------------------- #
            # Backup Mode Grid Charge callbacks
            # ----------------------------------------------------------------- #
            async def _set_backup_charge_enabled(value: str) -> bool:
                period = await inverter.get_backup_grid_charge()
                if period is None:
                    period = GridChargePeriod(False, 0, 0, 0, 0)
                period.enabled = value == "ON"
                return await inverter.set_backup_grid_charge(period)

            async def _set_backup_charge_start_hour(value: int) -> bool:
                period = await inverter.get_backup_grid_charge()
                if period is None:
                    return False
                period.start_hour = int(value)
                return await inverter.set_backup_grid_charge(period)

            async def _set_backup_charge_start_minute(value: int) -> bool:
                period = await inverter.get_backup_grid_charge()
                if period is None:
                    return False
                period.start_minute = int(value)
                return await inverter.set_backup_grid_charge(period)

            async def _set_backup_charge_end_hour(value: int) -> bool:
                period = await inverter.get_backup_grid_charge()
                if period is None:
                    return False
                period.end_hour = int(value)
                return await inverter.set_backup_grid_charge(period)

            async def _set_backup_charge_end_minute(value: int) -> bool:
                period = await inverter.get_backup_grid_charge()
                if period is None:
                    return False
                period.end_minute = int(value)
                return await inverter.set_backup_grid_charge(period)

            backup = _period_to_mqtt_state(await inverter.get_backup_grid_charge())
            mqtt_dev.set_actuator_callback(
                "backup_charge_enabled",
                wrap_async_callback(loop, _set_backup_charge_enabled),
                backup.get("enabled"),
            )
            mqtt_dev.set_actuator_callback(
                "backup_charge_start_hour",
                wrap_async_callback(loop, _set_backup_charge_start_hour),
                backup.get("start_hour"),
            )
            mqtt_dev.set_actuator_callback(
                "backup_charge_start_minute",
                wrap_async_callback(loop, _set_backup_charge_start_minute),
                backup.get("start_minute"),
            )
            mqtt_dev.set_actuator_callback(
                "backup_charge_end_hour",
                wrap_async_callback(loop, _set_backup_charge_end_hour),
                backup.get("end_hour"),
            )
            mqtt_dev.set_actuator_callback(
                "backup_charge_end_minute",
                wrap_async_callback(loop, _set_backup_charge_end_minute),
                backup.get("end_minute"),
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

                    # Poll grid charge period settings
                    p1 = _period_to_mqtt_state(await inverter.get_force_time_period1())
                    mqtt_dev.set_actuator_value("force_time_p1_enabled", p1.get("enabled"))
                    mqtt_dev.set_actuator_value("force_time_p1_start_hour", p1.get("start_hour"))
                    mqtt_dev.set_actuator_value("force_time_p1_start_minute", p1.get("start_minute"))
                    mqtt_dev.set_actuator_value("force_time_p1_end_hour", p1.get("end_hour"))
                    mqtt_dev.set_actuator_value("force_time_p1_end_minute", p1.get("end_minute"))

                    p2 = _period_to_mqtt_state(await inverter.get_force_time_period2())
                    mqtt_dev.set_actuator_value("force_time_p2_enabled", p2.get("enabled"))
                    mqtt_dev.set_actuator_value("force_time_p2_start_hour", p2.get("start_hour"))
                    mqtt_dev.set_actuator_value("force_time_p2_start_minute", p2.get("start_minute"))
                    mqtt_dev.set_actuator_value("force_time_p2_end_hour", p2.get("end_hour"))
                    mqtt_dev.set_actuator_value("force_time_p2_end_minute", p2.get("end_minute"))

                    backup = _period_to_mqtt_state(await inverter.get_backup_grid_charge())
                    mqtt_dev.set_actuator_value("backup_charge_enabled", backup.get("enabled"))
                    mqtt_dev.set_actuator_value("backup_charge_start_hour", backup.get("start_hour"))
                    mqtt_dev.set_actuator_value("backup_charge_start_minute", backup.get("start_minute"))
                    mqtt_dev.set_actuator_value("backup_charge_end_hour", backup.get("end_hour"))
                    mqtt_dev.set_actuator_value("backup_charge_end_minute", backup.get("end_minute"))

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