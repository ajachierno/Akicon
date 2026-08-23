"""Audio bridge for the Akicon Bath Fan Speaker.

The AK-SNP80-W speaker is a plain Bluetooth A2DP sink: it has no network
interface and no API. To use it like a Home Assistant media player we have to
do two things on the host that runs Home Assistant:

1. Hold a Bluetooth connection to the speaker (via ``bluetoothctl``).
2. Decode media and push the audio to the speaker's audio sink.

Two playback engines are supported and auto-selected:

* ``mpv`` (preferred) runs as a long-lived idle process driven over its JSON IPC
  socket, giving real transport control: load a URL, pause, resume, stop, set
  volume, and read back position/duration/title.
* ``ffmpeg`` is the fallback for hosts where mpv cannot be installed (notably
  Home Assistant OS, where the Core container ships ffmpeg but not mpv). It plays
  a URL straight to the PulseAudio sink. Playback is per-track: play, stop, and
  pause/resume (via process signals) work; live volume changes apply to the next
  track, and position/duration are not reported.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal

_LOGGER = logging.getLogger(__name__)

# Timeouts (seconds)
_CONNECT_TIMEOUT = 20
_IPC_TIMEOUT = 3
_MPV_START_TIMEOUT = 5
_FF_START_GRACE = 0.4
# Seconds of trailing silence appended under ffmpeg so the audio an A2DP speaker
# drops from its buffer when playback ends is silence, not the real tail.
_FF_TAIL_PAD = 10

# Default PulseAudio socket on Home Assistant OS, used if the environment does
# not already point at a server.
_HAOS_PULSE = "unix:/run/audio/pulse.sock"


class AkiconBridgeError(Exception):
    """Raised when the bridge cannot talk to the player or the speaker."""


class AkiconBridge:
    """Manage the Bluetooth connection and the audio pipeline."""

    def __init__(
        self,
        mac: str,
        audio_device: str | None = None,
        mpv_path: str = "mpv",
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        """Set up the bridge for one speaker."""
        self._mac = mac.upper()
        self._audio_device = audio_device or None
        self._mpv_path = mpv_path
        self._ffmpeg_path = ffmpeg_path
        safe = self._mac.replace(":", "")
        self._sock = f"/tmp/akicon-{safe}.sock"
        self._engine: str | None = None
        # mpv engine state
        self._proc: asyncio.subprocess.Process | None = None
        # ffmpeg engine state
        self._ff_proc: asyncio.subprocess.Process | None = None
        self._ff_stderr_task: asyncio.Task | None = None
        self._state = "idle"
        # Shared, remembered so a change while idle applies at the next play.
        self._volume = 100
        self._mute = False
        self._resolved_sink: str | None = None
        self._lock = asyncio.Lock()

    @property
    def mac(self) -> str:
        """Return the speaker's Bluetooth MAC."""
        return self._mac

    def _detect_engine(self) -> str:
        """Pick and cache the playback engine: mpv if present, else ffmpeg."""
        if self._engine:
            return self._engine
        if shutil.which(self._mpv_path):
            self._engine = "mpv"
        elif shutil.which(self._ffmpeg_path):
            self._engine = "ffmpeg"
        else:
            raise AkiconBridgeError(
                f"Neither mpv ('{self._mpv_path}') nor ffmpeg "
                f"('{self._ffmpeg_path}') was found on the Home Assistant host. "
                "Install mpv, or make ffmpeg available."
            )
        _LOGGER.debug("Akicon playback engine: %s", self._engine)
        return self._engine

    # ---------------------------------------------------------------- Bluetooth

    async def async_is_connected(self) -> bool:
        """Return True if bluetoothctl reports the speaker as connected."""
        code, out, _ = await self._run("bluetoothctl", "info", self._mac)
        if code != 0:
            return False
        return "Connected: yes" in out

    async def async_connect(self) -> None:
        """Connect to the speaker, pairing it automatically if needed."""
        if await self.async_is_connected():
            return

        await self._run("bluetoothctl", "power", "on")

        # A plain connect works when the speaker is already paired and trusted.
        code, out, err = await self._run(
            "bluetoothctl", "connect", self._mac, timeout=_CONNECT_TIMEOUT
        )
        if self._connect_ok(code, out, err) or await self.async_is_connected():
            return

        # Best-effort auto-pair when we have never bonded with it. This only
        # succeeds if the speaker is discoverable (in pairing mode) and not held
        # by another source such as a phone.
        if not await self._is_paired():
            _LOGGER.debug("Attempting to auto-pair %s", self._mac)
            await self._run("bluetoothctl", "--timeout", "12", "scan", "on", timeout=16)
            await self._run("bluetoothctl", "pair", self._mac, timeout=_CONNECT_TIMEOUT)
            await self._run("bluetoothctl", "trust", self._mac)
            code, out, err = await self._run(
                "bluetoothctl", "connect", self._mac, timeout=_CONNECT_TIMEOUT
            )
            if self._connect_ok(code, out, err) or await self.async_is_connected():
                return

        raise AkiconBridgeError(
            f"Could not connect to {self._mac}. If it has never been paired, put "
            "the speaker in pairing mode and disconnect it from any phone, then try "
            "again; or pair it once by hand with bluetoothctl. Last output: "
            f"{(out + err).strip()}"
        )

    async def async_disconnect(self) -> None:
        """Disconnect the speaker over Bluetooth."""
        await self._run("bluetoothctl", "disconnect", self._mac, timeout=_CONNECT_TIMEOUT)

    async def _is_paired(self) -> bool:
        """Return True if bluetoothctl reports the speaker as paired."""
        code, out, _ = await self._run("bluetoothctl", "info", self._mac)
        return code == 0 and "Paired: yes" in out

    @staticmethod
    def _connect_ok(code: int | None, out: str, err: str) -> bool:
        """Return True if a bluetoothctl connect reported success."""
        combined = f"{out}\n{err}".lower()
        return code == 0 and (
            "connection successful" in combined or "already connected" in combined
        )

    async def _resolve_sink(self) -> str:
        """Return the PulseAudio sink name for the speaker (no 'pulse/' prefix).

        Uses the configured value when given; otherwise finds the sink via
        ``pactl`` when available, and finally derives the deterministic
        PulseAudio bluez name from the MAC.
        """
        if self._audio_device:
            dev = self._audio_device
            return dev[len("pulse/") :] if dev.startswith("pulse/") else dev
        if self._resolved_sink:
            return self._resolved_sink

        mac_us = self._mac.replace(":", "_")
        try:
            code, out, _ = await self._run("pactl", "list", "sinks", "short")
        except AkiconBridgeError:
            code, out = 1, ""
        if code == 0:
            for line in out.splitlines():
                for field in line.split("\t"):
                    if mac_us in field and "blue" in field.lower():
                        self._resolved_sink = field.strip()
                        _LOGGER.debug("Resolved sink via pactl: %s", self._resolved_sink)
                        return self._resolved_sink

        self._resolved_sink = f"bluez_sink.{mac_us}.a2dp_sink"
        _LOGGER.debug("Derived sink from MAC: %s", self._resolved_sink)
        return self._resolved_sink

    async def async_ensure_ready(self) -> None:
        """Make sure the speaker is connected and, for mpv, that it is running."""
        await self.async_connect()
        if self._detect_engine() == "mpv":
            await self._ensure_mpv()

    # ----------------------------------------------------------- transport API

    async def async_play_url(self, url: str) -> None:
        """Load and play a URL, replacing anything currently playing."""
        await self.async_connect()
        if self._detect_engine() == "mpv":
            await self._ensure_mpv()
            await self._rpc(
                [["loadfile", url, "replace"], ["set_property", "pause", False]]
            )
        else:
            await self._ff_play(url)

    async def async_pause(self) -> None:
        """Pause playback. No-op if nothing is running."""
        if self._engine == "ffmpeg":
            if self._ff_running():
                with contextlib.suppress(ProcessLookupError):
                    self._ff_proc.send_signal(signal.SIGSTOP)
                self._state = "paused"
            return
        if self._mpv_running():
            await self._rpc([["set_property", "pause", True]])

    async def async_resume(self) -> None:
        """Resume playback. No-op if nothing is running."""
        if self._engine == "ffmpeg":
            if self._ff_running():
                with contextlib.suppress(ProcessLookupError):
                    self._ff_proc.send_signal(signal.SIGCONT)
                self._state = "playing"
            return
        if self._mpv_running():
            await self._rpc([["set_property", "pause", False]])

    async def async_stop(self) -> None:
        """Stop playback. No-op if nothing is running."""
        if self._engine == "ffmpeg":
            await self._ff_kill()
            self._state = "idle"
            return
        if self._mpv_running():
            await self._rpc([["stop"]])

    async def async_set_volume(self, level: float) -> None:
        """Set volume from a 0.0-1.0 level.

        Applied live under mpv; under ffmpeg it takes effect on the next track.
        """
        self._volume = max(0, min(100, round(level * 100)))
        if self._engine == "mpv" and self._mpv_running():
            await self._rpc([["set_property", "volume", self._volume]])

    async def async_set_mute(self, mute: bool) -> None:
        """Mute or unmute (live under mpv, next-track under ffmpeg)."""
        self._mute = bool(mute)
        if self._engine == "mpv" and self._mpv_running():
            await self._rpc([["set_property", "mute", self._mute]])

    async def async_status(self) -> dict:
        """Return a normalised playback snapshot.

        Keys: state ('idle'|'playing'|'paused'), volume (0-100), mute (bool),
        and title/duration/position (mpv only; None under ffmpeg).
        """
        if self._engine == "mpv":
            return await self._mpv_status()
        if self._engine == "ffmpeg":
            return self._ffmpeg_status()
        return self._idle_status()

    async def async_shutdown(self) -> None:
        """Stop playback and any long-lived process. Leaves Bluetooth alone."""
        await self._ff_kill()
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(Exception):
                await self._rpc([["quit"]])
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            self._proc = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._sock)

    # -------------------------------------------------------------- ffmpeg impl

    def _ff_running(self) -> bool:
        """Return True if the ffmpeg playback process is alive."""
        return self._ff_proc is not None and self._ff_proc.returncode is None

    async def _ff_play(self, url: str) -> None:
        """Play a URL to the PulseAudio sink via ffmpeg."""
        await self._ff_kill()

        args = [
            self._ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-i",
            url,
            "-vn",
        ]
        filters = []
        gain = 0.0 if self._mute else self._volume / 100
        if abs(gain - 1.0) > 1e-3:
            filters.append(f"volume={gain:.3f}")
        # Trailing silence so an A2DP speaker clips silence, not the real tail.
        filters.append(f"apad=pad_dur={_FF_TAIL_PAD}")
        args += ["-af", ",".join(filters)]
        args += ["-f", "pulse", "-device", await self._resolve_sink(), "akicon"]

        env = dict(os.environ)
        env.setdefault("PULSE_SERVER", _HAOS_PULSE)

        _LOGGER.debug("Starting ffmpeg: %s", " ".join(args))
        try:
            self._ff_proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as err:
            raise AkiconBridgeError(
                f"ffmpeg executable '{self._ffmpeg_path}' not found"
            ) from err

        self._state = "playing"

        # Catch an immediate failure (e.g. ffmpeg built without the pulse muxer,
        # or the audio server unreachable) and surface its message.
        await asyncio.sleep(_FF_START_GRACE)
        if self._ff_proc.returncode not in (None, 0):
            err_out = b""
            with contextlib.suppress(Exception):
                err_out = await asyncio.wait_for(self._ff_proc.stderr.read(), timeout=1)
            self._state = "idle"
            raise AkiconBridgeError(
                "ffmpeg could not play to PulseAudio: "
                f"{err_out.decode(errors='replace').strip() or 'exited immediately'}"
            )

        # Keep draining stderr so a full pipe can never stall ffmpeg partway
        # through a file (which would cut the audio off mid-playback).
        self._ff_stderr_task = asyncio.create_task(self._ff_drain(self._ff_proc.stderr))

    async def _ff_kill(self) -> None:
        """Terminate the ffmpeg process if running."""
        if self._ff_stderr_task is not None:
            self._ff_stderr_task.cancel()
            self._ff_stderr_task = None
        if self._ff_proc is not None and self._ff_proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._ff_proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._ff_proc.wait(), timeout=2)
        self._ff_proc = None

    @staticmethod
    async def _ff_drain(stream: asyncio.StreamReader) -> None:
        """Read ffmpeg stderr until EOF so its pipe can never fill and block."""
        with contextlib.suppress(Exception):
            while True:
                line = await stream.readline()
                if not line:
                    break
                _LOGGER.debug("ffmpeg: %s", line.decode(errors="replace").rstrip())

    def _ffmpeg_status(self) -> dict:
        """Normalised status for the ffmpeg engine."""
        if self._ff_proc is not None and self._ff_proc.returncode is not None:
            self._state = "idle"
        return {
            "state": self._state,
            "volume": self._volume,
            "mute": self._mute,
            "title": None,
            "duration": None,
            "position": None,
        }

    # ----------------------------------------------------------------- mpv impl

    def _mpv_running(self) -> bool:
        """Return True if mpv is up and its IPC socket exists."""
        return (
            self._proc is not None
            and self._proc.returncode is None
            and os.path.exists(self._sock)
        )

    async def _ensure_mpv(self) -> None:
        """Start the idle mpv process if it is not already running."""
        if self._mpv_running():
            return

        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._sock)

        args = [
            self._mpv_path,
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            "--really-quiet",
            "--vid=no",
            "--volume-max=100",
            f"--volume={self._volume}",
            f"--mute={'yes' if self._mute else 'no'}",
            f"--audio-device=pulse/{await self._resolve_sink()}",
            f"--input-ipc-server={self._sock}",
        ]

        _LOGGER.debug("Starting mpv: %s", " ".join(args))
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        for _ in range(_MPV_START_TIMEOUT * 10):
            if os.path.exists(self._sock):
                return
            await asyncio.sleep(0.1)
        raise AkiconBridgeError("mpv did not create its IPC socket in time")

    async def _rpc(self, commands: list[list]) -> list:
        """Run one or more mpv IPC commands, returning each reply's data."""
        async with self._lock:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(self._sock), timeout=_IPC_TIMEOUT
                )
            except (OSError, asyncio.TimeoutError) as err:
                raise AkiconBridgeError(f"mpv IPC socket unavailable: {err}") from err

            results: list = []
            try:
                for req_id, cmd in enumerate(commands, start=1):
                    payload = json.dumps({"command": cmd, "request_id": req_id}) + "\n"
                    writer.write(payload.encode())
                    await writer.drain()
                    results.append(await self._read_reply(reader, req_id))
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            return results

    @staticmethod
    async def _read_reply(reader: asyncio.StreamReader, req_id: int):
        """Read IPC lines until the reply matching req_id arrives."""
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=_IPC_TIMEOUT)
            except asyncio.TimeoutError:
                return None
            if not line:
                return None
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                continue
            if msg.get("request_id") == req_id and "error" in msg:
                if msg.get("error") == "success":
                    return msg.get("data")
                return None

    async def _mpv_status(self) -> dict:
        """Normalised status for the mpv engine."""
        if not self._mpv_running():
            return self._idle_status()

        props = [
            "idle-active",
            "pause",
            "eof-reached",
            "volume",
            "mute",
            "duration",
            "time-pos",
            "media-title",
        ]
        try:
            values = await self._rpc([["get_property", p] for p in props])
        except AkiconBridgeError:
            return self._idle_status()
        data = dict(zip(props, values))

        if data.get("idle-active") or data.get("eof-reached"):
            state = "idle"
        elif data.get("pause"):
            state = "paused"
        else:
            state = "playing"

        volume = data.get("volume")
        duration = data.get("duration")
        position = data.get("time-pos")
        return {
            "state": state,
            "volume": int(volume) if volume is not None else self._volume,
            "mute": bool(data.get("mute")),
            "title": data.get("media-title") if state != "idle" else None,
            "duration": int(duration) if duration else None,
            "position": int(position) if position is not None else None,
        }

    def _idle_status(self) -> dict:
        """A quiescent status carrying the remembered volume/mute."""
        return {
            "state": "idle",
            "volume": self._volume,
            "mute": self._mute,
            "title": None,
            "duration": None,
            "position": None,
        }

    # ----------------------------------------------------------------- helpers

    @staticmethod
    async def _run(*args: str, timeout: float = 5) -> tuple[int | None, str, str]:
        """Run a host command, returning (returncode, stdout, stderr)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as err:
            raise AkiconBridgeError(
                f"Command '{args[0]}' not found on the Home Assistant host"
            ) from err
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return None, "", "timed out"
        return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
