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

1. It connects to the speaker over Bluetooth using `bluetoothctl` (BlueZ), and
   pairs it automatically when the speaker is discoverable.
2. It plays media through one of two engines, chosen automatically:
   - **mpv** (preferred) runs as a long-lived background process driven over its
     JSON IPC socket. Full transport control: play, pause, stop, volume, mute,
     and title/position/duration readback.
   - **ffmpeg** is the fallback for hosts where mpv can't be installed — notably
     Home Assistant OS, whose Core container ships ffmpeg but not mpv. It plays
     the URL straight to the speaker's PulseAudio sink. Play, stop, and
     pause/resume work; volume and mute changes apply to the next track, and
     position/duration are not reported.

Because TTS and Music Assistant both hand off a URL, they work through either
engine.

## What the integration does for you

- Connects to the speaker on demand (on play, or on `media_player.turn_on`) and
  reconnects if it has dropped.
- Attempts to pair and trust the speaker automatically when it's discoverable,
  so in the common case you never touch a shell.
- Picks the playback engine automatically (mpv if installed, otherwise ffmpeg).
- Works out the speaker's PulseAudio sink from its MAC, so the audio device field
  is optional. Leave it blank unless you're on PipeWire or a non-standard sink.
- Remembers volume and mute while idle and applies them when playback starts.

## Requirements

This integration controls host-level Bluetooth audio, so it needs a Home
Assistant install that can reach the host's Bluetooth and audio stack:

- Home Assistant on Linux with a working Bluetooth adapter — a Raspberry Pi or a
  mini PC is typical. Home Assistant OS works too: its Core container ships
  ffmpeg and the Supervisor provides a shared PulseAudio server, so the ffmpeg
  engine plays to the speaker there with nothing extra to install.
- `bluez` / `bluetoothctl` available to Home Assistant and the adapter powered on.
- A running audio server (PulseAudio or PipeWire) that exposes the speaker as a
  sink. On Home Assistant OS this happens automatically once the speaker is paired.
- A playback engine: either `mpv` on `PATH` (`sudo apt install mpv`) for full
  transport control, or `ffmpeg` (bundled with Home Assistant Core) for the
  fallback path.

## Installation

### HACS (custom repository)

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/ajachierno/Akicon` with category **Integration**.
3. Install **Akicon Bath Fan Speaker** and restart Home Assistant.

### Manual

Copy `custom_components/akicon` into your Home Assistant
`config/custom_components` directory and restart.

## Setup

### 1. Get the speaker's MAC address

You need the speaker's **classic Bluetooth (A2DP) address**, not a BLE address
and not your phone's address. On the Home Assistant host, with the speaker in
pairing mode and disconnected from any phone:

```bash
bluetoothctl --timeout 20 scan on
bluetoothctl devices
```

Look for an audio device (often named like `WIRELESS SPEAKER` or `BT401`). Note
its MAC, e.g. `AA:BB:CC:DD:EE:FF`.

Some of these modules advertise two addresses one apart: an odd one ending in
`-BLE` (the BLE side) and an even one (the classic audio side). You want the
classic one — that's the address that carries audio.

### 2. Pair the speaker (usually automatic)

When you add the integration and first play, it tries to pair, trust, and connect
the speaker on its own. That works when the speaker is in pairing mode and not
already connected to a phone.

If auto-pairing doesn't take, pair it once by hand on the host. Turn off
Bluetooth on your phone first (A2DP allows only one source at a time), put the
fan in pairing mode per its manual, then:

```bash
bluetoothctl
```

At the `[bluetoothctl]#` prompt:

```
power on
agent on
default-agent
scan on
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
connect AA:BB:CC:DD:EE:FF
scan off
exit
```

Confirm with `bluetoothctl info AA:BB:CC:DD:EE:FF` — you want `Paired: yes`,
`Trusted: yes`, `Connected: yes`. Trusting it lets BlueZ reconnect on its own,
which is what the integration relies on.

### 3. Add the integration

Go to **Settings → Devices & services → Add integration → Akicon Bath Fan
Speaker**, then enter:

- **Name** — the friendly name for the media player entity.
- **Bluetooth MAC address** — the classic address from step 1.
- **Audio device** — leave blank to auto-detect. Only set it for PipeWire or a
  non-standard sink (see below).
- **Path to mpv** — leave as `mpv`; it's ignored when the ffmpeg fallback runs.

The audio device and mpv path can be changed later from the entry's **Configure**
button.

### Finding the audio device manually (only if auto-detect is wrong)

With the speaker connected, list the sinks the player can see:

```bash
mpv --audio-device=help | grep -i bluez
# or, if you only have ffmpeg:
pactl list sinks short | grep -i bluez
```

Typical values:

- PulseAudio: `pulse/bluez_sink.AA_BB_CC_DD_EE_FF.a2dp_sink`
- PipeWire: `pulse/bluez_output.AA_BB_CC_DD_EE_FF.1`

Put that string in the **Audio device** field.

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

The entity is also a target for Music Assistant and shows up in the media
browser, so you can pick tracks from the UI.

## Limitations

- One speaker per config entry. Add the integration again for a second unit.
- A2DP is one-way audio. There's no track metadata from the fan itself; any title
  or position comes from the player reading the source you gave it.
- Under the ffmpeg engine (Home Assistant OS), volume and mute apply to the next
  track rather than mid-playback, and position/duration aren't reported. The mpv
  engine has none of these limits.
- A2DP speakers drop whatever is still buffered when playback ends, which would
  clip the last few seconds of a file. Under ffmpeg the integration appends a
  short trailing silence so nothing real is lost; the player therefore stays in
  the `playing` state for a few extra seconds of silence after the audio ends.
- The fan's own controls (the physical fan and light) are not part of this
  integration — it only handles the speaker.
- Bluetooth range and interference apply as they would for any BT speaker.

## Troubleshooting

**"mpv executable ... not found" / "Neither mpv nor ffmpeg was found"**
The host has no playback engine on the path Home Assistant uses. Install mpv
(`sudo apt install mpv`) or make sure ffmpeg is present. On Home Assistant OS,
ffmpeg lives in the Core container, which is separate from the SSH add-on — you
can't `apt`/`apk` mpv into Core, so the integration uses ffmpeg there instead.

**"ffmpeg could not play to PulseAudio: Unknown output format 'pulse'"**
Your ffmpeg build was compiled without the PulseAudio output muxer. Install mpv
so the integration uses it instead, or run the audio bridge on a host with a
pulse-enabled ffmpeg.

**"ffmpeg could not play to PulseAudio: Connection refused" (or similar)**
ffmpeg can't reach the audio server. Confirm PulseAudio is running and that the
Home Assistant process can see it (`pactl info` should return a server). On Home
Assistant OS the socket is `unix:/run/audio/pulse.sock`, which the integration
uses by default.

**"Could not connect ... Device ... not available"**
BlueZ hasn't discovered the speaker's classic audio radio. Usually the speaker is
still connected to a phone (turn the phone's Bluetooth off — A2DP is one source
at a time) or isn't in pairing mode. Put it in pairing mode and try again.

If a plain scan only shows a BLE address and never the classic one, force a
classic (BR/EDR) inquiry: inside `bluetoothctl`, run `menu scan`, then
`transport bredr`, then `back`, then `scan on`. Home Assistant's own constant BLE
scanning can otherwise hide the classic side.

**Connects, but no sound**
The sink is probably wrong or the speaker isn't the target. Confirm the speaker
shows up with `pactl list sinks short | grep -i bluez`, then set that exact sink
in the **Audio device** field. Also check the entity isn't muted and the volume
isn't at zero. Remember that under ffmpeg a volume change only takes effect on the
next track.

**Interactive `bluetoothctl` scrolls endlessly and `scan off` seems ignored**
Home Assistant keeps the adapter in a passive scan, so its device events keep
printing. Use one-shot shell commands instead (they run and exit), e.g.
`bluetoothctl info AA:BB:CC:DD:EE:FF`, `bluetoothctl pair AA:BB:CC:DD:EE:FF`.

**Audio drops out**
Check the signal — `bluetoothctl info AA:BB:CC:DD:EE:FF` reports RSSI. A weak
signal (roughly -75 dBm or worse) causes A2DP dropouts. Move the adapter closer,
add a better antenna, or use a Bluetooth adapter with more range.

### Debug logging

```yaml
logger:
  logs:
    custom_components.akicon: debug
```

This logs the chosen engine, the exact mpv/ffmpeg command, and the resolved sink.

## Disclaimer

Unofficial community integration, not affiliated with or endorsed by Akicon. Use
at your own risk. Licensed under the MIT License — see [LICENSE](LICENSE).

## Buy me a coffee

Did you find this helpful? Consider supporting additional development:

<a href="https://www.buymeacoffee.com/ajachiernoo" target="_blank">
  <img src="https://raw.githubusercontent.com/ajachierno/Akicon/main/brand/bmc_button.png" alt="Buy me a coffee" height="50">
</a>

<br><br>

<a href="https://www.buymeacoffee.com/ajachiernoo" target="_blank">
  <img src="https://raw.githubusercontent.com/ajachierno/Akicon/main/brand/buymeacoffee_qr.png" alt="Buy me a coffee QR code" width="200">
</a>
