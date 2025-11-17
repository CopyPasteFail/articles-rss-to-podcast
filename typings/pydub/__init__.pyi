from __future__ import annotations

from os import PathLike
from typing import Any, IO


class AudioSegment:
    frame_rate: int
    channels: int
    sample_width: int

    def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    def __add__(self, other: "AudioSegment") -> "AudioSegment": ...

    @classmethod
    def from_file(
        cls,
        file: str | PathLike[str] | IO[bytes],
        format: str | None = ...,
        codec: str | None = ...,
        parameters: Any | None = ...,
        start_second: float | None = ...,
        duration: float | None = ...,
        **kwargs: Any,
    ) -> "AudioSegment": ...

    def export(
        self,
        out_f: str | PathLike[str] | IO[bytes],
        format: str = ...,
        bitrate: str | None = ...,
        tags: dict[str, str] | None = ...,
        id3v2_version: int | None = ...,
        **kwargs: Any,
    ) -> None: ...


class _EffectsModule:
    def normalize(self, segment: AudioSegment, headroom: float = ...) -> AudioSegment: ...


effects: _EffectsModule
