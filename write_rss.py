#!/usr/bin/env python
from __future__ import annotations

import datetime
import email.utils
import hashlib
import html
import json
import os
import pathlib
import sys
import xml.etree.ElementTree as ET
from typing import NotRequired, Protocol, TypedDict, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from feedgen.feed import FeedGenerator


class ChannelMeta(TypedDict):
    title: str
    site: str
    desc: str
    author: str
    email: str
    image: str
    feed_url: str


class EpisodePayload(TypedDict):
    article_title: str
    article_pub_utc: str
    audio_url: str
    article_summary_html: NotRequired[str]
    article_summary: NotRequired[str]
    article_subtitle: NotRequired[str]
    article_link: NotRequired[str]
    article_image_url: NotRequired[str]


class PodcastExtension(Protocol):
    def itunes_author(self, author: str) -> None: ...

    def itunes_explicit(self, explicit: str) -> None: ...

    def itunes_owner(self, *, name: str, email: str) -> None: ...

    def itunes_image(self, url: str) -> None: ...

    def itunes_category(self, category: str) -> None: ...


class FeedEntryProtocol(Protocol):
    @property
    def podcast(self) -> PodcastExtension: ...

    def title(self, title: str) -> None: ...

    def description(self, description: str) -> None: ...

    def pubDate(self, value: datetime.datetime | str) -> None: ...

    def enclosure(self, url: str, length: str, mime_type: str) -> None: ...

    def guid(self, guid: str, *, permalink: bool = False) -> None: ...


class FeedGeneratorProtocol(Protocol):
    @property
    def podcast(self) -> PodcastExtension: ...

    def load_extension(self, name: str, atom: bool = True, rss: bool = True) -> None: ...

    def title(self, title: str) -> None: ...

    def link(self, *, href: str, rel: str) -> None: ...

    def description(self, description: str) -> None: ...

    def language(self, language: str) -> None: ...

    def rss_file(self, filename: str, *, pretty: bool = False) -> None: ...

    def add_entry(self) -> FeedEntryProtocol: ...


FeedItem = tuple[str | None, str | None, str | None, str | None, str | None, str | None, str]

ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def _create_feed_generator() -> FeedGeneratorProtocol:
    return cast(FeedGeneratorProtocol, FeedGenerator())


def rfc2822(dt: datetime.datetime) -> str:
    return email.utils.format_datetime(dt)


def get_len(url: str) -> int | None:
    try:
        response = requests.head(url, allow_redirects=True, timeout=15)
        value = response.headers.get("Content-Length")
        return int(value) if value and value.isdigit() else None
    except Exception:
        return None


def ensure_base_feed(
    feed_path: str,
    title: str,
    site: str,
    desc: str,
    author_name: str,
    author_email: str,
    image_url: str,
    feed_url: str,
) -> None:
    fp = pathlib.Path(feed_path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    if fp.exists() and fp.stat().st_size > 0:
        return

    fg = _create_feed_generator()
    fg.load_extension("podcast")
    fg.title(title)
    fg.link(href=site, rel="alternate")
    if feed_url:
        fg.link(href=feed_url, rel="self")
    fg.description(desc)
    fg.language("und")
    fg.podcast.itunes_author(author_name or "")
    fg.podcast.itunes_explicit("no")
    if author_email:
        fg.podcast.itunes_owner(name=author_name or "", email=author_email)
    if image_url:
        fg.podcast.itunes_image(image_url)
    fg.podcast.itunes_category("News")
    fg.rss_file(feed_path, pretty=True)


def _valid_itunes_image(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return parsed.path.lower().endswith((".jpg", ".png"))


def add_item(feed_path: str, channel_meta: ChannelMeta, ep: EpisodePayload, keep_last: int = 200) -> None:
    items: list[FeedItem] = []
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

    fg = _create_feed_generator()
    fg.load_extension("podcast")
    fg.title(channel_meta["title"])
    fg.link(href=channel_meta["site"], rel="alternate")
    if channel_meta["feed_url"]:
        fg.link(href=channel_meta["feed_url"], rel="self")
    fg.description(channel_meta["desc"])
    fg.language("und")
    fg.podcast.itunes_author(channel_meta["author"])
    fg.podcast.itunes_explicit("no")
    if channel_meta.get("email"):
        fg.podcast.itunes_owner(name=channel_meta["author"], email=channel_meta["email"])
    if channel_meta.get("image"):
        fg.podcast.itunes_image(channel_meta["image"])
    fg.podcast.itunes_category("News")

    channel_has_image = bool(channel_meta.get("image"))

    for t, d, l, p, ln, g, img in items[:keep_last]:
        fe = fg.add_entry()
        fe.title(t or "")
        fe.description(d or "")
        if p:
            fe.pubDate(p)
        if l:
            fe.enclosure(l, str(ln or 0), "audio/mpeg")
        if g:
            fe.guid(g, permalink=False)
        if not channel_has_image and _valid_itunes_image(img):
            fe.podcast.itunes_image(img)

    fe = fg.add_entry()
    fe.title(ep["article_title"])
    full_desc_html = ep.get("article_summary_html") or ""
    subtitle = ep.get("article_subtitle") or ""
    article_link = ep.get("article_link")
    if full_desc_html:
        if subtitle:
            full_desc_html = f"<p><strong>{html.escape(subtitle)}</strong></p>" + full_desc_html
        if article_link:
            full_desc_html += f'<p>Original: <a href="{article_link}">{article_link}</a></p>'
        fe.description(full_desc_html.strip())
    else:
        full_desc = ep.get("article_summary") or ""
        if subtitle:
            full_desc = f"{subtitle}\n\n{full_desc}" if full_desc else subtitle
        if article_link:
            full_desc += f"\nOriginal: {article_link}"
        fe.description(full_desc.strip() or ep["article_title"])

    try:
        pub_dt = datetime.datetime.fromisoformat(ep["article_pub_utc"])
    except Exception:
        pub_dt = datetime.datetime.now(ZoneInfo("Asia/Jerusalem"))
    fe.pubDate(rfc2822(pub_dt.astimezone(ZoneInfo("Asia/Jerusalem"))))

    size = get_len(ep["audio_url"]) or 0
    fe.enclosure(ep["audio_url"], str(size), "audio/mpeg")
    fe.guid(
        hashlib.sha1((ep["audio_url"] + ep.get("article_link", "")).encode("utf-8")).hexdigest(),
        permalink=False,
    )
    if not channel_has_image:
        episode_img = ep.get("article_image_url") or ""
        if _valid_itunes_image(episode_img):
            fe.podcast.itunes_image(episode_img)

    pathlib.Path(feed_path).parent.mkdir(parents=True, exist_ok=True)
    fg.rss_file(feed_path, pretty=True)


def resolve_feed_path() -> str:
    fp = os.getenv("FEED_PATH", "").strip()
    if fp:
        return fp
    fname = os.getenv("PODCAST_FILE") or (os.getenv("PODCAST_SLUG", "podcast") + ".xml")
    return os.path.join("./public", fname)


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python write_rss.py <audio_url> <sidecar_json_path>")
        sys.exit(2)

    audio_url = sys.argv[1]
    sidecar = sys.argv[2]
    if not os.path.isfile(sidecar):
        sys.exit(f"Not found: {sidecar}")

    with open(sidecar, "r", encoding="utf-8") as f:
        ep = cast(EpisodePayload, json.load(f))
    ep["audio_url"] = audio_url

    feed_path = resolve_feed_path()
    channel: ChannelMeta = {
        "title": os.getenv("PODCAST_TITLE", "TTS Podcast"),
        "author": os.getenv("PODCAST_AUTHOR", "Omer"),
        "desc": os.getenv("PODCAST_DESCRIPTION", "Auto-generated TTS episodes"),
        "site": os.getenv("PODCAST_SITE", "https://example.com"),
        "email": os.getenv("SHOW_EMAIL", ""),
        "image": os.getenv("PODCAST_IMAGE_URL", ""),
        "feed_url": os.getenv("FEED_URL", ""),
    }

    ensure_base_feed(
        feed_path,
        channel["title"],
        channel["site"],
        channel["desc"],
        channel["author"],
        channel["email"],
        channel["image"],
        channel["feed_url"],
    )

    add_item(feed_path, channel, ep, keep_last=200)
    print(f"Updated: {feed_path}")


if __name__ == "__main__":
    main()
