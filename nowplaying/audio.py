"""Audio source discovery and fixed-length capture via PipeWire/PulseAudio."""
from __future__ import annotations

import array
import math
import subprocess
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass(frozen=True)
class Source:
    index: int
    name: str
    kind: str  # "monitor" (loopback of what we play) or "mic"

    @property
    def label(self) -> str:
        pretty = self.name.replace("alsa_output.", "").replace("alsa_input.", "")
        pretty = pretty.replace(".monitor", "")
        return f"{self.kind}: {pretty}"


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def list_sources() -> list[Source]:
    """All capture sources PulseAudio/PipeWire knows about."""
    out = []
    for line in _run(["pactl", "list", "short", "sources"]).splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        name = parts[1]
        out.append(Source(idx, name, "monitor" if name.endswith(".monitor") else "mic"))
    return out


def default_monitor() -> str | None:
    """The monitor of the default sink -- i.e. whatever the laptop is playing."""
    for line in _run(["pactl", "info"]).splitlines():
        if line.startswith("Default Sink:"):
            return line.split(":", 1)[1].strip() + ".monitor"
    return None


def default_mic() -> str | None:
    for line in _run(["pactl", "info"]).splitlines():
        if line.startswith("Default Source:"):
            name = line.split(":", 1)[1].strip()
            # The default source is sometimes itself a monitor; only accept a
            # real input here.
            if not name.endswith(".monitor"):
                return name
    for s in list_sources():
        if s.kind == "mic":
            return s.name
    return None


def sink_is_busy() -> bool:
    """True when some application is actually feeding the default sink."""
    return bool(_run(["pactl", "list", "short", "sink-inputs"]).strip())


def resolve_source(preference: str) -> tuple[str | None, str]:
    """Map a user preference to a concrete source name.

    preference: "auto" | "loopback" | "mic" | <explicit source name>
    Returns (source_name, reason).
    """
    if preference == "loopback":
        return default_monitor(), "loopback (forced)"
    if preference == "mic":
        return default_mic(), "mic (forced)"
    if preference != "auto":
        return preference, "explicit source"
    # auto: prefer the loopback when the laptop is actually playing something,
    # otherwise listen to the room.
    if sink_is_busy():
        mon = default_monitor()
        if mon:
            return mon, "loopback (sink is busy)"
    mic = default_mic()
    if mic:
        return mic, "mic (sink idle)"
    return default_monitor(), "loopback (no mic available)"


@dataclass
class Capture:
    raw: bytes
    rms: float
    end_wall: float  # time.time() at the instant the last sample was read
    seconds: float

    @property
    def complete(self) -> bool:
        return bool(self.raw) and self.seconds > 0


def capture(source: str, seconds: float, dest: Path | None = None) -> Capture:
    """Capture exactly `seconds` of mono 16 kHz PCM from `source`.

    Reading an exact byte count off parec's stdout keeps the clip length
    deterministic, which the offset math in `recognizer` depends on. `end_wall`
    is stamped the moment the final sample lands so the sync anchor is not
    polluted by WAV writing or network latency.
    """
    want = int(config.SAMPLE_RATE * config.CHANNELS * 2 * seconds)
    cmd = [
        "parec",
        f"--device={source}",
        "--format=s16le",
        f"--rate={config.SAMPLE_RATE}",
        f"--channels={config.CHANNELS}",
        f"--latency-msec={config.CAPTURE_LATENCY_MS}",
    ]
    buf = bytearray()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert proc.stdout is not None
        while len(buf) < want:
            chunk = proc.stdout.read(min(8192, want - len(buf)))
            if not chunk:
                break
            buf.extend(chunk)
        end_wall = time.time()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    raw = bytes(buf[:want])
    got_seconds = len(raw) / (config.SAMPLE_RATE * config.CHANNELS * 2)
    if dest is not None and raw:
        write_wav(raw, dest)
    return Capture(raw=raw, rms=_rms(raw), end_wall=end_wall, seconds=got_seconds)


class StreamCapture:
    """One long-lived capture feeding a rolling buffer.

    Opening and closing the audio device for every probe and every recognition
    makes the desktop's recording indicator blink once per cycle. Holding a
    single stream open instead means the indicator is simply on while we're
    listening -- honest, and far less distracting.
    """

    def __init__(self, source: str, seconds: float = 20.0) -> None:
        self.source = source
        self.capacity = int(config.SAMPLE_RATE * config.CHANNELS * 2 * seconds)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._end_wall = 0.0
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        if self.alive():
            return True
        self._stop.clear()
        cmd = [
            "parec", f"--device={self.source}", "--format=s16le",
            f"--rate={config.SAMPLE_RATE}", f"--channels={config.CHANNELS}",
            f"--latency-msec={config.CAPTURE_LATENCY_MS}",
        ]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL)
        except OSError:
            self._proc = None
            return False
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        return True

    def _reader(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while not self._stop.is_set():
            chunk = self._proc.stdout.read(4096)
            if not chunk:
                break
            with self._lock:
                self._buf.extend(chunk)
                if len(self._buf) > self.capacity:
                    del self._buf[:len(self._buf) - self.capacity]
                self._end_wall = time.time()

    def alive(self) -> bool:
        return (self._proc is not None and self._proc.poll() is None
                and self._thread is not None and self._thread.is_alive())

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        with self._lock:
            self._buf.clear()

    # --- reading -----------------------------------------------------------
    def seconds_buffered(self) -> float:
        with self._lock:
            return len(self._buf) / (config.SAMPLE_RATE * config.CHANNELS * 2)

    def snapshot(self, seconds: float) -> Capture | None:
        """The most recent `seconds` of audio, or None if not buffered yet."""
        want = int(config.SAMPLE_RATE * config.CHANNELS * 2 * seconds)
        with self._lock:
            if len(self._buf) < want or self._end_wall == 0.0:
                return None
            raw = bytes(self._buf[-want:])
            end = self._end_wall
        return Capture(raw=raw, rms=_rms(raw), end_wall=end, seconds=seconds)

    def level(self, seconds: float = 1.0) -> float:
        """Recent loudness, for silence detection -- costs no new stream."""
        want = int(config.SAMPLE_RATE * config.CHANNELS * 2 * seconds)
        with self._lock:
            if not self._buf:
                return 0.0
            raw = bytes(self._buf[-want:]) if len(self._buf) >= want else bytes(self._buf)
        return _rms(raw)


def _rms(raw: bytes) -> float:
    if not raw:
        return 0.0
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        return 0.0
    total = 0
    # Stride large clips; we only need a level estimate, not precision.
    step = max(1, len(samples) // 20000)
    count = 0
    for i in range(0, len(samples), step):
        v = samples[i]
        total += v * v
        count += 1
    return math.sqrt(total / count) / 32768.0


def write_wav(raw: bytes, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest), "wb") as w:
        w.setnchannels(config.CHANNELS)
        w.setsampwidth(2)
        w.setframerate(config.SAMPLE_RATE)
        w.writeframes(raw)
    return dest
