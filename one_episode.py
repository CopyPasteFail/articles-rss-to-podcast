#!/usr/bin/env python
import os, re, sys, json, pathlib, datetime, html, io
import feedparser
from google.cloud import texttospeech
from pydub import AudioSegment, effects

from content_utils import resolve_article_content, text_to_html

OUT = pathlib.Path(os.getenv("OUT_DIR", "./out"))
VOICE = os.getenv("GCP_TTS_VOICE", "en-US-Standard-C")
LANG  = os.getenv("GCP_TTS_LANG", "").strip()
RATE  = float(os.getenv("GCP_TTS_RATE", "1.0"))
PITCH = float(os.getenv("GCP_TTS_PITCH", "0.0"))
RSS_URL = os.getenv("RSS_URL", "")
TARGET_LINK = os.getenv("TARGET_ENTRY_LINK", "").strip()
TARGET_ID = os.getenv("TARGET_ENTRY_ID", "").strip()

def slugify(url_or_title: str) -> str:
    base = url_or_title.strip()
    base = re.sub(r"https?://", "", base)
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base.lower()).strip("-")
    return base[:120] or "article"

def feed_entry_to_meta(e, *, allow_fetch=False):
    link = getattr(e, "link", None) or getattr(e, "id", None)
    title = getattr(e, "title", link or "Article")
    author = getattr(e, "author", "") or getattr(e, "creator", "") or ""
    tstruct = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    if tstruct:
        pub_utc = datetime.datetime(*tstruct[:6], tzinfo=datetime.timezone.utc).isoformat()
    else:
        pub_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    plain_text, html_content, subtitle, lead_image = resolve_article_content(e, link, allow_fetch=allow_fetch)
    if not plain_text:
        plain_text = getattr(e, "summary", "") or getattr(e, "description", "") or title
    if not html_content and plain_text:
        html_content = text_to_html(plain_text)
    if not subtitle:
        subtitle = ""
    return {
        "link": link,
        "title": title,
        "article_text": plain_text,
        "article_html": html_content,
        "article_subtitle": subtitle,
        "article_image_url": lead_image,
        "author": author,
        "pub_utc": pub_utc,
    }


def select_entry():
    p = feedparser.parse(RSS_URL)
    if not p.entries:
        sys.exit("RSS has no entries")

    def matches_target(entry):
        link = getattr(entry, "link", None) or ""
        entry_id = getattr(entry, "id", None) or ""
        if TARGET_LINK and link == TARGET_LINK:
            return True
        if TARGET_ID and entry_id == TARGET_ID:
            return True
        return False

    target = next((e for e in p.entries if matches_target(e)), None)
    if not target:
        if TARGET_LINK or TARGET_ID:
            print("Target entry not found in feed - falling back to latest")
        target = p.entries[0]
    return feed_entry_to_meta(target, allow_fetch=True)


MAX_SSML_BYTES = 4500  # keep margin below Google's 5000-byte hard limit
MAX_PARAGRAPH_CHARS = 1000  # break very large paragraphs into smaller chunks


def _chunk_paragraph(text: str, limit: int) -> list[str]:
    words = text.split()
    if not words:
        return [text]
    chunks = []
    current = []
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
    normalized = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > MAX_PARAGRAPH_CHARS:
            normalized.extend(_chunk_paragraph(para, MAX_PARAGRAPH_CHARS))
        else:
            normalized.append(para)
    return normalized or paragraphs


def _mk_segment(title: str, paras: list[str], include_title: bool) -> str:
    parts = ["<speak>"]
    if include_title:
        parts.append(f"<p>{html.escape(title)}</p>")
    for para in paras:
        parts.append(f"<p>{html.escape(para)}</p>")
    parts.append("</speak>")
    return "\n".join(parts)


def render_ssml(meta):
    """Return SSML payload segments and character count."""
    title = meta["title"]
    body_text = meta.get("article_text") or ""
    if not body_text:
        body_text = meta.get("article_html") or ""
    body_text = body_text.strip()
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", body_text) if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in body_text.splitlines() if p.strip()]
    paragraphs = _normalize_paragraphs(paragraphs)
    subtitle = meta.get("article_subtitle") or ""
    speech_paragraphs = paragraphs
    if subtitle:
        speech_paragraphs = [subtitle.strip()] + paragraphs
    plain_text = "\n".join([title] + speech_paragraphs).strip()
    if not plain_text:
        plain_text = title
    char_count = len(plain_text)

    segments = []
    current: list[str] = []
    include_title = True

    def flush_current(curr, include_title_flag):
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
                buffer = []
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

def synthesize_ssml(ssml_segments, out_path: pathlib.Path):
    client = texttospeech.TextToSpeechClient()
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
    combined_audio = None
    for idx, ssml in enumerate(ssml_segments):
        input_ssml = texttospeech.SynthesisInput(ssml=ssml)
        resp = client.synthesize_speech(input=input_ssml, voice=voice, audio_config=audio_config)
        segment_audio = AudioSegment.from_file(io.BytesIO(resp.audio_content), format="mp3")
        combined_audio = segment_audio if combined_audio is None else combined_audio + segment_audio
        print(f"Segment {idx+1}/{len(ssml_segments)} bytes={len(resp.audio_content)}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined_audio.export(out_path, format="mp3")
    print(f"Voice: {name}  Lang: {lang}")

def normalize_mp3(path: pathlib.Path):
    audio = AudioSegment.from_file(path)
    audio = effects.normalize(audio)
    audio.export(path, format="mp3")

def main():
    if not RSS_URL:
        sys.exit("Missing RSS_URL")

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
    side = {
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
