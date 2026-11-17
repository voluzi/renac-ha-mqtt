"""Entry point for the RENAC Home Assistant bridge (multi-device)."""

import asyncio
import os
import signal
import logging
import time
from concurrent.futures import Future
from typing import Any, Callable, Awaitable, Optional, Dict, Iterable

from renac_ble import RenacWallboxBLE, RenacInverterBLE, WorkMode, GridChargePeriod, ChargingMode
from renac_ha_mqtt import RenacInverterDevice, RenacWallboxDevice
from renac_ha_mqtt.mqtt_device import BLE_WRITE_TIMEOUT_S

# --------------------------------------------------------------------------- #
# Config & logging
# --------------------------------------------------------------------------- #

logger = logging.getLogger("renac_ha_bridge")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for name in ("renac_ble", "renac_ha_mqtt", "renac_ha_bridge"):
        logging.getLogger(name).setLevel(os.getenv("LOG_LEVEL", "INFO"))


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
BLE_RECONNECT_DELAY_S = 5.0

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
                        ) -> Callable[[Any], Future[Optional[bool]]]:
    """Schedule a cancellable async setter with a BLE write timeout."""
    def wrapper(value: Any) -> Future[Optional[bool]]:
        async def execute() -> Optional[bool]:
            try:
                return await asyncio.wait_for(coro_func(value), timeout=BLE_WRITE_TIMEOUT_S)
            except Exception as exc:
                logger.warning("Error executing %s: %s",
                                getattr(coro_func, "__name__", coro_func), str(exc) or type(exc).__name__)
                return False

        return asyncio.run_coroutine_threadsafe(execute(), loop)
    return wrapper


def wrap_async_refresh(loop: asyncio.AbstractEventLoop,
                       coro_func: Callable[[], Awaitable[Any]]) -> Callable[[], Future]:
    """Schedule a fresh device read with a BLE timeout."""
    def wrapper() -> Future:
        return asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(coro_func(), timeout=BLE_WRITE_TIMEOUT_S), loop,
        )
    return wrapper


# --------------------------------------------------------------------------- #
# Wallbox pipeline (one task per device)
# --------------------------------------------------------------------------- #

def make_wallbox_callback(ble_addr: str) -> Callable[[Dict[str, Any]], None]:
    """Create a per-wallbox callback that forwards telemetry to its MQTT device."""
    def _callback(parsed: Dict[str, Any]) -> None:
        if shutdown_event.is_set():
            return
        dev = wallbox_mqtt_by_addr.get(ble_addr)
        if dev is None:
            # Build device on first telemetry (we need serial/model)
            dev = RenacWallboxDevice(
                device_name=f"RENAC Wallbox",
                serial_number=parsed.get("sn"),
                ble_address=ble_addr,
                model=parsed.get("model"),
                mqtt_host=MQTT_HOST,
                mqtt_port=MQTT_PORT,
                mqtt_user=MQTT_USER,
                mqtt_password=MQTT_PASSWORD,
            )
            dev.connect()
            wallbox_mqtt_by_addr[ble_addr] = dev
            logger.info("🔌 MQTT device created for wallbox %s (sn=%s model=%s)",
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

    def settings_refresh(key: str) -> Callable[[], Awaitable[Any]]:
        async def refresh() -> Any:
            return _wallbox_actuator_state(await wallbox.get_basic_settings()).get(key)
        return refresh

    for key, setter in setters.items():
        dev.set_actuator_callback(
            key, wrap_async_callback(loop, setter), state.get(key),
            refresh=wrap_async_refresh(loop, settings_refresh(key)) if key != "charging" else None,
        )


async def run_wallbox_task(ble_addr: str) -> None:
    """Loop to keep a wallbox connected and forwarding notifications."""
    publish_status = make_wallbox_callback(ble_addr)
    wallbox = RenacWallboxBLE(ble_addr, on_notification=publish_status)
    wired_dev: Optional[RenacWallboxDevice] = None

    while not shutdown_event.is_set():
        try:
            await wallbox.connect()
            logger.info("⚡️ Connected to wallbox %s", ble_addr)
            last_actuator_poll = 0.0
            last_status_poll = 0.0

            # The wallbox pushes its status only about once a minute, so it is
            # also polled; settings are polled on the actuator interval.
            while True:
                if shutdown_event.is_set():
                    await asyncio.Future()
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

        except ConnectionError as exc:
            logger.warning("Wallbox %s link lost: %s; reconnecting in %ss",
                           ble_addr, exc, BLE_RECONNECT_DELAY_S)
            await asyncio.sleep(BLE_RECONNECT_DELAY_S)
        except Exception:
            logger.exception("Wallbox loop error (%s). Reconnecting in %ss...", ble_addr, BLE_RECONNECT_DELAY_S)
            await asyncio.sleep(BLE_RECONNECT_DELAY_S)
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


def _period_setter(get_period, set_period, field: str) -> Callable[[Any], Awaitable[bool]]:
    async def setter(value: Any) -> bool:
        period = await get_period()
        if period is None:
            return False
        setattr(period, field, value == "ON" if field == "enabled" else int(value))
        return await set_period(period)
    return setter


async def run_inverter_task(ble_addr: str) -> None:
    """Loop to keep an inverter connected, publish telemetry and wire actuators."""
    inverter = RenacInverterBLE(ble_addr)

    while not shutdown_event.is_set():
        try:
            await inverter.connect()
            logger.info("⚡️ Connected to inverter %s", ble_addr)

            mqtt_dev = inverter_mqtt_by_addr.get(ble_addr)
            if mqtt_dev is None:
                info = await inverter.get_info()
                if info is None:
                    raise ConnectionError("could not read inverter info")
                if shutdown_event.is_set():
                    return
                mqtt_dev = RenacInverterDevice(
                    device_name="RENAC Inverter",
                    serial_number=info.get("sn"),
                    ble_address=ble_addr,
                    model=info.get("model"),
                    mqtt_host=MQTT_HOST,
                    mqtt_port=MQTT_PORT,
                    mqtt_user=MQTT_USER,
                    mqtt_password=MQTT_PASSWORD,
                )
                mqtt_dev.connect()
                inverter_mqtt_by_addr[ble_addr] = mqtt_dev

            loop = asyncio.get_running_loop()

            def wire(key, setter, refresh, initial):
                mqtt_dev.set_actuator_callback(
                    key, wrap_async_callback(loop, setter), initial,
                    refresh=wrap_async_refresh(loop, refresh),
                )

            for key in ("max_charge_current", "max_discharge_current", "min_soc",
                        "min_soc_on_grid", "export_limit", "power_limit_percent"):
                get_value = getattr(inverter, f"get_{key}")
                wire(key, getattr(inverter, f"set_{key}"), get_value, await get_value())

            async def _set_work_mode(value: str) -> bool:
                try:
                    mode = WorkMode[value.upper()]
                except KeyError:
                    return False
                return await inverter.set_work_mode(mode)

            async def _get_work_mode() -> Optional[str]:
                mode = await inverter.get_work_mode()
                return mode.name.lower() if mode is not None else None

            wire("work_mode", _set_work_mode, _get_work_mode, await _get_work_mode())

            def period_refresh(get_period, field: str) -> Callable[[], Awaitable[Any]]:
                async def refresh() -> Any:
                    return _period_to_mqtt_state(await get_period()).get(field)
                return refresh

            for prefix, get_period, set_period in (
                ("force_time_p1", inverter.get_force_time_period1, inverter.set_force_time_period1),
                ("force_time_p2", inverter.get_force_time_period2, inverter.set_force_time_period2),
                ("backup_charge", inverter.get_backup_grid_charge, inverter.set_backup_grid_charge),
            ):
                initial = _period_to_mqtt_state(await get_period())
                for field in ("enabled", "start_hour", "start_minute", "end_hour", "end_minute"):
                    wire(f"{prefix}_{field}", _period_setter(get_period, set_period, field),
                         period_refresh(get_period, field), initial.get(field))

            last_actuator_poll = time.monotonic()

            # Poll & publish inverter overview periodically
            while True:
                if shutdown_event.is_set():
                    await asyncio.Future()
                overview = await inverter.get_power_and_energy_overview()
                if overview:
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

        except ConnectionError as exc:
            logger.warning("Inverter %s link lost: %s; reconnecting in %ss",
                           ble_addr, exc, BLE_RECONNECT_DELAY_S)
            await asyncio.sleep(BLE_RECONNECT_DELAY_S)
        except Exception:
            logger.exception("Inverter loop error (%s). Reconnecting in %ss...", ble_addr, BLE_RECONNECT_DELAY_S)
            await asyncio.sleep(BLE_RECONNECT_DELAY_S)
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

    try:
        await shutdown_event.wait()
    finally:
        devices = [*inverter_mqtt_by_addr.values(), *wallbox_mqtt_by_addr.values()]
        await asyncio.gather(*(asyncio.to_thread(dev.disconnect) for dev in devices), return_exceptions=True)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    configure_logging()
    inverter_addrs = _resolve_inverter_addrs()
    wallbox_addrs = _resolve_wallbox_addrs()

    logger.info("Starting RENAC bridge | inverters=%s | wallboxes=%s",
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
