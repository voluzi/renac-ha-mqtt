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
- Exposes control actuators (charge/discharge limits, Min SoC, export limits, …)
- Fully compatible with **Home Assistant MQTT Discovery**
- Works with **any RENAC backend** (BLE, cloud, or others)
- Includes a ready-to-use **bridge CLI** (`renac-bridge`) for BLE ↔ MQTT

---

## 📦 Installation

```bash
pip install renac-ha-mqtt
```

This installs both `renac_ha_mqtt` and `renac_ha_bridge`, along with the `renac-bridge` CLI.

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
renac-bridge
```

This will connect to both the inverter and the wallbox via BLE and publish their telemetry/control entities into Home Assistant.

---

## 📚 Related
- [renac-ble](https://github.com/voluzi/renac-ble) – BLE library for RENAC devices

---

## 📜 License
This project is open source and available under the [MIT License](LICENSE).
