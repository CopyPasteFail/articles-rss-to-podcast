#!/usr/bin/env python
import os, re, sys, json, pathlib, datetime
import feedparser
from urllib.parse import urlparse
from google.cloud import texttospeech
from pydub import AudioSegment, effects

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

def feed_entry_to_meta(e):
    link = getattr(e, "link", None) or getattr(e, "id", None)
    title = getattr(e, "title", link or "Article")
    summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""
    author = getattr(e, "author", "") or getattr(e, "creator", "") or ""
    tstruct = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    if tstruct:
        pub_utc = datetime.datetime(*tstruct[:6], tzinfo=datetime.timezone.utc).isoformat()
    else:
        pub_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {"link": link, "title": title, "summary": summary, "author": author, "pub_utc": pub_utc}


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
    return feed_entry_to_meta(target)

def normalize_text(text: str) -> str:
    return re.sub("<.*?>", "", text or "")


def render_ssml(meta):
    """Return SSML payload and character count for billing approximation."""
    title = meta["title"]
    summary = normalize_text(meta["summary"])
    ssml = f"""<speak>
<p>{title}</p>
<p>{summary}</p>
</speak>"""
    plain_text = f"{title}\n{summary}".strip()
    char_count = len(plain_text)
    return ssml, char_count

def synthesize_ssml(ssml, out_path: pathlib.Path):
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
    input_ssml = texttospeech.SynthesisInput(ssml=ssml)
    resp = client.synthesize_speech(input=input_ssml, voice=voice, audio_config=audio_config)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        f.write(resp.audio_content)
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

    ssml, char_count = render_ssml({"title": e["title"], "summary": e["summary"]})

    generated = False
    if mp3_path.exists():
        print(f"Exists, skipping TTS: {mp3_path}")
    else:
        synthesize_ssml(ssml, mp3_path)
        normalize_mp3(mp3_path)
        print(f"Wrote {mp3_path}")
        generated = True
        print(f"Characters billed (approx): {char_count}")

    sidecar = mp3_path.with_suffix(".mp3.rssmeta.json")
    side = {
        "article_title": e["title"],
        "article_summary": e["summary"],
        "article_link": e["link"],
        "article_author": e["author"],
        "article_pub_utc": e["pub_utc"],
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
