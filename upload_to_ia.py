#!/usr/bin/env python
from __future__ import annotations  # keep compatibility with <3.11 lazy annotations

import hashlib
import json
import os
import pathlib
import sys
import time
from typing import Any, Iterable, Mapping, MutableMapping, Protocol, Sequence, cast

import internetarchive
import requests


class UploadResponse(Protocol):
    ok: bool


class ArchiveFile(Protocol):
    name: str


class ArchiveItem(Protocol):
    def upload(
        self,
        files: Mapping[str, str],
        *,
        metadata: Mapping[str, Any],
        verbose: bool = False,
    ) -> Sequence[UploadResponse]:
        ...

    def get_files(self) -> Iterable[ArchiveFile]:
        ...


class ArchiveSession(Protocol):
    def get_item(self, identifier: str) -> ArchiveItem:
        ...


def get_session(
    config: Mapping[str, Any] | None = None,
    config_file: str | None = None,
    debug: bool = False,
    http_adapter_kwargs: MutableMapping[str, Any] | None = None,
) -> ArchiveSession:
    ia = cast(Any, internetarchive)
    return cast(
        ArchiveSession,
        ia.get_session(
            config=config,
            config_file=config_file,
            debug=debug,
            http_adapter_kwargs=http_adapter_kwargs,
        ),
    )

RETRYABLE_STATUS_CODES: set[int] = {408, 425, 429, 500, 502, 503, 504}


def should_retry(exc: requests.exceptions.RequestException) -> bool:
    """Decide whether the raised request exception is safe to retry."""
    response: requests.Response | None = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(exc, requests.exceptions.HTTPError):
        return status in RETRYABLE_STATUS_CODES
    return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))


def retry_delay(attempt: int, response: requests.Response | None) -> float:
    """Return the number of seconds to wait before the next retry, starting at ~5s and doubling per attempt (capped at 60s) while honoring Retry-After when present."""
    base = min(5 * (2 ** (attempt - 1)), 60)
    headers: Mapping[str, str] | None = getattr(response, "headers", None)
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            retry_after = float(retry_after)
            base = max(base, retry_after)
        except (TypeError, ValueError):
            pass
    return base


def upload_with_retries(
    item: ArchiveItem,
    files: Mapping[str, str],
    *,
    metadata: Mapping[str, Any],
    verbose: bool = False,
    max_attempts: int = 5,
) -> Sequence[UploadResponse]:
    """Upload files to IA, retrying transient errors with exponential backoff, delaying between attempts via retry_delay so we slow down when IA asks us to."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    for attempt in range(1, max_attempts + 1):
        try:
            return item.upload(files, metadata=metadata, verbose=verbose)
        except requests.exceptions.RequestException as exc:
            if not should_retry(exc) or attempt == max_attempts:
                raise
            response: requests.Response | None = getattr(exc, "response", None)
            status = getattr(response, "status_code", "?")
            wait = retry_delay(attempt, response)
            print(
                f"Upload attempt {attempt} failed with status {status}: {exc}. "
                f"Retrying in {wait:.1f}s..."
            )
            time.sleep(wait)
    # This point should be unreachable, but keep mypy/Pylance happy.
    raise RuntimeError("upload_with_retries exhausted without returning or raising")

def read_sidecar(mp3_path: pathlib.Path) -> dict[str, Any]:
    sidecar = mp3_path.with_suffix(mp3_path.suffix + ".rssmeta.json")
    if not sidecar.exists():
        sys.exit(f"Sidecar not found: {sidecar}")
    with open(sidecar, "r", encoding="utf-8") as f:
        return json.load(f)

def link_id(link: str) -> str:
    slug = os.getenv("PODCAST_SLUG", "default")
    h = hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]
    return f"tts-{slug}-{h}"

def get_ia_session() -> ArchiveSession:
    os.environ.pop("IA_CONFIG_FILE", None)
    ak = os.getenv("IA_ACCESS_KEY")
    sk = os.getenv("IA_SECRET_KEY")
    if ak and sk:
        print("Using IA credentials from env vars")
        return get_session(config={"s3": {"access": ak, "secret": sk}}, config_file="")
    print("Using default IA config from home dir")
    return get_session(config_file="")

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python upload_to_ia.py path/to/file.mp3")
        sys.exit(2)

    mp3_path = pathlib.Path(sys.argv[1]).resolve()
    if not mp3_path.is_file():
        sys.exit(f"Not found: {mp3_path}")

    meta: dict[str, Any] = read_sidecar(mp3_path)
    identifier = link_id(meta["article_link"])
    remote_name = "episode.mp3"

    session = get_ia_session()
    item: ArchiveItem = session.get_item(identifier)

    try:
        replacing = any(f.name == remote_name for f in item.get_files())
    except Exception:
        replacing = False
    print(f"{'Replacing' if replacing else 'Creating'} {remote_name} in {identifier}\n")

    to_upload = {remote_name: str(mp3_path)}
    max_attempts_env = os.getenv("IA_UPLOAD_RETRIES", "5")
    try:
        max_attempts = int(max_attempts_env)
    except ValueError:
        max_attempts = 5
    max_attempts = max(1, max_attempts)
    result: Sequence[UploadResponse] = upload_with_retries(item, to_upload, metadata={
        "title": meta["article_title"],
        "mediatype": "audio",
        "language": "und",
        "creator": "Automated RSS to TTS",
        "description": "Auto-generated TTS episode",
        "subject": "podcast;tts;articles",
        "external-identifier": meta.get("article_link", ""),
    }, verbose=True, max_attempts=max_attempts)

    ok = all(getattr(r, "ok", False) for r in result)
    if not ok:
        print("Upload failed:")
        for r in result:
            if not getattr(r, "ok", False):
                print(r)
        sys.exit(1)

    url = f"https://archive.org/download/{identifier}/{remote_name}"
    print("OK:", url)

if __name__ == "__main__":
    main()
