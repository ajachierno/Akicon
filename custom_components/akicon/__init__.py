"""The Akicon Bath Fan Speaker integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .bridge import AkiconBridge
from .const import CONF_AUDIO_DEVICE, CONF_MAC, CONF_MPV_PATH, DEFAULT_MPV_PATH

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]

type AkiconConfigEntry = ConfigEntry[AkiconBridge]


async def async_setup_entry(hass: HomeAssistant, entry: AkiconConfigEntry) -> bool:
    """Set up Akicon from a config entry."""
    bridge = AkiconBridge(
        mac=entry.data[CONF_MAC],
        audio_device=entry.data.get(CONF_AUDIO_DEVICE),
        mpv_path=entry.data.get(CONF_MPV_PATH, DEFAULT_MPV_PATH),
    )
    entry.runtime_data = bridge

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AkiconConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: AkiconConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
