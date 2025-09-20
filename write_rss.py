#!/usr/bin/env python
import os, sys, json, hashlib, datetime, email.utils, pathlib, xml.etree.ElementTree as ET, requests, html
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from feedgen.feed import FeedGenerator

def rfc2822(dt): return email.utils.format_datetime(dt)

def get_len(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=15)
        v = r.headers.get("Content-Length")
        return int(v) if v and v.isdigit() else None
    except Exception:
        return None

def ensure_base_feed(feed_path, title, site, desc, author_name, author_email, image_url, feed_url):
    fp = pathlib.Path(feed_path); fp.parent.mkdir(parents=True, exist_ok=True)
    if fp.exists() and fp.stat().st_size > 0: return
    fg = FeedGenerator()
    fg.load_extension('podcast')
    fg.title(title)
    fg.link(href=site, rel='alternate')
    if feed_url: fg.link(href=feed_url, rel='self')
    fg.description(desc)
    fg.language('und')
    fg.podcast.itunes_author(author_name or "")
    fg.podcast.itunes_explicit('no')
    if author_email: fg.podcast.itunes_owner(name=author_name or "", email=author_email)
    if image_url: fg.podcast.itunes_image(image_url)
    fg.podcast.itunes_category('News')
    fg.rss_file(feed_path, pretty=True)

ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def _valid_itunes_image(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return parsed.path.lower().endswith((".jpg", ".png"))


def add_item(feed_path, channel_meta, ep, keep_last=200):
    items = []
    if os.path.exists(feed_path):
        tree = ET.parse(feed_path)
        root = tree.getroot()
        for it in root.findall("./channel/item"):
            title = it.findtext("title")
            desc = it.findtext("description")
            pub = it.findtext("pubDate")
            enc = it.find("./enclosure")
            link = enc.get("url") if enc is not None else None
            length = enc.get("length") if enc is not None else None
            guid = it.findtext("guid")
            image_el = it.find(f"{ITUNES_NS}image")
            image_url = ""
            if image_el is not None:
                image_url = image_el.get("href") or (image_el.text or "")
            if not _valid_itunes_image(image_url):
                image_url = ""
            items.append((title, desc, link, pub, length, guid, image_url))

    fg = FeedGenerator()
    fg.load_extension('podcast')
    fg.title(channel_meta["title"])
    fg.link(href=channel_meta["site"], rel='alternate')
    if channel_meta["feed_url"]:
        fg.link(href=channel_meta["feed_url"], rel='self')
    fg.description(channel_meta["desc"])
    fg.language('und')
    fg.podcast.itunes_author(channel_meta["author"])
    fg.podcast.itunes_explicit('no')
    if channel_meta.get("email"):
        fg.podcast.itunes_owner(name=channel_meta["author"], email=channel_meta["email"])
    if channel_meta.get("image"):
        fg.podcast.itunes_image(channel_meta["image"])
    fg.podcast.itunes_category('News')

    channel_has_image = bool(channel_meta.get("image"))

    for t, d, l, p, ln, g, img in items[:keep_last]:
        fe = fg.add_entry()
        fe.title(t or "")
        fe.description(d or "")
        if p: fe.pubDate(p)
        if l: fe.enclosure(l, str(ln or 0), 'audio/mpeg')
        if g: fe.guid(g, permalink=False)
        if not channel_has_image and _valid_itunes_image(img):
            fe.podcast.itunes_image(img)

    fe = fg.add_entry()
    fe.title(ep["article_title"])
    full_desc_html = ep.get("article_summary_html") or ""
    subtitle = ep.get("article_subtitle") or ""
    if full_desc_html:
        if subtitle:
            full_desc_html = f"<p><strong>{html.escape(subtitle)}</strong></p>" + full_desc_html
        if ep.get("article_link"):
            full_desc_html += f"<p>Original: <a href=\"{ep['article_link']}\">{ep['article_link']}</a></p>"
        fe.description(full_desc_html.strip())
    else:
        full_desc = ep.get("article_summary") or ""
        if subtitle:
            full_desc = f"{subtitle}\n\n{full_desc}" if full_desc else subtitle
        if ep.get("article_link"):
            full_desc += f"\nOriginal: {ep['article_link']}"
        fe.description(full_desc.strip() or ep["article_title"])
    try:
        pub_dt = datetime.datetime.fromisoformat(ep["article_pub_utc"])
    except Exception:
        pub_dt = datetime.datetime.now(ZoneInfo("Asia/Jerusalem"))
    fe.pubDate(rfc2822(pub_dt.astimezone(ZoneInfo("Asia/Jerusalem"))))
    size = get_len(ep["audio_url"]) or 0
    fe.enclosure(ep["audio_url"], str(size), "audio/mpeg")
    fe.guid(hashlib.sha1((ep["audio_url"] + ep.get("article_link","")).encode("utf-8")).hexdigest(), permalink=False)
    if not channel_has_image:
        episode_img = ep.get("article_image_url") or ""
        if _valid_itunes_image(episode_img):
            fe.podcast.itunes_image(episode_img)

    pathlib.Path(feed_path).parent.mkdir(parents=True, exist_ok=True)
    fg.rss_file(feed_path, pretty=True)

def resolve_feed_path():
    fp = os.getenv("FEED_PATH", "").strip()
    if fp: return fp
    fname = os.getenv("PODCAST_FILE") or (os.getenv("PODCAST_SLUG","podcast") + ".xml")
    return os.path.join("./public", fname)

def main():
    if len(sys.argv) < 3:
        print("Usage: python write_rss.py <audio_url> <sidecar_json_path>")
        sys.exit(2)

    audio_url = sys.argv[1]
    sidecar = sys.argv[2]
    if not os.path.isfile(sidecar):
        sys.exit(f"Not found: {sidecar}")

    with open(sidecar, "r", encoding="utf-8") as f:
        ep = json.load(f)
    ep["audio_url"] = audio_url

    FEED_PATH = resolve_feed_path()
    CHANNEL = {
        "title": os.getenv("PODCAST_TITLE", "TTS Podcast"),
        "author": os.getenv("PODCAST_AUTHOR", "Omer"),
        "desc": os.getenv("PODCAST_DESCRIPTION", "Auto-generated TTS episodes"),
        "site": os.getenv("PODCAST_SITE", "https://example.com"),
        "email": os.getenv("SHOW_EMAIL",""),
        "image": os.getenv("PODCAST_IMAGE_URL",""),
        "feed_url": os.getenv("FEED_URL",""),
    }

    ensure_base_feed(FEED_PATH, CHANNEL["title"], CHANNEL["site"], CHANNEL["desc"],
                     CHANNEL["author"], CHANNEL["email"], CHANNEL["image"], CHANNEL["feed_url"])

    add_item(FEED_PATH, CHANNEL, ep, keep_last=200)
    print(f"Updated: {FEED_PATH}")

if __name__ == "__main__":
    main()
