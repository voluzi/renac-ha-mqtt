# renac_ha_mqtt_bridge/__init__.py
from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("renac-ha-mqtt")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

__all__ = ["__version__"]