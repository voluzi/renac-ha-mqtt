# renac-ha-mqtt

**Home Assistant MQTT integration library for RENAC devices.**

This repository contains two related Python packages:

- **`renac_ha_mqtt`** – a pure library that models RENAC inverters and wallboxes as MQTT-discovery–compatible Home Assistant devices.  
  It has **no dependency on BLE** – you can use it with any backend (BLE, cloud, serial, etc.) that provides RENAC telemetry/control.
- **`renac_ha_bridge`** – a bridge package that connects to RENAC devices over BLE (via [renac-ble](https://github.com/voluzi/renac-ble)) and exposes them to Home Assistant using `renac_ha_mqtt`.

---

## ✨ Features
- Models RENAC devices as Home Assistant MQTT entities
- Publishes telemetry as sensors
- Exposes control actuators (charge/discharge limits, Min SoC, export limits, work mode, …)
- Fully compatible with **Home Assistant MQTT Discovery**
- Works with **any RENAC backend** (BLE, cloud, or others)
- Includes a ready-to-use **bridge CLI** (`renac-ble-ha-bridge`) for BLE ↔ MQTT

---

## ✅ Tested devices

| Device | Type | Connection |
| --- | --- | --- |
| RENAC N3-HV-10.0 | Three-phase high-voltage hybrid inverter | BLE |
| RENAC EV-AC3P-22K | Three-phase 22 kW EV wallbox | BLE |

Other RENAC models may work if they expose BLE with the same Modbus register map; single-phase and low-voltage models (e.g. the N1 series) are untested. Reports for other devices are welcome in the issues.

---

## 📦 Installation

```bash
pip install "git+https://github.com/voluzi/renac-ha-mqtt.git@main"
```

This installs both `renac_ha_mqtt` and `renac_ha_bridge`, along with the `renac-ble-ha-bridge` CLI.

---

## 🚀 Usage

### As a library
Use `renac_ha_mqtt` directly if you have your own data source:

```python
from renac_ha_mqtt import RenacInverterDevice, RenacWallboxDevice

inverter = RenacInverterDevice(
    name="RENAC Inverter",
    serial="INV123456",
    model="InverterX",
    mqtt_host="192.168.1.10",
    mqtt_port=1883,
    mqtt_user="renac",
    mqtt_password="renac",
)

inverter.connect()

# Push telemetry
inverter.set_sensor_value({
    "power_w": 3200,
    "soc": 85,
})

# Expose an actuator callback
def handle_export_limit(value: int):
    print("User requested export limit:", value)

inverter.set_actuator_callback("export_limit", handle_export_limit, initial_value=5000)
```

### As a BLE ↔ MQTT bridge
Use the CLI provided by `renac_ha_bridge`:

```bash
RENAC_INVERTER_ADDR="28:9C:6E:92:7C:F6" \
RENAC_WALLBOX_ADDR="E8:FD:F8:D4:A1:75" \
MQTT_HOST="192.168.1.10" \
MQTT_USER="renacble" \
MQTT_PASSWORD="renacble" \
renac-ble-ha-bridge
```

This will connect to both the inverter and the wallbox via BLE and publish their telemetry/control entities into Home Assistant.

MQTT client IDs include the device serial number (or BLE address when missing), hostname, and process ID. Set `MQTT_CLIENT_ID_PREFIX` to override the hostname portion for a bridge instance; Home Assistant topics and entity IDs are independent of this setting.

Set `LOG_LEVEL` to control bridge and RENAC library logging (default: `INFO`).
Use `LOG_LEVEL=DEBUG` to include BLE request and response hex dumps; third-party
BLE and MQTT loggers stay at INFO.

Number, select and bypass relay commands wait for a quiet interval before
applying the latest value per entity. Set `RENAC_COMMAND_DEBOUNCE_S` to change
this interval (default: `1.0` seconds), or `0` to apply every command immediately.
Other switches apply immediately. If a write fails, the bridge reads and
republishes the current device value when available.

### N3 HV inverter entities

The bridge exposes the inverter operating state, charge and discharge
restrictions, BMS and meter connection status, battery and BMS readings,
and per-phase inverter AC and EPS telemetry. The BMS cell index and basic
status entities are disabled by default.

Inverter state and live measurements use `RENAC_POLL_INTERVAL_S` (default:
5 seconds). Inverter temperatures, BMS cell data and fault words use
`RENAC_INVERTER_DIAGNOSTICS_INTERVAL_S` (default: 30 seconds). Battery and BMS
readings that share a register block with battery power and SoC use the
faster poll. After three consecutive failures, a telemetry block uses the
diagnostics interval until it succeeds.

**Fault active** is on only when the inverter reports `Fault` or `Permanent`.
An unrecognized state code is logged and displayed as `Unknown`.
The diagnostic **Fault code** sensor lists non-zero fault words as numbered
hex values (for example, `07:0x0018`), or displays `none` when all words are
zero. Its attributes contain all 16 raw words. The meanings of individual
fault bits are undocumented.

The **Bypass relay** configuration switch is disabled by default in Home
Assistant. The bridge reads the setting on connection and during actuator
polling, and confirms writes before publishing the new state.

**Warning:** Changing the bypass relay setting changes how the EPS output is
fed. Before enabling the entity or changing the setting, check how bypass
affects the loads connected to your EPS output.

---

## 📚 Related
- [renac-ble](https://github.com/voluzi/renac-ble) – BLE library for RENAC devices

---

## 📜 License
This project is open source and available under the [MIT License](LICENSE).
