"""The breville_joule integration."""

from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.discovery import async_load_platform

from .const import DOMAIN
from .sensor import BrevilleAuth

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Breville Joule integration using YAML configuration."""
    if DOMAIN not in config:
        return True

    hass.data.setdefault(DOMAIN, {})

    username = config[DOMAIN].get("username")
    password = config[DOMAIN].get("password")
    polling_interval = config[DOMAIN].get("polling_interval", 60)

    _LOGGER.debug("Setting up breville_joule with username: %s", username)

    auth = BrevilleAuth(hass, username, password, polling_interval)
    hass.data[DOMAIN]["auth"] = auth

    hass.async_create_task(
        async_load_platform(hass, "sensor", DOMAIN, {}, config)
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return False  # Not applicable as we're not using config entries.