"""The breville_joule integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN
from .sensor import BrevilleAuth

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Breville Joule integration using YAML configuration."""
    if DOMAIN not in config:
        return True

    hass.data.setdefault(DOMAIN, {})

    username = config[DOMAIN].get("username")
    password = config[DOMAIN].get("password")

    polling_interval = config[DOMAIN].get("polling_interval")

    auth = BrevilleAuth(hass, username, password, polling_interval)
    hass.data[DOMAIN]["auth"] = auth

    hass.async_create_task(
        hass.helpers.discovery.async_load_platform("sensor", DOMAIN, {}, config)
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return False  # Not applicable as we're not using config entries.
