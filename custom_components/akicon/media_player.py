"""Media player platform for the Akicon Bath Fan Speaker."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.util.dt as dt_util

from . import AkiconConfigEntry
from .bridge import AkiconBridge, AkiconBridgeError
from .const import DEFAULT_NAME, DOMAIN, MANUFACTURER, MODEL, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=SCAN_INTERVAL_SECONDS)

SUPPORT_AKICON = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AkiconConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Akicon media player from a config entry."""
    name = entry.data.get(CONF_NAME, DEFAULT_NAME)
    async_add_entities([AkiconMediaPlayer(entry.runtime_data, entry.entry_id, name)])


class AkiconMediaPlayer(MediaPlayerEntity):
    """Represent the Akicon speaker as a Home Assistant media player."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = SUPPORT_AKICON
    _attr_media_content_type = MediaType.MUSIC

    def __init__(self, bridge: AkiconBridge, entry_id: str, name: str) -> None:
        """Initialise the entity."""
        self._bridge = bridge
        self._attr_unique_id = bridge.mac
        self._attr_available = True
        self._attr_state = MediaPlayerState.IDLE
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, bridge.mac)},
            connections={("bluetooth", bridge.mac)},
            name=name,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    async def async_update(self) -> None:
        """Poll mpv for the current playback state."""
        try:
            status = await self._bridge.async_status()
        except AkiconBridgeError:
            self._attr_available = False
            return

        self._attr_available = True

        if not status:
            self._attr_state = MediaPlayerState.IDLE
            self._attr_media_title = None
            self._attr_media_duration = None
            self._attr_media_position = None
            return

        volume = status.get("volume")
        if volume is not None:
            self._attr_volume_level = max(0.0, min(1.0, volume / 100))
        self._attr_is_volume_muted = bool(status.get("mute"))

        if status.get("idle-active") or status.get("eof-reached"):
            self._attr_state = MediaPlayerState.IDLE
            self._attr_media_title = None
            self._attr_media_duration = None
            self._attr_media_position = None
            return

        self._attr_state = (
            MediaPlayerState.PAUSED
            if status.get("pause")
            else MediaPlayerState.PLAYING
        )
        self._attr_media_title = status.get("media-title")
        duration = status.get("duration")
        self._attr_media_duration = int(duration) if duration else None
        position = status.get("time-pos")
        if position is not None:
            self._attr_media_position = int(position)
            self._attr_media_position_updated_at = dt_util.utcnow()

    async def async_turn_on(self) -> None:
        """Connect the speaker over Bluetooth."""
        await self._call(self._bridge.async_ensure_ready())
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Stop playback and disconnect the speaker."""
        await self._call(self._bridge.async_stop())
        await self._call(self._bridge.async_disconnect())
        self._attr_state = MediaPlayerState.OFF
        self.async_write_ha_state()

    async def async_media_play(self) -> None:
        """Resume playback."""
        await self._call(self._bridge.async_resume())
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    async def async_media_pause(self) -> None:
        """Pause playback."""
        await self._call(self._bridge.async_pause())
        self._attr_state = MediaPlayerState.PAUSED
        self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        """Stop playback."""
        await self._call(self._bridge.async_stop())
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the volume level, 0.0-1.0."""
        await self._call(self._bridge.async_set_volume(volume))
        self._attr_volume_level = volume
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute the speaker."""
        await self._call(self._bridge.async_set_mute(mute))
        self._attr_is_volume_muted = mute
        self.async_write_ha_state()

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs
    ) -> None:
        """Play a URL, a TTS announcement, or a media-source item."""
        if media_source.is_media_source_id(media_id):
            sourced = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = sourced.url

        media_id = async_process_play_media_url(self.hass, media_id)

        await self._call(self._bridge.async_play_url(media_id))
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    async def async_browse_media(
        self, media_content_type: str | None = None, media_content_id: str | None = None
    ):
        """Let the UI browse media sources (TTS, local media, etc.)."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )

    async def _call(self, coro) -> None:
        """Await a bridge coroutine, turning bridge errors into HA errors."""
        try:
            await coro
        except AkiconBridgeError as err:
            raise HomeAssistantError(str(err)) from err
