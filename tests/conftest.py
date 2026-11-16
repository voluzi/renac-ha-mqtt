from unittest.mock import MagicMock

import pytest

from renac_ha_bridge import __main__ as bridge
from renac_ha_mqtt import mqtt_device


@pytest.fixture
def mqtt_clients(monkeypatch):
    clients = []

    def create_client(*args, **kwargs):
        client = MagicMock()
        clients.append(client)
        return client

    factory = MagicMock(side_effect=create_client)
    monkeypatch.setattr(mqtt_device.mqtt, "Client", factory)
    return factory, clients


@pytest.fixture
def device(mqtt_clients):
    dev = mqtt_device.RenacMqttDevice(
        "inverter_test", "Test inverter", "Test model",
        {"number": {"min_soc": {}, "min_soc_on_grid": {}}}, "localhost",
    )
    yield dev
    dev.disconnect()


@pytest.fixture
def bridge_devices(monkeypatch, mqtt_clients):
    monkeypatch.setattr(bridge, "inverter_mqtt_by_addr", {})
    monkeypatch.setattr(bridge, "wallbox_mqtt_by_addr", {})
    monkeypatch.setattr(bridge, "BLE_RECONNECT_DELAY_S", 0, raising=False)
    yield
    for dev in [*bridge.inverter_mqtt_by_addr.values(), *bridge.wallbox_mqtt_by_addr.values()]:
        dev.disconnect()
