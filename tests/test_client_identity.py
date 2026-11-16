import os
import socket

import pytest

from renac_ha_mqtt import RenacInverterDevice, RenacWallboxDevice


@pytest.mark.parametrize("device_type", [RenacInverterDevice, RenacWallboxDevice])
def test_missing_serial_uses_ble_address_only_for_client_identity(device_type, mqtt_clients):
    devices = []
    try:
        for addr in ("AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"):
            devices.append(device_type("RENAC", None, "test", "localhost", ble_address=addr))
        factory, clients = mqtt_clients
        ids = [c.kwargs["client_id"] for c in factory.call_args_list]
        assert ids[0] != ids[1]
        assert all(addr in client_id for addr, client_id in zip(
            ("AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"), ids,
        ))
        assert devices[0].device_id == devices[1].device_id
        for dev, client in zip(devices, clients):
            client.will_set.assert_called_once_with(
                f"homeassistant/{dev.device_id}/availability", "offline", retain=True,
            )
    finally:
        for dev in devices:
            dev.disconnect()


@pytest.mark.parametrize("second_prefix", [None, "second-bridge"])
def test_client_identity_separates_bridge_instances(mqtt_clients, monkeypatch, second_prefix):
    devices = []
    try:
        monkeypatch.delenv("MQTT_CLIENT_ID_PREFIX", raising=False)
        monkeypatch.setattr(socket, "gethostname", lambda: "bridge-host")
        monkeypatch.setattr(os, "getpid", lambda: 100)
        devices.append(RenacInverterDevice("RENAC", "serial", "test", "localhost"))
        if second_prefix is not None:
            monkeypatch.setenv("MQTT_CLIENT_ID_PREFIX", second_prefix)
        monkeypatch.setattr(os, "getpid", lambda: 200)
        devices.append(RenacInverterDevice("RENAC", "serial", "test", "localhost"))
        factory, _ = mqtt_clients
        first, second = [c.kwargs["client_id"] for c in factory.call_args_list]
        assert first != second
        assert "bridge-host" in first
        assert (second_prefix or "bridge-host") in second
        assert "100" in first
        assert "200" in second
        assert devices[0].device_id == devices[1].device_id == "inverter_serial"
    finally:
        for dev in devices:
            dev.disconnect()
