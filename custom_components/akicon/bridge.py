"""Audio bridge for the Akicon Bath Fan Speaker.

The AK-SNP80-W speaker is a plain Bluetooth A2DP sink: it has no network
interface and no API. To use it like a Home Assistant media player we have to
do two things on the host that runs Home Assistant:

1. Hold a Bluetooth connection to the speaker (via ``bluetoothctl``).
2. Decode media and push the audio to the speaker's audio sink (via ``mpv``).

``mpv`` runs as a single long-lived, idle process and is driven over its JSON
IPC socket, which gives us real transport control: load a URL, pause, resume,
stop, set volume, and read back position/duration/title.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil

_LOGGER = logging.getLogger(__name__)

# Timeouts (seconds)
_CONNECT_TIMEOUT = 20
_IPC_TIMEOUT = 3
_MPV_START_TIMEOUT = 5


class AkiconBridgeError(Exception):
    """Raised when the bridge cannot talk to mpv or the speaker."""


class AkiconBridge:
    """Manage the Bluetooth connection and the mpv audio pipeline."""

    def __init__(
        self,
        mac: str,
        audio_device: str | None = None,
        mpv_path: str = "mpv",
    ) -> None:
        """Set up the bridge for one speaker."""
        self._mac = mac.upper()
        self._audio_device = audio_device or None
        self._mpv_path = mpv_path
        # A per-speaker IPC socket, keyed by the sanitised MAC.
        safe = self._mac.replace(":", "")
        self._sock = f"/tmp/akicon-{safe}.sock"
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    @property
    def mac(self) -> str:
        """Return the speaker's Bluetooth MAC."""
        return self._mac

    # ---------------------------------------------------------------- Bluetooth

    async def async_is_connected(self) -> bool:
        """Return True if bluetoothctl reports the speaker as connected."""
        code, out, _ = await self._run("bluetoothctl", "info", self._mac)
        if code != 0:
            return False
        return "Connected: yes" in out

    async def async_connect(self) -> None:
        """Connect to the speaker over Bluetooth, if not already connected."""
        if await self.async_is_connected():
            return
        code, out, err = await self._run(
            "bluetoothctl", "connect", self._mac, timeout=_CONNECT_TIMEOUT
        )
        combined = f"{out}\n{err}"
        if code == 0 and (
            "Connection successful" in combined or "already connected" in combined.lower()
        ):
            return
        # bluetoothctl sometimes returns 0 without the success banner; verify.
        if await self.async_is_connected():
            return
        raise AkiconBridgeError(
            f"Could not connect to {self._mac}. Pair and trust it once with "
            f"bluetoothctl before using the integration. Output: {combined.strip()}"
        )

    async def async_disconnect(self) -> None:
        """Disconnect the speaker over Bluetooth."""
        await self._run("bluetoothctl", "disconnect", self._mac, timeout=_CONNECT_TIMEOUT)

    # --------------------------------------------------------------------- mpv

    async def async_ensure_ready(self) -> None:
        """Make sure the speaker is connected and mpv is running."""
        await self.async_connect()
        await self._ensure_mpv()

    async def _ensure_mpv(self) -> None:
        """Start the idle mpv process if it is not already running."""
        if self._proc is not None and self._proc.returncode is None:
            return

        if shutil.which(self._mpv_path) is None:
            raise AkiconBridgeError(
                f"mpv executable '{self._mpv_path}' not found on the Home "
                "Assistant host. Install mpv (e.g. 'apt install mpv')."
            )

        # A stale socket from a crashed process blocks a clean restart.
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
            f"--input-ipc-server={self._sock}",
        ]
        if self._audio_device:
            args.append(f"--audio-device={self._audio_device}")

        _LOGGER.debug("Starting mpv: %s", " ".join(args))
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Wait for the IPC socket to appear.
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
        """Read IPC lines until the reply matching req_id arrives.

        mpv interleaves asynchronous ``event`` lines with command replies on the
        same socket. Replies carry an ``error`` field and echo the request_id.
        """
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

    # ----------------------------------------------------------- transport API

    async def async_play_url(self, url: str) -> None:
        """Load and play a URL, replacing anything currently playing."""
        await self.async_ensure_ready()
        await self._rpc(
            [
                ["loadfile", url, "replace"],
                ["set_property", "pause", False],
            ]
        )

    async def async_pause(self) -> None:
        """Pause playback."""
        await self._rpc([["set_property", "pause", True]])

    async def async_resume(self) -> None:
        """Resume playback."""
        await self._rpc([["set_property", "pause", False]])

    async def async_stop(self) -> None:
        """Stop playback and clear the playlist."""
        await self._rpc([["stop"]])

    async def async_set_volume(self, level: float) -> None:
        """Set volume from a 0.0-1.0 Home Assistant level."""
        volume = max(0, min(100, round(level * 100)))
        await self._rpc([["set_property", "volume", volume]])

    async def async_set_mute(self, mute: bool) -> None:
        """Mute or unmute."""
        await self._rpc([["set_property", "mute", mute]])

    async def async_status(self) -> dict:
        """Return a snapshot of mpv's playback state.

        Returns an empty dict if mpv is not running yet, so the entity can
        report an idle/off state without raising.
        """
        if self._proc is None or self._proc.returncode is not None:
            return {}
        if not os.path.exists(self._sock):
            return {}

        props = [
            "idle-active",
            "pause",
            "eof-reached",
            "volume",
            "mute",
            "duration",
            "time-pos",
            "media-title",
            "path",
        ]
        try:
            values = await self._rpc([["get_property", p] for p in props])
        except AkiconBridgeError:
            return {}
        return dict(zip(props, values))

    async def async_shutdown(self) -> None:
        """Quit mpv. Leaves the Bluetooth connection alone."""
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(Exception):
                await self._rpc([["quit"]])
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._proc.wait(), timeout=3)
                self._proc = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._sock)

    # ----------------------------------------------------------------- helpers

    @staticmethod
    async def _run(
        *args: str, timeout: float = 5
    ) -> tuple[int | None, str, str]:
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
