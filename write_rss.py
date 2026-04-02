#!/usr/bin/env python
"""Build and update the podcast RSS feed after each successful upload."""

from __future__ import annotations

import datetime
import email.utils
import hashlib
import html
import json
import os
import pathlib
import sys
from typing import NotRequired, Protocol, TypedDict, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from defusedxml import ElementTree as ET
from feedgen.feed import FeedGenerator


class ChannelMeta(TypedDict):
    """Minimal description of the entire podcast channel."""

    title: str
    site: str
    desc: str
    author: str
    email: str
    image: str
    feed_url: str


class EpisodePayload(TypedDict):
    """Sidecar payload values that we append into the RSS feed."""

    article_title: str
    article_pub_utc: str
    audio_url: str
    article_summary_html: NotRequired[str]
    article_summary: NotRequired[str]
    article_subtitle: NotRequired[str]
    article_link: NotRequired[str]
    article_image_url: NotRequired[str]


class ExistingFeedItem(TypedDict):
    """Represent the subset of an existing RSS item needed for dedupe decisions."""

    title: str
    description: str
    pub_date: str
    audio_url: str
    audio_length: str
    guid: str
    image_url: str


class PodcastExtension(Protocol):
    """Feedgen's iTunes extension interface (subset used by this script)."""

    def itunes_author(self, author: str) -> None: ...

    def itunes_explicit(self, explicit: str) -> None: ...

    def itunes_owner(self, *, name: str, email: str) -> None: ...

    def itunes_image(self, url: str) -> None: ...

    def itunes_category(self, category: str) -> None: ...


class FeedEntryProtocol(Protocol):
    """Protocol describing feed entries so type-checkers stay happy."""

    @property
    def podcast(self) -> PodcastExtension: ...

    def title(self, title: str) -> None: ...

    def description(self, description: str) -> None: ...

    def pubDate(self, value: datetime.datetime | str) -> None: ...

    def enclosure(self, url: str, length: str, mime_type: str) -> None: ...

    def guid(self, guid: str, *, permalink: bool = False) -> None: ...


class FeedGeneratorProtocol(Protocol):
    """Protocol describing the feed generator API we rely on."""

    @property
    def podcast(self) -> PodcastExtension: ...

    def load_extension(
        self, name: str, atom: bool = True, rss: bool = True
    ) -> None: ...

    def title(self, title: str) -> None: ...

    def link(self, *, href: str, rel: str) -> None: ...

    def description(self, description: str) -> None: ...

    def language(self, language: str) -> None: ...

    def rss_file(self, filename: str, *, pretty: bool = False) -> None: ...

    def add_entry(self) -> FeedEntryProtocol: ...


ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def _create_feed_generator() -> FeedGeneratorProtocol:
    """Return the feedgen FeedGenerator with a protocol-friendly type."""
    return cast(FeedGeneratorProtocol, FeedGenerator())


def rfc2822(dt: datetime.datetime) -> str:
    """Format datetimes the way RSS readers expect."""
    return email.utils.format_datetime(dt)


def get_len(url: str) -> int | None:
    """Fetch Content-Length so enclosure entries have accurate file sizes."""
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
    """Create a minimal RSS skeleton if it does not exist yet."""
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
    """Validate episode images so podcast apps do not reject the feed."""
    if not url:
        return False
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return parsed.path.lower().endswith((".jpg", ".png"))


def _build_episode_guid(episode_payload: EpisodePayload) -> str:
    """Return the deterministic RSS guid for one episode payload.

    Inputs: episode payload with audio_url and optional article_link.
    Outputs: stable SHA-1 guid string used by the RSS feed.
    Edge cases: empty article_link still yields a stable guid based on audio_url.
    """

    return hashlib.sha1(
        (episode_payload["audio_url"] + episode_payload.get("article_link", "")).encode(
            "utf-8"
        ),
        usedforsecurity=False,
    ).hexdigest()


def _existing_item_matches_episode(
    existing_item: ExistingFeedItem,
    episode_payload: EpisodePayload,
) -> bool:
    """Decide whether an existing RSS item already represents the same article.

    Inputs: one parsed existing feed item and the new episode payload.
    Outputs: True when the new episode should replace nothing because it is already present.
    Edge cases: matches first on article link, then falls back to guid, then title+pubDate.
    """

    article_link = (episode_payload.get("article_link") or "").strip()
    if article_link and article_link in existing_item["description"]:
        return True

    if existing_item["guid"] == _build_episode_guid(episode_payload):
        return True

    try:
        pub_date = datetime.datetime.fromisoformat(
            episode_payload["article_pub_utc"]
        ).astimezone(ZoneInfo("Asia/Jerusalem"))
        expected_pub_date = rfc2822(pub_date)
    except Exception:
        expected_pub_date = ""

    return bool(
        existing_item["title"] == episode_payload["article_title"]
        and expected_pub_date
        and existing_item["pub_date"] == expected_pub_date
    )


def add_item(
    feed_path: str, channel_meta: ChannelMeta, ep: EpisodePayload, keep_last: int = 200
) -> None:
    """Append the episode described by ``ep`` and trim the feed to ``keep_last`` items."""
    items: list[ExistingFeedItem] = []
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
            items.append(
                ExistingFeedItem(
                    title=title or "",
                    description=desc or "",
                    audio_url=link or "",
                    pub_date=pub or "",
                    audio_length=length or "",
                    guid=guid or "",
                    image_url=image_url,
                )
            )

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
        fg.podcast.itunes_owner(
            name=channel_meta["author"], email=channel_meta["email"]
        )
    if channel_meta.get("image"):
        fg.podcast.itunes_image(channel_meta["image"])
    fg.podcast.itunes_category("News")

    channel_has_image = bool(channel_meta.get("image"))

    unique_existing_items: list[ExistingFeedItem] = []
    for existing_item in items:
        if _existing_item_matches_episode(existing_item, ep):
            continue
        unique_existing_items.append(existing_item)

    for existing_item in unique_existing_items[:keep_last]:
        fe = fg.add_entry()
        fe.title(existing_item["title"])
        fe.description(existing_item["description"])
        if existing_item["pub_date"]:
            fe.pubDate(existing_item["pub_date"])
        if existing_item["audio_url"]:
            fe.enclosure(
                existing_item["audio_url"],
                str(existing_item["audio_length"] or 0),
                "audio/mpeg",
            )
        if existing_item["guid"]:
            fe.guid(existing_item["guid"], permalink=False)
        if not channel_has_image and _valid_itunes_image(existing_item["image_url"]):
            fe.podcast.itunes_image(existing_item["image_url"])

    fe = fg.add_entry()
    fe.title(ep["article_title"])
    full_desc_html = ep.get("article_summary_html") or ""
    subtitle = ep.get("article_subtitle") or ""
    article_link = ep.get("article_link")
    if full_desc_html:
        if subtitle:
            full_desc_html = (
                f"<p><strong>{html.escape(subtitle)}</strong></p>" + full_desc_html
            )
        if article_link:
            full_desc_html += (
                f'<p>Original: <a href="{article_link}">{article_link}</a></p>'
            )
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
    fe.guid(_build_episode_guid(ep), permalink=False)
    if not channel_has_image:
        episode_img = ep.get("article_image_url") or ""
        if _valid_itunes_image(episode_img):
            fe.podcast.itunes_image(episode_img)

    pathlib.Path(feed_path).parent.mkdir(parents=True, exist_ok=True)
    fg.rss_file(feed_path, pretty=True)


def resolve_feed_path() -> str:
    """Figure out where to write the RSS file based on environment variables."""
    fp = os.getenv("FEED_PATH", "").strip()
    if fp:
        return fp
    fname = os.getenv("PODCAST_FILE") or (os.getenv("PODCAST_SLUG", "podcast") + ".xml")
    return os.path.join("./public", fname)


def main() -> None:
    """CLI entry point invoked after each upload to refresh the RSS feed."""
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
