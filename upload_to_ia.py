#!/usr/bin/env python
import os, sys, pathlib, hashlib, json, time
import internetarchive
import requests

RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


def should_retry(exc: requests.exceptions.RequestException) -> bool:
    """Decide whether the raised request exception is safe to retry."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(exc, requests.exceptions.HTTPError):
        return status in RETRYABLE_STATUS_CODES
    return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))


def retry_delay(attempt: int, response) -> float:
    """Return the number of seconds to wait before the next retry, starting at ~5s and doubling per attempt (capped at 60s) while honoring Retry-After when present."""
    base = min(5 * (2 ** (attempt - 1)), 60)
    headers = getattr(response, "headers", {})
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            retry_after = float(retry_after)
            base = max(base, retry_after)
        except (TypeError, ValueError):
            pass
    return base


def upload_with_retries(item, files, *, metadata, verbose=False, max_attempts=5):
    """Upload files to IA, retrying transient errors with exponential backoff, delaying between attempts via retry_delay so we slow down when IA asks us to."""
    for attempt in range(1, max_attempts + 1):
        try:
            return item.upload(files, metadata=metadata, verbose=verbose)
        except requests.exceptions.RequestException as exc:
            if not should_retry(exc) or attempt == max_attempts:
                raise
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", "?")
            wait = retry_delay(attempt, response)
            print(
                f"Upload attempt {attempt} failed with status {status}: {exc}. "
                f"Retrying in {wait:.1f}s..."
            )
            time.sleep(wait)

def read_sidecar(mp3_path: pathlib.Path) -> dict:
    sidecar = mp3_path.with_suffix(mp3_path.suffix + ".rssmeta.json")
    if not sidecar.exists():
        sys.exit(f"Sidecar not found: {sidecar}")
    with open(sidecar, "r", encoding="utf-8") as f:
        return json.load(f)

def link_id(link: str) -> str:
    slug = os.getenv("PODCAST_SLUG", "default")
    h = hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]
    return f"tts-{slug}-{h}"

def get_ia_session():
    os.environ.pop("IA_CONFIG_FILE", None)
    ak = os.getenv("IA_ACCESS_KEY")
    sk = os.getenv("IA_SECRET_KEY")
    if ak and sk:
        print("Using IA credentials from env vars")
        return internetarchive.get_session(config={"s3": {"access": ak, "secret": sk}}, config_file="")
    print("Using default IA config from home dir")
    return internetarchive.get_session(config_file="")

def main():
    if len(sys.argv) < 2:
        print("Usage: python upload_to_ia.py path/to/file.mp3")
        sys.exit(2)

    mp3_path = pathlib.Path(sys.argv[1]).resolve()
    if not mp3_path.is_file():
        sys.exit(f"Not found: {mp3_path}")

    meta = read_sidecar(mp3_path)
    identifier = link_id(meta["article_link"])
    remote_name = "episode.mp3"

    session = get_ia_session()
    item = session.get_item(identifier)

    try:
        replacing = any(f.name == remote_name for f in item.get_files())
    except Exception:
        replacing = False
    print(f"{'Replacing' if replacing else 'Creating'} {remote_name} in {identifier}\n")

    to_upload = {remote_name: str(mp3_path)}
    max_attempts = os.getenv("IA_UPLOAD_RETRIES", "5")
    try:
        max_attempts = int(max_attempts)
    except ValueError:
        max_attempts = 5
    max_attempts = max(1, max_attempts)
    result = upload_with_retries(item, to_upload, metadata={
        "title": meta["article_title"],
        "mediatype": "audio",
        "language": "und",
        "creator": "Automated RSS to TTS",
        "description": "Auto-generated TTS episode",
        "subject": "podcast;tts;articles",
        "external-identifier": meta.get("article_link",""),
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
