"""Shazam recognition wrapper plus the offset -> track-position math."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass
class Match:
    key: str            # Shazam track id -- stable identity for "same song?"
    title: str
    artist: str
    album: str = ""
    cover: str = ""
    offset: float | None = None   # Shazam's reported position anchor
    clip_seconds: float = config.CLIP_SECONDS

    @property
    def position_at_clip_end(self) -> float | None:
        """Track position at the instant the clip's final sample was *played*.

        Shazam's `offset` is where the fingerprinted window starts, and that
        window is centred in the clip -- so the clip's end is half a clip plus
        half a segment later. The capture buffer means those samples reached us
        slightly after they played, hence the latency term.
        """
        if self.offset is None:
            return None
        return (self.offset + self.clip_seconds / 2 + config.OFFSET_TAIL
                + config.CAPTURE_LATENCY)

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title


def _song_metadata(track: dict) -> dict:
    for section in track.get("sections") or []:
        if section.get("type") == "SONG":
            return {
                item.get("title"): item.get("text")
                for item in section.get("metadata") or []
                if item.get("title")
            }
    return {}


def _cover(track: dict) -> str:
    images = track.get("images") or {}
    return images.get("coverarthq") or images.get("coverart") or ""


def parse_response(payload: dict) -> Match | None:
    track = payload.get("track") or {}
    if not track:
        return None
    matches = payload.get("matches") or []
    offset = None
    if matches:
        raw = matches[0].get("offset")
        if isinstance(raw, (int, float)):
            offset = float(raw)
    meta = _song_metadata(track)
    return Match(
        key=str(track.get("key") or track.get("title")),
        title=track.get("title") or "",
        artist=track.get("subtitle") or "",
        album=meta.get("Album", "") or "",
        cover=_cover(track),
        offset=offset,
    )


class Recognizer:
    """Thin async wrapper -- shazamio is imported lazily so the UIs don't need it."""

    def __init__(self) -> None:
        self._shazam = None

    def _client(self):
        if self._shazam is None:
            from shazamio import Shazam  # imported here: heavy, daemon-only
            self._shazam = Shazam()
        return self._shazam

    async def recognize_file(self, path: Path) -> Match | None:
        payload = await self._client().recognize(str(path))
        return parse_response(payload)
