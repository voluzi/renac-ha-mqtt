FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System libs required for Bleak/BlueZ DBus (no bluetoothd here; we use host's)
RUN apt-get update && apt-get install -y --no-install-recommends \
      dbus \
      bluez \
      libglib2.0-0 \
      ca-certificates \
      git \
      && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE /app/
COPY src /app/src

RUN pip install --upgrade pip && \
    pip install "."

ENV MQTT_HOST=127.0.0.1 \
    MQTT_PORT=1883 \
    MQTT_USER=renacble \
    MQTT_PASSWORD=renacble \
    MQTT_PREFIX=homeassistant

ENTRYPOINT ["renac-ble-ha-bridge"]