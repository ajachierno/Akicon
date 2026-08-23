# Akicon Bath Fan Speaker

A Home Assistant integration that turns the Bluetooth speaker in an Akicon
AK-SNP80-W bathroom exhaust fan into a normal `media_player` entity. Once set
up, you can stream music to it and fire TTS announcements the same way you would
with any other speaker.

## How it works

The AK-SNP80-W's speaker is a plain Bluetooth A2DP sink. It has no Wi-Fi, no
network stack, and no API, so Home Assistant can't "cast" to it the way it casts
to a Chromecast or a Sonos. The only way to play audio on it is to connect over
Bluetooth and push a decoded audio stream.

This integration does that on the Home Assistant host:

1. It connects to the speaker over Bluetooth using `bluetoothctl` (BlueZ).
2. It runs `mpv` as a long-lived background process and drives it over mpv's
   JSON IPC socket. When you call `media_player.play_media`, it hands mpv the
   URL; mpv decodes it and outputs the audio to the speaker's Bluetooth sink.

Because TTS and Music Assistant both produce a URL, they work through the same
path. The entity supports play, pause, stop, volume, mute, and reports
title/position/duration while something is playing.

## Requirements

This integration controls host-level Bluetooth audio, so it needs a Home
Assistant install that can reach the host's Bluetooth and audio stack:

- Home Assistant running on Linux (Core or Supervised) on hardware with a
  working Bluetooth adapter — a Raspberry Pi or a mini PC is typical. It does
  not work on Home Assistant OS in its default container setup, which does not
  expose the host audio server to the core container.
- `bluez` / `bluetoothctl` installed and the adapter powered on.
- A working audio server (PipeWire or PulseAudio) that the Home Assistant
  process can talk to.
- `mpv` installed and on `PATH` (`sudo apt install mpv`).

## Installation

### HACS (custom repository)

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/ajachierno/Akicon` with category **Integration**.
3. Install **Akicon Bath Fan Speaker** and restart Home Assistant.

### Manual

Copy `custom_components/akicon` into your Home Assistant `config/custom_components`
directory and restart.

## One-time Bluetooth pairing

Pair and trust the speaker once from a shell on the Home Assistant host. Put the
fan into pairing mode per its manual (usually powered on with no active
connection), then:

```bash
bluetoothctl
power on
agent on
default-agent
scan on          # wait for the speaker to appear, note its MAC
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
connect AA:BB:CC:DD:EE:FF
scan off
exit
```

Trusting it lets BlueZ reconnect automatically, which is what the integration
relies on. You only do this once.

## Finding the mpv audio device

When the speaker is connected, list the audio devices mpv can see:

```bash
mpv --audio-device=help
```

Look for an entry that contains `bluez` and the speaker's MAC, for example:

- PipeWire: `pulse/bluez_output.AA_BB_CC_DD_EE_FF.1`
- PulseAudio: `pulse/bluez_sink.AA_BB_CC_DD_EE_FF.a2dp_sink`

Use that string for the audio device in the config flow. If you leave it blank,
mpv plays to the host's default output — fine if the speaker is already the
default sink, otherwise set it explicitly.

## Configuration

Add the integration from **Settings → Devices & services → Add integration →
Akicon Bath Fan Speaker**, then enter:

- **Name** — the friendly name for the media player entity.
- **Bluetooth MAC address** — the speaker's address, e.g. `AA:BB:CC:DD:EE:FF`.
- **mpv audio device** — the string from the step above (optional).
- **Path to mpv** — leave as `mpv` unless the binary lives somewhere unusual.

The audio device and mpv path can be changed later from the entry's
**Configure** button.

## Usage

TTS announcement:

```yaml
service: tts.speak
target:
  entity_id: tts.piper
data:
  media_player_entity_id: media_player.akicon_bath_fan_speaker
  message: "The laundry is done."
```

Play a stream or file URL:

```yaml
service: media_player.play_media
target:
  entity_id: media_player.akicon_bath_fan_speaker
data:
  media_content_id: "http://example.com/stream.mp3"
  media_content_type: music
```

The entity also shows up as a target for Music Assistant and for the media
browser, so you can pick tracks from the UI.

## Limitations

- One speaker per config entry. Add the integration again for a second unit.
- A2DP is one-way audio. There is no track metadata coming back from the fan
  itself; title and position come from mpv reading the source you gave it.
- The fan's own controls (the physical fan, light) are not part of this
  integration — it only handles the speaker.
- Bluetooth range and interference apply as they would for any BT speaker.

## Troubleshooting

- **"mpv executable not found"** — install mpv on the host and make sure it's on
  the same `PATH` the Home Assistant process uses.
- **"Could not connect"** — confirm the speaker is paired and trusted
  (`bluetoothctl info AA:BB:CC:DD:EE:FF` should show `Paired: yes` and
  `Trusted: yes`), and that nothing else is connected to it.
- **Connects but no sound** — the audio device string is probably wrong or the
  sink isn't the default. Re-run `mpv --audio-device=help` while connected and
  set the exact `bluez` device.
- Turn on debug logging to see the mpv command and IPC traffic:

  ```yaml
  logger:
    logs:
      custom_components.akicon: debug
  ```

## License

MIT. See [LICENSE](LICENSE).
