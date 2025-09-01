from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("renac-ha-mqtt")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

# Public API (kept for backward compatibility)
from .mqtt_device import RenacMqttDevice
from .inverter import RenacInverterDevice
from .wallbox import RenacWallboxDevice

__all__ = [
    "__version__",
    "RenacMqttDevice",
    "RenacInverterDevice",
    "RenacWallboxDevice",
]