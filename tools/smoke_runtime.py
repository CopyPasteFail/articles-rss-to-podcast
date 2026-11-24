"""Check the runtime's tools and audio processing without external API calls."""

from __future__ import annotations

import importlib
import pathlib
import shutil
import subprocess  # nosec B404
import sys
import tempfile

from pydub import AudioSegment, effects
from pydub.generators import Sine


def tool_version(name: str, version_arg: str = "--version") -> str:
    """Run only a known runtime binary, with a short startup timeout."""
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f"Runtime is missing {name}")
    result = subprocess.run(  # nosec B603
        [path, version_arg], capture_output=True, text=True, check=True, timeout=20
    )
    version = result.stdout.strip().splitlines()[0]
    print(f"{name}: {version}")
    return version


def main() -> None:
    if sys.version_info[:2] != (3, 13):
        raise SystemExit("The podcast runtime requires Python 3.13")
    for name in (
        "pipeline",
        "one_episode",
        "upload_to_ia",
        "write_rss",
        "tts_usage",
    ):
        importlib.import_module(name)
    if not tool_version("node").startswith("v22."):
        raise SystemExit("The podcast runtime requires Node 22")
    tool_version("wrangler")
    tool_version("ffmpeg", "-version")
    tool_version("ffprobe", "-version")

    with tempfile.TemporaryDirectory(prefix="podcast-audio-smoke-") as directory:
        source_path = pathlib.Path(directory) / "source.mp3"
        output_path = pathlib.Path(directory) / "normalized.mp3"
        tone = Sine(440).to_audio_segment(duration=300).apply_gain(-20)
        tone.export(source_path, format="mp3").close()
        decoded = AudioSegment.from_file(source_path)
        normalized = effects.normalize(decoded)
        if normalized.max_dBFS <= decoded.max_dBFS:
            raise SystemExit(
                "Audio normalization did not increase the test tone's level"
            )
        normalized.export(output_path, format="mp3").close()
        if abs(len(AudioSegment.from_file(output_path)) - len(decoded)) > 50:
            raise SystemExit("MP3 round trip changed the audio duration")
    print("Runtime imports, binaries, and MP3 normalization passed")


if __name__ == "__main__":
    main()
