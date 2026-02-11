#!/usr/bin/env python
"""End-to-end pipeline that turns an RSS feed into fully hosted podcast episodes."""

from __future__ import annotations

import datetime
import hashlib
import importlib
import json
import os
import pathlib
import re
import random
import random
import shutil
import subprocess
import sys
import tempfile
import time
import io
from collections.abc import Mapping
from typing import Any, Protocol, TypedDict, cast

import requests

try:
    stdout = sys.stdout
    if isinstance(stdout, io.TextIOWrapper):
        stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from content_utils import resolve_article_content, text_to_html

StrPath = str | os.PathLike[str]
JSONDict = dict[str, Any]
EntryDict = dict[str, Any]
StateItems = dict[str, JSONDict]


class FeedParserDict(dict[str, Any]):
    """Narrow type so the pipeline knows feedparser returns an object with entries."""

    entries: list[Any]


class BillingGroup(TypedDict):
    """Minimal billing snapshot for a specific Google TTS price tier."""

    characters: int
    free_tier_remaining: int


class BillingUsage(TypedDict, total=False):
    """Structured response used when printing TTS billing info at the end."""

    summary: JSONDict
    by_group: list[JSONDict]


def _ensure_json_dict(value: object) -> JSONDict:
    """Keep the persisted pipeline state predictable by forcing dict-like data."""
    if isinstance(value, dict):
        return cast(JSONDict, value)
    return {}


def _empty_billing_group() -> BillingGroup:
    """Return a zeroed-out billing group so we can safely access fields later."""
    return BillingGroup(characters=0, free_tier_remaining=0)


class FeedparserModule(Protocol):
    def parse(
        self,
        url_file_stream_or_string: str,
        etag: Any | None = ...,
        modified: Any | None = ...,
        agent: Any | None = ...,
        referrer: Any | None = ...,
        handlers: Any | None = ...,
        request_headers: Any | None = ...,
        response_headers: Any | None = ...,
        resolve_relative_uris: bool | None = ...,
        sanitize_html: bool | None = ...,
    ) -> FeedParserDict: ...


_feedparser_module: FeedparserModule | None = None


def _get_feedparser() -> FeedparserModule:
    """Lazy-load feedparser so short-lived CLI invocations stay snappy."""
    global _feedparser_module
    if _feedparser_module is None:
        module = importlib.import_module("feedparser")
        _feedparser_module = cast(FeedparserModule, module)
    return _feedparser_module


def _fetch_rss_payload(
    rss_url: str,
    *,
    http_get: Any = requests.get,
) -> tuple[bytes | None, str | None]:
    """Fetch RSS payload over HTTP(S) and classify common CDN/origin failures.

    Inputs: rss_url from RSS_URL and an injectable http_get for testing.
    Outputs: (payload bytes, error message) where payload is None on failure.
    Edge cases: non-HTTP URLs, timeouts, Cloudflare status codes, or unknown 4xx/5xx.
    """
    if not rss_url.startswith(("http://", "https://")):
        return None, None
    headers = {
        "User-Agent": RSS_HTTP_USER_AGENT,
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    }
    source_feed_context = f"source RSS feed '{rss_url}'"
    try:
        response = http_get(
            rss_url,
            headers=headers,
            timeout=(RSS_HTTP_CONNECT_TIMEOUT_S, RSS_HTTP_READ_TIMEOUT_S),
        )
    except requests.Timeout:
        return (
            None,
            f"Failed to fetch {source_feed_context}: request timed out while waiting for the "
            "origin server response (possible Cloudflare 524 or slow origin).",
        )
    except requests.RequestException as exc:
        return None, f"Failed to fetch {source_feed_context} before parsing: {exc}"

    status_code = response.status_code
    server_header = (response.headers.get("server") or "").lower()
    is_cloudflare = "cloudflare" in server_header

    if status_code in RSS_HTTP_CLOUDFLARE_STATUS_CODES and is_cloudflare:
        return (
            None,
            f"Failed to fetch {source_feed_context}: Cloudflare {status_code} (origin error).",
        )
    if status_code in RSS_HTTP_BLOCK_STATUS_CODES and is_cloudflare:
        return (
            None,
            f"Failed to fetch {source_feed_context}: blocked at Cloudflare (HTTP {status_code}).",
        )
    if status_code >= 400:
        return None, f"Failed to fetch {source_feed_context}: HTTP {status_code}."
    return response.content, None


def _payload_looks_like_html(payload: bytes) -> bool:
    """Return True when the RSS payload appears to be HTML instead of XML."""
    try:
        text = payload.decode("utf-8", errors="replace")
    except Exception:
        return False
    snippet = text.lstrip()[:200].lower()
    return snippet.startswith("<!doctype html") or snippet.startswith("<html")

# Root folders used throughout the flow (generation -> upload -> deploy).
ROOT = pathlib.Path(__file__).resolve().parent
OUT = pathlib.Path(os.getenv("OUT_DIR", "./out")).resolve()
PUBLIC = (ROOT / "public").resolve()

WRANGLER_BINARY_NAME = "wrangler"
LOCAL_WRANGLER_PATH = (ROOT / "node_modules" / ".bin" / WRANGLER_BINARY_NAME).resolve()
_has_warned_about_global_wrangler_fallback = False

PY = sys.executable

# Cloudflare vars
CF_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CF_API_TOKEN  = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
CF_PAGES_PROJECT = os.getenv("CF_PAGES_PROJECT", "tts-podcast-feeds").strip()
CF_KV_NAMESPACE_NAME = os.getenv("CF_KV_NAMESPACE_NAME", "tts-podcast-state").strip()
_cf_kv_namespace_id = os.getenv("CF_KV_NAMESPACE_ID", "").strip()

SLUG = os.getenv("PODCAST_SLUG", "default").strip()
IA_ID_PREFIX = os.getenv("IA_ID_PREFIX", SLUG).strip() or SLUG
RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_HTTP_USER_AGENT = "tts-podcast-rss-fetcher/1.0"
RSS_HTTP_CONNECT_TIMEOUT_S = 10.0
RSS_HTTP_READ_TIMEOUT_S = 20.0
RSS_HTTP_CLOUDFLARE_STATUS_CODES = {520, 521, 522, 523, 524, 525, 526}
RSS_HTTP_BLOCK_STATUS_CODES = {403, 429, 503}
FAILURE_MESSAGE_MAX_CHARS = 500
FAILURE_TRUNCATION_SUFFIX = "...[truncated]"
FAILED_ENTRY_AT_UTC_KEY = "last_failure_at_utc"
FAILED_ENTRY_PUB_UTC_KEY = "last_failure_pub_utc"
FAILED_ENTRY_STEP_KEY = "last_failure_step"
FAILED_ENTRY_MESSAGE_KEY = "last_failure_message"
FAILED_ENTRY_IDENTIFIER_KEY = "last_failure_identifier"
FAILED_ENTRY_LINK_KEY = "last_failure_link"
FAILED_ENTRY_KEYS = (
    FAILED_ENTRY_AT_UTC_KEY,
    FAILED_ENTRY_PUB_UTC_KEY,
    FAILED_ENTRY_STEP_KEY,
    FAILED_ENTRY_MESSAGE_KEY,
    FAILED_ENTRY_IDENTIFIER_KEY,
    FAILED_ENTRY_LINK_KEY,
)
FAILURE_STEP_GENERATE_AUDIO = "one_episode"
RETRY_FAILED_ENV_NAME = "PODCAST_RETRY_FAILED"
RETRY_FAILED_VALUES = {"1", "true", "yes", "y"}


def sh(*args: object, env: Mapping[str, str] | None = None, cwd: StrPath | None = None) -> str:
    """Run a subprocess, streaming output live while still capturing it for parsing."""
    print("→", " ".join(map(str, args)))
    env_map = os.environ.copy()
    if env:
        env_map.update(env)
    env_map.setdefault("PYTHONUNBUFFERED", "1")
    cmd = [str(a) for a in args]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env_map,
        cwd=cwd,
        bufsize=1,
    )
    assert proc.stdout is not None  # appease type checkers
    out_lines: list[str] = []
    try:
        for line in proc.stdout:
            print(line.rstrip())
            out_lines.append(line)
    finally:
        proc.stdout.close()
    proc.wait()
    out = "".join(out_lines)
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out)
    return out


def _truncate_text(value: str, limit: int) -> str:
    """Clamp long strings so persisted error messages stay readable and bounded.

    Inputs: value string and max length limit.
    Outputs: original string or truncated string with a suffix.
    Edge cases: empty strings, non-positive limits, already-short values.
    """
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    suffix = FAILURE_TRUNCATION_SUFFIX
    if limit <= len(suffix):
        return suffix[:limit]
    max_prefix_len = limit - len(suffix)
    trimmed = value[:max_prefix_len].rstrip()
    return f"{trimmed}{suffix}"


def _is_retry_failed_enabled() -> bool:
    """Check whether the caller wants to retry entries marked as failed.

    Inputs: none (reads RETRY_FAILED_ENV_NAME from the environment).
    Outputs: True when retries are enabled, otherwise False.
    Edge cases: unset or blank env var, mixed-case truthy values.
    """
    raw_value = os.getenv(RETRY_FAILED_ENV_NAME, "").strip().lower()
    return raw_value in RETRY_FAILED_VALUES


def _should_skip_failed_entry(
    entry_state: JSONDict,
    entry_pub_utc: str,
    retry_failed_entries: bool,
) -> bool:
    """Determine whether to skip a previously failed entry for the same publication time.

    Inputs: entry_state dict, entry_pub_utc string, and retry flag.
    Outputs: True when the entry should be skipped, False otherwise.
    Edge cases: missing failure metadata, empty publication timestamps.
    """
    if retry_failed_entries:
        return False
    last_failed_pub = str(entry_state.get(FAILED_ENTRY_PUB_UTC_KEY, ""))
    return bool(last_failed_pub and entry_pub_utc and last_failed_pub == entry_pub_utc)


def _record_entry_failure(
    entry_state: JSONDict,
    *,
    identifier: str,
    link: str,
    entry_pub_utc: str,
    step: str,
    message: str,
) -> None:
    """Annotate entry_state with failure metadata for later diagnostics and skipping.

    Inputs: entry_state dict (mutated), identifier, link, entry_pub_utc, step name, message.
    Outputs: None (mutates entry_state in place).
    Edge cases: empty message or timestamps; caller is responsible for persistence.
    Atomicity: not thread-safe; caller must persist in KV after mutation.
    """
    failure_timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    entry_state[FAILED_ENTRY_AT_UTC_KEY] = failure_timestamp
    entry_state[FAILED_ENTRY_PUB_UTC_KEY] = entry_pub_utc
    entry_state[FAILED_ENTRY_STEP_KEY] = step
    entry_state[FAILED_ENTRY_MESSAGE_KEY] = message
    entry_state[FAILED_ENTRY_IDENTIFIER_KEY] = identifier
    entry_state[FAILED_ENTRY_LINK_KEY] = link


def _clear_entry_failure(entry_state: JSONDict) -> None:
    """Remove any stored failure metadata after a successful processing run.

    Inputs: entry_state dict (mutated).
    Outputs: None (removes keys when present).
    Edge cases: keys already absent; no error raised.
    Atomicity: not thread-safe; caller must persist in KV after mutation.
    """
    for key in FAILED_ENTRY_KEYS:
        entry_state.pop(key, None)


def git_info() -> tuple[str | None, str | None]:
    """Return branch + commit so deploys can label the Cloudflare Pages run."""
    try:
        branch = sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=str(ROOT)).strip()
        commit = sh("git", "rev-parse", "HEAD", cwd=str(ROOT)).strip()
        return branch, commit
    except Exception:
        return None, None

def resolve_wrangler_path() -> str | None:
    """Resolve the Wrangler CLI path used by deploy/KV fallback commands.

    Inputs: none (reads repository layout and PATH).
    Outputs: absolute path to Wrangler, or None when unavailable.
    Edge cases: if local repo dependencies are missing, fall back to global PATH and emit
    a warning so operators know a non-pinned version may be used.
    """
    if LOCAL_WRANGLER_PATH.exists():
        return str(LOCAL_WRANGLER_PATH)
    system_path = shutil.which(WRANGLER_BINARY_NAME)
    if system_path:
        global _has_warned_about_global_wrangler_fallback
        if not _has_warned_about_global_wrangler_fallback:
            print(
                "[warn] Local Wrangler not found at "
                f"{LOCAL_WRANGLER_PATH}; falling back to global PATH binary: {system_path}"
            )
            _has_warned_about_global_wrangler_fallback = True
        return system_path
    return None

# ---------- Cloudflare KV helpers ----------
def _kv_base() -> str:
    """Base Cloudflare KV URL used by the state helpers below."""
    if not (CF_ACCOUNT_ID and CF_API_TOKEN):
        raise SystemExit("Missing CLOUDFLARE_API_TOKEN or CF_ACCOUNT_ID")
    return f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces"

def ensure_kv_namespace_id() -> str:
    """Ensure we know the KV namespace id so we can store per-feed progress."""
    global _cf_kv_namespace_id
    if _cf_kv_namespace_id:
        return _cf_kv_namespace_id
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    # list
    r = requests.get(_kv_base(), headers=headers, timeout=15)
    if r.ok:
        for ns in r.json().get("result", []):
            if ns.get("title") == CF_KV_NAMESPACE_NAME:
                _cf_kv_namespace_id = ns["id"]
                print(f"[cf] Using existing KV '{CF_KV_NAMESPACE_NAME}' id={_cf_kv_namespace_id}")
                return _cf_kv_namespace_id
    # create
    r = requests.post(_kv_base(), headers=headers, json={"title": CF_KV_NAMESPACE_NAME}, timeout=15)
    if not r.ok:
        raise SystemExit(f"Failed to create KV namespace: {r.status_code} {r.text[:200]}")
    _cf_kv_namespace_id = r.json()["result"]["id"]
    print(f"[cf] Created KV '{CF_KV_NAMESPACE_NAME}' id={_cf_kv_namespace_id}")
    return _cf_kv_namespace_id

def kv_url(key: str) -> str:
    """Build the REST endpoint for a specific KV key."""
    return f"{_kv_base()}/{ensure_kv_namespace_id()}/values/{key}"


def kv_get(key: str) -> JSONDict | None:
    """Read JSON pipeline state for the feed from Cloudflare KV."""
    try:
        r = requests.get(kv_url(key), headers={"Authorization": f"Bearer {CF_API_TOKEN}"}, timeout=15)
        if r.status_code == 200:
            return json.loads(r.text) if r.text else {}
        if r.status_code == 404:
            return None
        print(f"[kv] GET {key} -> {r.status_code}")
        return None
    except Exception as e:
        print(f"[kv] GET error: {e}")
        return None

def kv_put_via_wrangler(key: str, data: JSONDict) -> bool:
    """Fallback KV writer using wrangler CLI when direct API calls keep timing out."""
    wrangler_path = resolve_wrangler_path()
    if not wrangler_path:
        return False

    tmp_path: str | None = None
    try:
        tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8")
        with tmp:
            json.dump(data, tmp, ensure_ascii=False)
            tmp_path = tmp.name

        ns_id = ensure_kv_namespace_id()
        cmd = [
            wrangler_path,
            "kv",
            "key",
            "put",
            "--remote",
            "--namespace-id",
            ns_id,
            key,
            "--path",
            tmp_path,
        ]
        print(f"[kv] Falling back to wrangler for {key} (namespace {ns_id})")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            print(f"[kv] Wrangler PUT {key} ok")
            return True
        err_out = proc.stderr.strip() or proc.stdout.strip()
        print(f"[kv] Wrangler put failed ({proc.returncode}): {err_out}")
    except Exception as e:
        print(f"[kv] Wrangler put error: {e}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    return False


def kv_put(key: str, data: JSONDict) -> bool:
    """Store mutable pipeline state back into Cloudflare KV with simple retries/backoff, then wrangler fallback."""
    attempts = 4
    backoff_s = 2.0
    base_timeout = 15.0
    for attempt in range(1, attempts + 1):
        try:
            timeout = base_timeout * (2 ** (attempt - 1))
            r = requests.put(
                kv_url(key),
                headers={"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"},
                data=json.dumps(data, ensure_ascii=False),
                timeout=timeout,
            )
            if r.status_code in (200, 204):
                print(f"[kv] PUT {key} ok (attempt {attempt}, timeout={timeout}s)")
                return True
            print(f"[kv] PUT {key} -> {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"[kv] PUT error attempt {attempt}/{attempts}: {e}")

        if attempt < attempts:
            delay = max(0.5, backoff_s * (2 ** (attempt - 1)))
            jitter = delay * 0.25
            delay += random.uniform(-jitter, jitter)
            print(f"[kv] Retry {attempt + 1}/{attempts} in {delay:.1f}s...")
            time.sleep(delay)

    # Last-resort fallback via wrangler CLI (uses local auth config / env)
    return kv_put_via_wrangler(key, data)


def kv_put_or_die(key: str, data: JSONDict) -> None:
    """Fail fast if we cannot persist state to KV, to avoid duplicate processing."""
    if kv_put(key, data):
        return
    raise SystemExit("KV update failed; aborting to avoid duplicates")

# ---------- IA helpers ----------
def link_hash(link: str) -> str:
    """Return a stable short hash used anywhere we need deterministic filenames."""
    return hashlib.sha1(link.encode("utf-8")).hexdigest()

def ia_identifier_for_link(link: str) -> str:
    """Namespace the link hash so uploads land under unique IA identifiers."""
    return f"tts-{IA_ID_PREFIX}-{link_hash(link)[:16]}"

def ia_has_episode_http(identifier: str) -> bool:
    """Quickly check whether IA already hosts audio for this entry."""
    url = f"https://archive.org/download/{identifier}/episode.mp3"
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        return r.status_code == 200
    except Exception:
        return False

# ---------- RSS fetch helpers ----------
def _entry_from_feed(e: Any) -> EntryDict:
    """Map feedparser entries to the schema expected by the rest of the pipeline."""
    link = getattr(e, "link", None) or getattr(e, "id", None)
    title = getattr(e, "title", link)
    plain_text, html_content, subtitle, lead_image = resolve_article_content(e, link, allow_fetch=False)
    summary = plain_text or getattr(e, "summary", "") or getattr(e, "description", "") or ""
    if not html_content and summary:
        html_content = text_to_html(summary)
    tstruct = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    if tstruct:
        pub_utc = datetime.datetime(*tstruct[:6], tzinfo=datetime.timezone.utc).isoformat()
    else:
        pub_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    author = getattr(e, "author", "") or getattr(e, "creator", "") or ""
    return {
        "article_title": title,
        "article_summary": summary,
        "article_summary_html": html_content,
        "article_subtitle": subtitle,
        "article_link": link,
        "article_author": author,
        "article_pub_utc": pub_utc,
        "article_image_url": lead_image,
    }


def fetch_entries_from_rss(limit: int | None = None) -> list[EntryDict]:
    """Pull entries from the configured RSS feed so we know what to process."""
    feedparser = _get_feedparser()
    payload, rss_error = _fetch_rss_payload(RSS_URL)
    if rss_error:
        raise SystemExit(rss_error)
    if payload is not None:
        if _payload_looks_like_html(payload):
            raise SystemExit(
                "RSS fetch returned HTML instead of XML (possible CDN block or origin error)."
            )
        p = feedparser.parse(payload)
    else:
        p = feedparser.parse(RSS_URL)
    if not p.entries:
        raise SystemExit("RSS has no entries")
    entries = [_entry_from_feed(e) for e in p.entries]
    entries.sort(key=lambda ent: ent["article_pub_utc"])
    if limit is not None:
        entries = entries[-limit:]
    return entries


def update_latest_state_snapshot(state: JSONDict) -> None:
    """Maintain legacy top-level keys for backward compatibility."""
    raw_items = state.get("items")
    if not isinstance(raw_items, dict):
        return
    items = cast(StateItems, raw_items)
    latest: JSONDict | None = None
    for data in items.values():
        pub = data.get("last_pub_utc")
        if not isinstance(pub, str) or not pub:
            continue
        if latest is None:
            latest = data
            continue
        latest_pub = str(latest.get("last_pub_utc") or "")
        if pub > latest_pub:
            latest = data
    if latest:
        state["last_pub_utc"] = latest.get("last_pub_utc")
        state["rss_added"] = latest.get("rss_added")
        state["uploaded_url"] = latest.get("uploaded_url")

def newest_sidecar() -> str | None:
    """Return the latest sidecar on disk as a fallback when parsing TTS output."""
    sc = sorted(OUT.glob("*.mp3.rssmeta.json"), key=os.path.getmtime, reverse=True)
    return str(sc[0]) if sc else None

def main() -> None:
    """Coordinate fetching entries, synthesizing audio, uploading, and feed updates."""
    if not RSS_URL:
        raise SystemExit("Missing RSS_URL")

    ensure_kv_namespace_id()
    state_key = f"feed:{SLUG}"
    state = _ensure_json_dict(kv_get(state_key) or {})

    raw_items_obj: object = state.setdefault("items", {})
    if not isinstance(raw_items_obj, dict):
        raw_items_obj = {}
        state["items"] = raw_items_obj
    items = cast(StateItems, raw_items_obj)

    raw_usage_obj: object = state.setdefault("usage", {"cumulative_characters": 0})
    if not isinstance(raw_usage_obj, dict):
        raw_usage_obj = {"cumulative_characters": 0}
        state["usage"] = raw_usage_obj
    usage = cast(JSONDict, raw_usage_obj)

    state.setdefault("pending_deploy", False)

    entries: list[EntryDict] = fetch_entries_from_rss()
    if not entries:
        raise SystemExit("RSS has no entries")

    total_entries = len(entries)
    force_full_rescan = os.getenv("PODCAST_FULL_RESCAN", "").strip().lower() in {"1", "true", "yes", "y"}
    last_processed_pub = state.get("last_pub_utc") or ""

    if force_full_rescan:
        print("[info] PODCAST_FULL_RESCAN set; scanning entire feed")
    else:
        candidates: list[EntryDict] = []
        for entry in entries:
            link = entry.get("article_link")
            entry_pub = entry.get("article_pub_utc", "")
            if not link:
                candidates.append(entry)
                continue

            identifier = ia_identifier_for_link(link)
            entry_state = items.get(identifier)
            already_recorded = (
                entry_state
                and entry_state.get("rss_added")
                and entry_state.get("last_pub_utc") == entry_pub
                and entry_state.get("uploaded_url")
            )
            if already_recorded:
                continue

            if (
                not entry_state
                or not entry_state.get("rss_added")
                or (last_processed_pub and entry_pub and entry_pub > last_processed_pub)
            ):
                candidates.append(entry)

        if candidates:
            entries = candidates
            print(f"[info] Processing {len(entries)} new/changed RSS entries (out of {total_entries})")
        else:
            entries = cast(list[EntryDict], [])
            print("[info] No new RSS entries detected; skipping re-scan")

    feed_xml = os.getenv(
        "FEED_PATH",
        str(PUBLIC / (os.getenv("PODCAST_FILE", f"feeds/{SLUG}.xml"))),
    )

    gcp_ready = False
    feed_updated = False
    processed = False
    run_characters = 0
    retry_failed_entries = _is_retry_failed_enabled()

    def estimate_characters(meta_like: Mapping[str, Any]) -> int:
        """Approximate characters for billing before we run expensive TTS work."""
        summary = str(meta_like.get("article_summary") or "")
        summary_clean = re.sub("<.*?>", "", summary)
        subtitle = str(meta_like.get("article_subtitle") or "")
        title = str(meta_like.get("article_title", ""))
        parts: list[str] = [title, subtitle, summary_clean]
        plain = "\n".join([p for p in parts if p]).strip()
        return len(plain)

    for entry in entries:
        link = entry.get("article_link")
        if not link:
            print(f"[skip] Entry missing link: {entry['article_title']}")
            continue

        identifier = ia_identifier_for_link(link)
        entry_state_obj = items.get(identifier)
        if isinstance(entry_state_obj, dict):
            entry_state = entry_state_obj
        else:
            entry_state = {}

        if not entry_state:
            legacy_pub = state.get("last_pub_utc")
            if legacy_pub and legacy_pub == entry["article_pub_utc"]:
                entry_state.update(
                    {
                        "last_pub_utc": legacy_pub,
                        "rss_added": state.get("rss_added", False),
                        "uploaded_url": state.get("uploaded_url"),
                    }
                )
        items[identifier] = entry_state

        entry_state.setdefault("article_title", entry["article_title"])
        entry_state.setdefault("article_link", link)
        entry_state.setdefault("article_pub_utc", entry["article_pub_utc"])
        entry_state.setdefault("tts_characters", estimate_characters(entry))
        entry_state["article_summary"] = entry["article_summary"]
        entry_state["article_summary_html"] = entry.get("article_summary_html", "")
        entry_state["article_subtitle"] = entry.get("article_subtitle", "")
        entry_state["article_image_url"] = entry.get("article_image_url", "")

        ia_present = ia_has_episode_http(identifier)
        last_pub = entry_state.get("last_pub_utc")
        already_in_feed = bool(entry_state.get("rss_added"))

        print("\n[entry]")
        print(f"  title: {entry['article_title']}")
        print(f"  link:  {link}")
        print(f"  id:    {identifier}")
        print(f"  pub:   {entry['article_pub_utc']}")
        print(f"  IA has audio: {ia_present}")
        print(f"  feed already updated: {already_in_feed}")

        if _should_skip_failed_entry(entry_state, entry["article_pub_utc"], retry_failed_entries):
            last_failed_at = entry_state.get(FAILED_ENTRY_AT_UTC_KEY, "")
            last_failed_step = entry_state.get(FAILED_ENTRY_STEP_KEY, "")
            print(
                "  → Skipping previously failed entry "
                f"(last_failure_at={last_failed_at}, step={last_failed_step})"
            )
            print(f"  → Set {RETRY_FAILED_ENV_NAME}=1 to retry this entry")
            continue

        if ia_present and last_pub == entry["article_pub_utc"] and already_in_feed:
            print("  → Skipping (already processed)")
            continue

        ia_url = f"https://archive.org/download/{identifier}/episode.mp3"

        # Case: audio exists but feed is missing the episode
        if ia_present and last_pub == entry["article_pub_utc"] and not already_in_feed:
            print("  → Restoring feed entry from existing audio")
            sidecar_path = (OUT / f"sidecar-{link_hash(link)}.json")
            OUT.mkdir(parents=True, exist_ok=True)
            payload = entry | {
                "mp3_local_path": "",
                "mp3_filename": "episode.mp3",
                "generated_il_iso": "",
                "tts_characters": entry_state.get("tts_characters", estimate_characters(entry)),
                "tts_generated": False,
            }
            with sidecar_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            sh(PY, str(ROOT / "write_rss.py"), ia_url, str(sidecar_path))

            entry_state.update(
                {
                    "uploaded_url": ia_url,
                    "rss_added": True,
                    "last_pub_utc": entry["article_pub_utc"],
                    "article_pub_utc": entry["article_pub_utc"],
                    "article_subtitle": entry.get("article_subtitle", ""),
                    "article_summary": entry.get("article_summary", ""),
                    "article_summary_html": entry.get("article_summary_html", ""),
                    "article_image_url": entry.get("article_image_url", ""),
                }
            )
            _clear_entry_failure(entry_state)
            state["pending_deploy"] = True
            update_latest_state_snapshot(state)
            kv_put_or_die(state_key, state)

            feed_updated = True
            processed = True
            print(f"  Feed updated -> {pathlib.Path(feed_xml).resolve()}")
            print(f"  Audio: {ia_url}")
            continue

        # Otherwise we need to synthesize + upload
        if not gcp_ready:
            sa = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
            if not pathlib.Path(sa).exists():
                raise SystemExit(f"Missing GCP SA key: {sa}")
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa
            os.environ.pop("IA_CONFIG_FILE", None)
            gcp_ready = True

        print("  → Generating audio")
        env = os.environ.copy()
        env["TARGET_ENTRY_LINK"] = link or ""
        try:
            out1 = sh(PY, str(ROOT / "one_episode.py"), env=env)
        except subprocess.CalledProcessError as exc:
            output_text = getattr(exc, "output", "") or ""
            if not isinstance(output_text, str):
                output_text = str(output_text)
            failure_message = output_text.strip() or str(exc)
            failure_message = _truncate_text(failure_message, FAILURE_MESSAGE_MAX_CHARS)
            _record_entry_failure(
                entry_state,
                identifier=identifier,
                link=link,
                entry_pub_utc=entry["article_pub_utc"],
                step=FAILURE_STEP_GENERATE_AUDIO,
                message=failure_message,
            )
            items[identifier] = entry_state
            print("  → ERROR: one_episode.py failed; continuing to next entry")
            print(f"  → Failure stored for id={identifier} link={link}")
            if failure_message:
                print(f"  → Failure summary: {failure_message}")
            if not kv_put(state_key, state):
                print("[kv] Warning: failed to persist failure state to KV")
            continue

        sidecar_path = None
        for line in out1.splitlines()[::-1]:
            if line.startswith("Sidecar: "):
                sidecar_path = line.split("Sidecar: ", 1)[1].strip()
                break
        if not sidecar_path:
            sidecar_path = newest_sidecar()
            if not sidecar_path:
                raise SystemExit("No sidecar found after generation")

        with open(sidecar_path, "r", encoding="utf-8") as f:
            meta = cast(JSONDict, json.load(f))

        generated_this_run = meta.get("tts_generated", True)
        char_count = meta.get("tts_characters")
        if char_count is None:
            char_count = estimate_characters(entry)
        entry_state["tts_characters"] = char_count
        entry_state["article_subtitle"] = meta.get("article_subtitle", entry.get("article_subtitle", ""))
        entry_state["article_summary"] = meta.get("article_summary", entry.get("article_summary", ""))
        entry_state["article_summary_html"] = meta.get("article_summary_html", entry.get("article_summary_html", ""))
        entry_state["article_image_url"] = meta.get("article_image_url", entry.get("article_image_url", ""))

        print("  → Uploading to Internet Archive")
        out2 = sh(PY, str(ROOT / "upload_to_ia.py"), meta["mp3_local_path"])
        ia_url = None
        for line in out2.splitlines():
            if line.startswith("OK:"):
                ia_url = line.split(" ", 1)[1].strip()
        if not ia_url:
            raise SystemExit("Upload failed or no IA URL captured")

        mp3_path = pathlib.Path(meta["mp3_local_path"])
        if mp3_path.exists():
            try:
                mp3_path.unlink()
                print(f"  → Deleted local MP3: {mp3_path}")
            except Exception as e:
                print(f"  → Warning: could not delete MP3: {e}")

        print("  → Updating RSS feed")
        sh(PY, str(ROOT / "write_rss.py"), ia_url, sidecar_path)

        entry_state.update(
            {
                "uploaded_url": ia_url,
                "rss_added": True,
                "last_pub_utc": entry["article_pub_utc"],
                "article_pub_utc": entry["article_pub_utc"],
                "article_subtitle": meta.get("article_subtitle", entry.get("article_subtitle", "")),
                "article_summary": meta.get("article_summary", entry.get("article_summary", "")),
                "article_summary_html": meta.get("article_summary_html", entry.get("article_summary_html", "")),
                "article_image_url": meta.get("article_image_url", entry.get("article_image_url", "")),
            }
        )
        _clear_entry_failure(entry_state)
        if generated_this_run:
            usage["cumulative_characters"] = usage.get("cumulative_characters", 0) + char_count
            run_characters += char_count
        state["pending_deploy"] = True
        update_latest_state_snapshot(state)
        kv_put_or_die(state_key, state)

        feed_updated = True
        processed = True
        print(f"  Feed updated -> {pathlib.Path(feed_xml).resolve()}")
        print(f"  Audio: {ia_url}")

    if not processed:
        print("No pending entries - everything up to date")

    cumulative = usage.get("cumulative_characters", 0)

    print("\n[TTS usage]")
    if run_characters:
        print(f"  This run: {run_characters:,} characters")
    else:
        print("  This run: 0 characters (no new synthesis)")

    print(f"  Recorded total: {cumulative:,} characters")

    if run_characters:
        try:
            from tts_usage import fetch_tts_usage

            billing_usage = cast(BillingUsage, fetch_tts_usage())
            summary = _ensure_json_dict(billing_usage.get("summary"))
            billing_used = int(summary.get("characters", 0) or 0)
            print("  Billing cycle: {:,} characters used".format(billing_used))

            groups_data_input: object = billing_usage.get("by_group", [])
            if isinstance(groups_data_input, list):
                raw_groups: list[object] = cast(list[object], groups_data_input)
            else:
                raw_groups = []
            groups: dict[str, BillingGroup] = {}
            for raw_row in raw_groups:
                row = _ensure_json_dict(raw_row)
                label = str(row.get("label") or "")
                groups[label] = BillingGroup(
                    characters=int(row.get("characters", 0) or 0),
                    free_tier_remaining=int(row.get("free_tier_remaining", 0) or 0),
                )

            standard = groups.get("standard") or _empty_billing_group()
            premium = groups.get("wavenet_or_neural2") or _empty_billing_group()

            print(
                "    Standard voices: used {:,} characters, {:,} free-tier remaining".format(
                    standard["characters"], standard["free_tier_remaining"],
                )
            )
            print(
                "    WaveNet/Neural2 voices: used {:,} characters, {:,} free-tier remaining".format(
                    premium["characters"], premium["free_tier_remaining"],
                )
            )
        except Exception as e:
            print(f"  Billing query failed: {e}")

    pending_deploy = state.get("pending_deploy", False)
    deploy_needed = feed_updated or pending_deploy

    if deploy_needed:
        if not feed_updated and pending_deploy:
            print("Feed unchanged but pending deploy exists; retrying deploy")
        ran, success = deploy_pages()
        if ran and success:
            state["pending_deploy"] = False
            kv_put_or_die(state_key, state)
        else:
            state["pending_deploy"] = True
            kv_put_or_die(state_key, state)
            if ran:
                print("Deploy failed; will retry on next run")
            else:
                print("Deploy skipped (missing configuration); will retry when ready")
    else:
        print("Feed unchanged; skipping deploy")

def deploy_pages() -> tuple[bool, bool]:
    """Kick Cloudflare Pages so listeners can see RSS/website updates right away."""
    # Optional automatic deploy to Cloudflare Pages with Wrangler
    if CF_API_TOKEN and CF_ACCOUNT_ID and CF_PAGES_PROJECT:
        wrangler_path = resolve_wrangler_path()
        if wrangler_path:
            try:
                deploy_args = [
                    wrangler_path, "pages", "deploy", "public",
                    "--project-name",CF_PAGES_PROJECT,
                    "--commit-dirty=true",
                ]
                env_branch = os.getenv("CF_PAGES_BRANCH") or os.getenv("CF_PAGES_PROD_BRANCH")
                env_commit = os.getenv("CF_PAGES_COMMIT")
                git_branch, git_commit = git_info()
                branch = env_branch or git_branch
                commit = env_commit or git_commit
                if branch:
                    deploy_args.extend(["--branch", branch])
                if commit:
                    deploy_args.extend(["--commit-hash", commit])
                sh(*deploy_args)
                print("Cloudflare Pages deploy OK")
                return True, True
            except Exception as e:
                print(f"Wrangler deploy failed: {e}")
                return True, False
        else:
            print("Wrangler not in PATH, skipping deploy")
            return False, False
    else:
        print("CF Pages env not set, skipping deploy")
        return False, False


if __name__ == "__main__":
    main()
