#!/usr/bin/env python
"""Upload generated MP3s to Internet Archive and print the public download URL."""

from __future__ import annotations  # keep compatibility with <3.11 lazy annotations

import hashlib
import json
import os
import pathlib
import random
import sys
import time
from typing import Any, Iterable, Mapping, MutableMapping, Protocol, Sequence, cast

import internetarchive
import requests


class UploadResponse(Protocol):
    """Subset of the IA response that matters for our success checks."""

    ok: bool


class ArchiveFile(Protocol):
    """Protocol describing the file objects returned by internetarchive."""

    name: str


class ArchiveItem(Protocol):
    """Minimal surface from internetarchive.Item that we actually call."""

    def upload(
        self,
        files: Mapping[str, str],
        *,
        metadata: Mapping[str, Any],
        verbose: bool = False,
        request_kwargs: Mapping[str, Any] | None = None,
    ) -> Sequence[UploadResponse]:
        ...

    def get_files(self) -> Iterable[ArchiveFile]:
        ...


class ArchiveSession(Protocol):
    """internetarchive.get_session interface (real or mocked)."""

    def get_item(
        self,
        identifier: str,
        item_metadata: Mapping[str, Any] | None = None,
        request_kwargs: MutableMapping[str, Any] | None = None,
    ) -> ArchiveItem:
        ...


def get_session(
    config: Mapping[str, Any] | None = None,
    config_file: str | None = None,
    debug: bool = False,
    http_adapter_kwargs: MutableMapping[str, Any] | None = None,
) -> ArchiveSession:
    """Wrapper around internetarchive.get_session so tests can stub it easily."""
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
    """Return the number of seconds to wait before the next retry.

    Starts at ~10s and doubles per attempt (capped at 300s normally), honoring
    Retry-After headers. When IA says "slow down"/"reduce your request rate",
    we start at 5 minutes and double up to ~20 minutes to respect their queue
    limits. Adds a little jitter so concurrent uploads do not retry in lockstep.
    """
    base = min(10 * (2 ** (attempt - 1)), 300)
    body = getattr(response, "text", "") if response is not None else ""
    slow_down = isinstance(body, str) and (
        "slow down" in body.lower() or "reduce your request rate" in body.lower()
    )
    if slow_down:
        # Start at 5 minutes and double, capped at 20 minutes, when IA tells us to back off.
        base = min(max(base, 300 * (2 ** (attempt - 1))), 1200)
    headers: Mapping[str, str] | None = getattr(response, "headers", None)
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            retry_after = float(retry_after)
            base = max(base, retry_after)
        except (TypeError, ValueError):
            pass
    return base * random.uniform(0.9, 1.1)


def wait_with_progress(seconds: float) -> None:
    """Sleep while printing occasional countdown updates so long waits are visible."""
    remaining = max(0.0, seconds)
    if remaining <= 0:
        return
    print(f"Waiting {remaining:.1f}s before retry...", flush=True)
    interval = 10.0 if remaining > 30 else 5.0
    while remaining > 0:
        sleep_for = min(interval, remaining)
        time.sleep(sleep_for)
        remaining -= sleep_for
        if remaining > 0:
            print(f"... {int(remaining)}s remaining", flush=True)


def upload_with_retries(
    item: ArchiveItem,
    files: Mapping[str, str],
    *,
    metadata: Mapping[str, Any],
    verbose: bool = False,
    max_attempts: int = 5,
    request_kwargs: Mapping[str, Any] | None = None,
) -> Sequence[UploadResponse]:
    """Upload files to IA, retrying transient errors with exponential backoff, delaying between attempts via retry_delay so we slow down when IA asks us to."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    request_kwargs = request_kwargs or {}
    for attempt in range(1, max_attempts + 1):
        try:
            print(
                f"Upload attempt {attempt}/{max_attempts} with timeout "
                f"{request_kwargs.get('timeout', 'default')}...",
                flush=True,
            )
            return item.upload(
                files,
                metadata=metadata,
                verbose=verbose,
                request_kwargs=request_kwargs,
            )
        except requests.exceptions.RequestException as exc:
            if not should_retry(exc) or attempt == max_attempts:
                raise
            response: requests.Response | None = getattr(exc, "response", None)
            status = getattr(response, "status_code", "?")
            wait = retry_delay(attempt, response)
            print(
                f"Upload attempt {attempt} failed with status {status}: {exc}. "
                f"Retrying in {wait:.1f}s...",
                flush=True,
            )
            wait_with_progress(wait)
    # This point should be unreachable, but keep mypy/Pylance happy.
    raise RuntimeError("upload_with_retries exhausted without returning or raising")


def get_item_with_retries(
    session: ArchiveSession,
    identifier: str,
    *,
    max_attempts: int,
    request_kwargs: Mapping[str, Any],
) -> ArchiveItem:
    """Fetch item metadata with retries so occasional IA timeouts don't fail the pipeline."""
    for attempt in range(1, max_attempts + 1):
        try:
            print(
                f"Metadata attempt {attempt}/{max_attempts} with timeout "
                f"{request_kwargs.get('timeout', 'default')}...",
                flush=True,
            )
            return session.get_item(identifier, request_kwargs=dict(request_kwargs))
        except requests.exceptions.RequestException as exc:
            if not should_retry(exc) or attempt == max_attempts:
                raise
            response: requests.Response | None = getattr(exc, "response", None)
            status = getattr(response, "status_code", "?")
            wait = retry_delay(attempt, response)
            print(
                f"Metadata attempt {attempt} failed with status {status}: {exc}. "
                f"Retrying in {wait:.1f}s...",
                flush=True,
            )
            wait_with_progress(wait)
    raise RuntimeError("get_item_with_retries exhausted without returning or raising")


def read_sidecar(mp3_path: pathlib.Path) -> dict[str, Any]:
    """Load the JSON metadata produced alongside the MP3."""
    sidecar = mp3_path.with_suffix(mp3_path.suffix + ".rssmeta.json")
    if not sidecar.exists():
        sys.exit(f"Sidecar not found: {sidecar}")
    with open(sidecar, "r", encoding="utf-8") as f:
        return json.load(f)

def link_id(link: str) -> str:
    """Generate deterministic IA identifiers per-article so reruns overwrite safely."""
    slug = os.getenv("PODCAST_SLUG", "default")
    h = hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]
    return f"tts-{slug}-{h}"

def get_ia_session() -> ArchiveSession:
    """Decide whether to use explicit env credentials or fall back to local config."""
    os.environ.pop("IA_CONFIG_FILE", None)
    ak = os.getenv("IA_ACCESS_KEY")
    sk = os.getenv("IA_SECRET_KEY")
    if ak and sk:
        print("Using IA credentials from env vars")
        return get_session(config={"s3": {"access": ak, "secret": sk}}, config_file="")
    print("Using default IA config from home dir")
    return get_session(config_file="")

def main() -> None:
    """CLI entry point invoked by pipeline.py right before RSS regeneration."""
    if len(sys.argv) < 2:
        print("Usage: python upload_to_ia.py path/to/file.mp3")
        sys.exit(2)

    mp3_path = pathlib.Path(sys.argv[1]).resolve()
    if not mp3_path.is_file():
        sys.exit(f"Not found: {mp3_path}")

    meta: dict[str, Any] = read_sidecar(mp3_path)
    identifier = link_id(meta["article_link"])
    remote_name = "episode.mp3"

    max_attempts_env = os.getenv("IA_UPLOAD_RETRIES", "8")
    try:
        max_attempts = int(max_attempts_env)
    except ValueError:
        max_attempts = 5
    max_attempts = max(1, max_attempts)
    read_timeout_env = os.getenv("IA_UPLOAD_TIMEOUT", "10")
    try:
        read_timeout = float(read_timeout_env)
    except ValueError:
        read_timeout = 300.0
    request_kwargs = {"timeout": (15.0, max(30.0, read_timeout))}
    print(
        f"Using IA request timeouts connect={request_kwargs['timeout'][0]}s "
        f"read={request_kwargs['timeout'][1]}s\n",
        flush=True,
    )

    session = get_ia_session()
    item: ArchiveItem = get_item_with_retries(
        session,
        identifier,
        max_attempts=max_attempts,
        request_kwargs=request_kwargs,
    )

    try:
        replacing = any(f.name == remote_name for f in item.get_files())
    except Exception:
        replacing = False
    print(f"{'Replacing' if replacing else 'Creating'} {remote_name} in {identifier}\n")

    to_upload = {remote_name: str(mp3_path)}
    result: Sequence[UploadResponse] = upload_with_retries(item, to_upload, metadata={
        "title": meta["article_title"],
        "mediatype": "audio",
        "language": "und",
        "creator": "Automated RSS to TTS",
        "description": "Auto-generated TTS episode",
        "subject": "podcast;tts;articles",
        "external-identifier": meta.get("article_link", ""),
    }, verbose=True, max_attempts=max_attempts, request_kwargs=request_kwargs)

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
