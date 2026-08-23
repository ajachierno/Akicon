"""Constants for the Akicon Bath Fan Speaker integration."""

from __future__ import annotations

DOMAIN = "akicon"

# Config entry keys
CONF_MAC = "mac"
CONF_AUDIO_DEVICE = "audio_device"
CONF_MPV_PATH = "mpv_path"

# Defaults
DEFAULT_NAME = "Akicon Bath Fan Speaker"
DEFAULT_MPV_PATH = "mpv"

# Device metadata
MANUFACTURER = "Akicon"
MODEL = "AK-SNP80-W"

# How often the media player polls mpv for status, in seconds.
SCAN_INTERVAL_SECONDS = 5
