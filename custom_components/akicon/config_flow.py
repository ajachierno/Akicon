"""Config flow for the Akicon Bath Fan Speaker integration."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_AUDIO_DEVICE,
    CONF_MAC,
    CONF_MPV_PATH,
    DEFAULT_MPV_PATH,
    DEFAULT_NAME,
    DOMAIN,
)

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _normalise_mac(value: str) -> str:
    """Validate and upper-case a Bluetooth MAC address."""
    value = value.strip()
    if not MAC_RE.match(value):
        raise vol.Invalid("invalid_mac")
    return value.upper()


class AkiconConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the speaker's MAC and optional audio settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                mac = _normalise_mac(user_input[CONF_MAC])
            except vol.Invalid:
                errors["base"] = "invalid_mac"
            else:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()

                data = {
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_MAC: mac,
                    CONF_MPV_PATH: user_input.get(CONF_MPV_PATH, DEFAULT_MPV_PATH),
                }
                audio_device = user_input.get(CONF_AUDIO_DEVICE, "").strip()
                if audio_device:
                    data[CONF_AUDIO_DEVICE] = audio_device
                return self.async_create_entry(title=user_input[CONF_NAME], data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): cv.string,
                vol.Required(CONF_MAC): cv.string,
                vol.Optional(CONF_AUDIO_DEVICE, default=""): cv.string,
                vol.Optional(CONF_MPV_PATH, default=DEFAULT_MPV_PATH): cv.string,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AkiconOptionsFlow:
        """Return the options flow."""
        return AkiconOptionsFlow()


class AkiconOptionsFlow(OptionsFlow):
    """Allow editing the audio device and mpv path after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            data = {**self.config_entry.data}
            audio_device = user_input.get(CONF_AUDIO_DEVICE, "").strip()
            if audio_device:
                data[CONF_AUDIO_DEVICE] = audio_device
            else:
                data.pop(CONF_AUDIO_DEVICE, None)
            data[CONF_MPV_PATH] = user_input.get(CONF_MPV_PATH, DEFAULT_MPV_PATH)

            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            return self.async_create_entry(title="", data={})

        current = self.config_entry.data
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_AUDIO_DEVICE,
                    default=current.get(CONF_AUDIO_DEVICE, ""),
                ): cv.string,
                vol.Optional(
                    CONF_MPV_PATH,
                    default=current.get(CONF_MPV_PATH, DEFAULT_MPV_PATH),
                ): cv.string,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
