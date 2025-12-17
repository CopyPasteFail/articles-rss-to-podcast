#!/usr/bin/env python
"""Create a single podcast episode MP3 from a chosen RSS article."""

from __future__ import annotations

import datetime
import gzip
import html
import io
import json
import os
import pathlib
import re
import sys
from typing import Callable, Protocol, Sequence, TypedDict

import feedparser
import requests
from google.cloud import texttospeech
from pydub import AudioSegment, effects

from content_utils import resolve_article_content, text_to_html


class EntryMeta(TypedDict):
    """Normalized info about an article that we can safely feed into TTS."""

    link: str
    title: str
    article_text: str
    article_html: str
    article_subtitle: str
    article_image_url: str
    author: str
    pub_utc: str


class SidecarPayload(TypedDict):
    """Metadata blob written next to each MP3 for downstream automation."""

    article_title: str
    article_summary: str
    article_summary_html: str
    article_subtitle: str
    article_text: str
    article_link: str
    article_author: str
    article_pub_utc: str
    article_image_url: str
    mp3_filename: str
    mp3_local_path: str
    tts_characters: int
    tts_generated: bool


class _TTSClient(Protocol):
    """Protocol to help type-check Google Text-to-Speech client usage."""

    def synthesize_speech(
        self,
        *,
        input: texttospeech.SynthesisInput,
        voice: texttospeech.VoiceSelectionParams,
        audio_config: texttospeech.AudioConfig,
    ) -> texttospeech.SynthesizeSpeechResponse: ...


OUT = pathlib.Path(os.getenv("OUT_DIR", "./out"))
VOICE = os.getenv("GCP_TTS_VOICE", "en-US-Standard-C")
LANG = os.getenv("GCP_TTS_LANG", "").strip()
RATE = float(os.getenv("GCP_TTS_RATE", "1.0"))
PITCH = float(os.getenv("GCP_TTS_PITCH", "0.0"))
RSS_URL = os.getenv("RSS_URL", "")
TARGET_LINK = os.getenv("TARGET_ENTRY_LINK", "").strip()
TARGET_ID = os.getenv("TARGET_ENTRY_ID", "").strip()
RSS_DEBUG_FILENAME = "last_rss.xml"
RSS_DEBUG_SNIPPET_CHARS = 400
RSS_DEBUG_HTTP_CONNECT_TIMEOUT_S = 10.0
RSS_DEBUG_HTTP_READ_TIMEOUT_S = 20.0


def _ensure_str(value: object, *, default: str = "") -> str:
    """Return feedparser values as plain strings so the rest of the flow stays sane."""
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


def _describe_rss_source(rss_url: str) -> str:
    """Return a concise description of the RSS input (URL, file path, or inline XML).

    Inputs: rss_url from RSS_URL; may be empty, a URL, a local path, or inline XML.
    Outputs: a short human-readable description (never the full inline XML payload).
    Edge cases: empty string, non-existent path, inline XML with leading whitespace.
    """
    if not rss_url:
        return "RSS_URL is empty"
    stripped = rss_url.lstrip()
    if stripped.startswith("<"):
        return f"inline XML string in RSS_URL (length={len(rss_url)})"
    rss_path = pathlib.Path(rss_url)
    if rss_path.exists():
        try:
            size_bytes = rss_path.stat().st_size
        except OSError:
            size_bytes = -1
        size_label = f"{size_bytes} bytes" if size_bytes >= 0 else "unknown size"
        return f"local file {rss_path.resolve()} ({size_label})"
    return f"url {rss_url} (length={len(rss_url)})"


def _looks_like_html(payload_text: str) -> bool:
    """Detect obvious HTML payloads that are masquerading as RSS/Atom feeds.

    Inputs: decoded text payload (best-effort).
    Outputs: True when the content resembles HTML, False otherwise.
    Edge cases: leading whitespace, mixed-case tags, short payloads.
    """
    stripped = payload_text.lstrip().lower()
    if stripped.startswith("<!doctype html") or stripped.startswith("<html"):
        return True
    return "<html" in stripped[:600]


def _decode_rss_payload_for_debug(payload: bytes) -> str:
    """Decode RSS bytes for debug output, handling gzip and invalid UTF-8 safely.

    Inputs: raw bytes captured from disk or HTTP.
    Outputs: decoded text (replacement characters on decode errors).
    Edge cases: gzip payloads, invalid encodings, empty payloads.
    """
    if payload.startswith(b"\x1f\x8b"):
        try:
            payload = gzip.decompress(payload)
        except OSError:
            pass
    return payload.decode("utf-8", errors="replace")


def _read_rss_payload_for_debug(
    rss_url: str,
    *,
    http_get: Callable[..., requests.Response] = requests.get,
) -> bytes | None:
    """Fetch or read the RSS payload for debugging when parsing yields no entries.

    Inputs: rss_url from RSS_URL and an injectable http_get function for HTTP fetches.
    Outputs: raw bytes, or None if the payload cannot be accessed.
    Edge cases: inline XML, missing files, network errors, non-HTTP strings.
    """
    if not rss_url:
        return None
    stripped = rss_url.lstrip()
    if stripped.startswith("<"):
        return rss_url.encode("utf-8", errors="replace")

    rss_path = pathlib.Path(rss_url)
    if rss_path.exists():
        try:
            return rss_path.read_bytes()
        except OSError as exc:
            print(f"RSS debug: failed to read RSS_URL file: {exc}")
            return None

    if rss_url.startswith(("http://", "https://")):
        try:
            response = http_get(
                rss_url,
                timeout=(RSS_DEBUG_HTTP_CONNECT_TIMEOUT_S, RSS_DEBUG_HTTP_READ_TIMEOUT_S),
            )
            response.raise_for_status()
            return response.content
        except Exception as exc:
            print(f"RSS debug: failed to fetch RSS_URL over HTTP: {exc}")
            return None

    print("RSS debug: RSS_URL is not a file path or HTTP URL; skipping payload fetch")
    return None


def _log_feedparser_diagnostics(parsed: feedparser.FeedParserDict, *, entries_count: int) -> None:
    """Log feedparser diagnostics to help explain why a feed produced zero entries.

    Inputs: parsed feedparser result and entries_count from that parse.
    Outputs: None (prints to stdout).
    Edge cases: missing feedparser attributes or exceptions during formatting.
    """
    bozo_flag = bool(getattr(parsed, "bozo", False))
    bozo_exception = getattr(parsed, "bozo_exception", None)
    href = getattr(parsed, "href", None)
    print(f"RSS parse entries: {entries_count}")
    if href:
        print(f"RSS parsed href: {href}")
    if bozo_flag or bozo_exception:
        print(f"RSS parse bozo={bozo_flag} exception={bozo_exception}")


def _dump_rss_debug(rss_url: str) -> None:
    """Persist the RSS payload and print a short snippet when parsing yields no entries.

    Inputs: rss_url from RSS_URL.
    Outputs: None (writes a debug file and prints summary/snippet).
    Edge cases: payload unavailable, file write errors, non-UTF-8 payloads.
    Atomicity: debug file write is best-effort and not atomic.
    """
    print(f"RSS debug source (RSS_URL): {_describe_rss_source(rss_url)}")
    payload = _read_rss_payload_for_debug(rss_url)
    if payload is None:
        print("RSS debug: no payload available to dump")
        return

    debug_dir = OUT / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_path = debug_dir / RSS_DEBUG_FILENAME
    try:
        debug_path.write_bytes(payload)
        print(f"RSS debug: wrote {len(payload)} bytes to {debug_path}")
    except OSError as exc:
        print(f"RSS debug: failed to write debug payload: {exc}")

    payload_text = _decode_rss_payload_for_debug(payload)
    snippet = payload_text[:RSS_DEBUG_SNIPPET_CHARS]
    print(f"RSS debug snippet (first {RSS_DEBUG_SNIPPET_CHARS} chars):")
    print(snippet)
    if _looks_like_html(payload_text):
        print("RSS debug: payload looks like HTML, not RSS")

def slugify(url_or_title: str) -> str:
    """Turn a link or title into a short filesystem-friendly slug for the MP3 name."""
    base = url_or_title.strip()
    base = re.sub(r"https?://", "", base)
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base.lower()).strip("-")
    return base[:120] or "article"


def feed_entry_to_meta(e: object, *, allow_fetch: bool = False) -> EntryMeta:
    """Expand a feed entry into EntryMeta so downstream SSML rendering has clean data."""
    link_source = getattr(e, "link", None) or getattr(e, "id", None)
    link = _ensure_str(link_source)
    title_value = getattr(e, "title", None)
    title = _ensure_str(title_value, default=link or "Article")
    author = _ensure_str(getattr(e, "author", None))
    if not author:
        author = _ensure_str(getattr(e, "creator", None))
    tstruct = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    if tstruct:
        pub_utc = datetime.datetime(*tstruct[:6], tzinfo=datetime.timezone.utc).isoformat()
    else:
        pub_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    plain_text, html_content, subtitle, lead_image = resolve_article_content(e, link, allow_fetch=allow_fetch)
    if not plain_text:
        plain_text = _ensure_str(getattr(e, "summary", None)) or _ensure_str(getattr(e, "description", None)) or title
    if not html_content and plain_text:
        html_content = text_to_html(plain_text)
    if not subtitle:
        subtitle = ""
    return EntryMeta(
        link=link,
        title=title,
        article_text=plain_text,
        article_html=html_content,
        article_subtitle=subtitle,
        article_image_url=lead_image,
        author=author,
        pub_utc=pub_utc,
    )


def select_entry() -> EntryMeta:
    """Pick the requested feed entry (or the latest) as the base article for the episode."""
    parsed: feedparser.FeedParserDict = feedparser.parse(RSS_URL)
    entries: list[object] = list(parsed.entries)
    if not entries:
        _log_feedparser_diagnostics(parsed, entries_count=0)
        _dump_rss_debug(RSS_URL)
        sys.exit("RSS has no entries; see RSS debug output above")

    def matches_target(entry: object) -> bool:
        """Check whether the feed entry matches the CLI-supplied target filters."""
        link = _ensure_str(getattr(entry, "link", None))
        entry_id = _ensure_str(getattr(entry, "id", None))
        if TARGET_LINK and link == TARGET_LINK:
            return True
        if TARGET_ID and entry_id == TARGET_ID:
            return True
        return False

    target = next((entry for entry in entries if matches_target(entry)), None)
    if not target:
        if TARGET_LINK or TARGET_ID:
            print("Target entry not found in feed - falling back to latest")
        target = entries[0]
    return feed_entry_to_meta(target, allow_fetch=True)


MAX_SSML_BYTES = 4500  # keep margin below Google's 5000-byte hard limit
MAX_PARAGRAPH_CHARS = 1000  # break very large paragraphs into smaller chunks


def _chunk_paragraph(text: str, limit: int) -> list[str]:
    """Break one long paragraph into smaller slices so the TTS API accepts them."""
    words = text.split()
    if not words:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        addition = len(word) + (1 if current else 0)
        if current and current_len + addition > limit:
            chunks.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += addition
    if current:
        chunks.append(" ".join(current))
    return chunks


def _normalize_paragraphs(paragraphs: list[str]) -> list[str]:
    """Clean and right-size paragraphs before we assemble the SSML sections."""
    normalized: list[str] = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > MAX_PARAGRAPH_CHARS:
            normalized.extend(_chunk_paragraph(para, MAX_PARAGRAPH_CHARS))
        else:
            normalized.append(para)
    return normalized or paragraphs


def _mk_segment(title: str, paras: Sequence[str], include_title: bool) -> str:
    """Wrap title + paragraphs in SSML so the TTS step knows exactly what to read."""
    parts = ["<speak>"]
    if include_title:
        parts.append(f"<p>{html.escape(title)}</p>")
    for para in paras:
        parts.append(f"<p>{html.escape(para)}</p>")
    parts.append("</speak>")
    return "\n".join(parts)


def render_ssml(meta: EntryMeta) -> tuple[list[str], int]:
    """Convert article text into bite-sized SSML segments and character counts before TTS."""
    title = meta["title"]
    body_text = (meta.get("article_text") or meta.get("article_html") or "").strip()
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", body_text) if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in body_text.splitlines() if p.strip()]
    normalized_paragraphs = _normalize_paragraphs(paragraphs)
    subtitle = meta.get("article_subtitle") or ""
    speech_paragraphs: list[str] = normalized_paragraphs
    if subtitle:
        speech_paragraphs = [subtitle.strip()] + normalized_paragraphs
    plain_text = "\n".join([title] + speech_paragraphs).strip() or title
    char_count = len(plain_text)

    segments: list[str] = []
    current: list[str] = []
    include_title = True

    def flush_current(curr: Sequence[str], include_title_flag: bool) -> None:
        """Move buffered paragraphs into the final list while preserving the title flag."""
        if not curr and include_title_flag:
            return
        segments.append(_mk_segment(title, curr, include_title_flag))

    for para in speech_paragraphs:
        trial = current + [para]
        ssml_trial = _mk_segment(title, trial, include_title)
        if len(ssml_trial.encode("utf-8")) > MAX_SSML_BYTES:
            if not current:
                # paragraph alone is too big -> split harder
                sub_chunks = _chunk_paragraph(para, MAX_PARAGRAPH_CHARS)
                buffer: list[str] = []
                for idx, chunk in enumerate(sub_chunks):
                    trial_chunk = buffer + [chunk]
                    chunk_ssml = _mk_segment(title, trial_chunk, include_title)
                    if len(chunk_ssml.encode("utf-8")) > MAX_SSML_BYTES:
                        if buffer:
                            flush_current(buffer, include_title)
                            include_title = False
                            buffer = [chunk]
                        else:
                            chunk_only = _mk_segment(title, [chunk], include_title)
                            if len(chunk_only.encode("utf-8")) > MAX_SSML_BYTES:
                                raise SystemExit("Paragraph chunk still exceeds SSML limit; consider reducing MAX_PARAGRAPH_CHARS")
                            flush_current([chunk], include_title)
                            include_title = False
                            buffer = []
                    else:
                        buffer = trial_chunk
                        if idx == len(sub_chunks) - 1:
                            flush_current(buffer, include_title)
                            include_title = False
                            buffer = []
                current = []
            else:
                flush_current(current, include_title)
                include_title = False
                current = [para]
        else:
            current = trial
    if current:
        flush_current(current, include_title)
    if not segments:
        segments.append(_mk_segment(title, [], True))

    return segments, char_count

def synthesize_ssml(ssml_segments: Sequence[str], out_path: pathlib.Path) -> None:
    """Send the SSML segments to Google, stitch the MP3s, and write the output for downstream steps."""
    client: _TTSClient = texttospeech.TextToSpeechClient()
    name = VOICE
    lang = LANG

    if not lang:
        # Fallback: derive from voice like "he-IL-Wavenet-A" -> "he-IL"
        parts = name.split("-")
        if len(parts) >= 2:
            lang = "-".join(parts[:2])
        else:
            lang = "en-US"

    voice = texttospeech.VoiceSelectionParams(
        name=name,
        language_code=lang
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=RATE,
        pitch=PITCH,
    )
    combined_audio: AudioSegment | None = None
    for idx, ssml in enumerate(ssml_segments):
        input_ssml = texttospeech.SynthesisInput(ssml=ssml)
        resp = client.synthesize_speech(input=input_ssml, voice=voice, audio_config=audio_config)
        segment_audio = AudioSegment.from_file(io.BytesIO(resp.audio_content), format="mp3")
        combined_audio = segment_audio if combined_audio is None else combined_audio + segment_audio
        print(f"Segment {idx+1}/{len(ssml_segments)} bytes={len(resp.audio_content)}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if combined_audio is None:
        raise RuntimeError("TTS synthesis produced no audio segments")
    combined_audio.export(out_path, format="mp3")
    print(f"Voice: {name}  Lang: {lang}")

def normalize_mp3(path: pathlib.Path) -> None:
    """Run loudness normalization right after synthesis so the finished MP3 sounds consistent."""
    audio = AudioSegment.from_file(path)
    audio = effects.normalize(audio)
    audio.export(path, format="mp3")

def main() -> None:
    """Drive the whole flow: select an article, render SSML, build MP3 + sidecar."""
    if not RSS_URL:
        sys.exit("Missing RSS_URL")

    print(f"RSS source (RSS_URL): {_describe_rss_source(RSS_URL)}")
    e = select_entry()
    # filename: <YYYYMMDD-HHMMSS>-<slug(title or path)>
    dt = datetime.datetime.fromisoformat(e["pub_utc"].replace("Z","+00:00")).astimezone(datetime.timezone.utc)
    ts = dt.strftime("%Y%m%d-%H%M%S")
    slug = slugify(e["link"] or e["title"])
    mp3_name = f"{ts}-{slug}.mp3"
    mp3_path = OUT / mp3_name

    ssml_segments, char_count = render_ssml(e)

    generated = False
    if mp3_path.exists():
        print(f"Exists, skipping TTS: {mp3_path}")
    else:
        synthesize_ssml(ssml_segments, mp3_path)
        normalize_mp3(mp3_path)
        print(f"Wrote {mp3_path}")
        generated = True
        print(f"Characters billed (approx): {char_count}")

    sidecar = mp3_path.with_suffix(".mp3.rssmeta.json")
    side: SidecarPayload = {
        "article_title": e["title"],
        "article_summary": e.get("article_text") or "",
        "article_summary_html": e.get("article_html") or "",
        "article_subtitle": e.get("article_subtitle") or "",
        "article_text": e.get("article_text") or "",
        "article_link": e["link"],
        "article_author": e["author"],
        "article_pub_utc": e["pub_utc"],
        "article_image_url": e.get("article_image_url", ""),
        "mp3_filename": mp3_name,
        "mp3_local_path": str(mp3_path),
        "tts_characters": char_count,
        "tts_generated": generated,
    }
    with sidecar.open("w", encoding="utf-8") as f:
        json.dump(side, f, ensure_ascii=False, indent=2)
    print(f"Sidecar: {sidecar}")

if __name__ == "__main__":
    main()
