"""Tests for RSS item dedupe behavior in write_rss.py."""

from __future__ import annotations

import pathlib
import importlib
import sys
import types
import xml.etree.ElementTree as ET


class FakePodcastExtension:
    """Store podcast extension calls used by write_rss.py during tests."""

    def __init__(self) -> None:
        self.image_url = ""

    def itunes_author(self, author: str) -> None:
        """Accept the configured author without additional behavior."""

    def itunes_explicit(self, explicit: str) -> None:
        """Accept the configured explicit flag without additional behavior."""

    def itunes_owner(self, *, name: str, email: str) -> None:
        """Accept the configured owner block without additional behavior."""

    def itunes_image(self, url: str) -> None:
        """Store the configured image URL so rss_file can emit it."""

        self.image_url = url

    def itunes_category(self, category: str) -> None:
        """Accept the configured category without additional behavior."""


class FakeFeedEntry:
    """Collect one RSS item payload before XML serialization."""

    def __init__(self) -> None:
        self.podcast = FakePodcastExtension()
        self.item: dict[str, str] = {}

    def title(self, title: str) -> None:
        """Store the entry title field."""

        self.item["title"] = title

    def description(self, description: str) -> None:
        """Store the entry description field."""

        self.item["description"] = description

    def pubDate(self, value) -> None:
        """Store the serialized publication date field."""

        self.item["pubDate"] = str(value)

    def enclosure(self, url: str, length: str, mime_type: str) -> None:
        """Store enclosure attributes for later XML serialization."""

        self.item["enclosure_url"] = url
        self.item["enclosure_length"] = length
        self.item["enclosure_type"] = mime_type

    def guid(self, guid: str, *, permalink: bool = False) -> None:
        """Store the guid field."""

        self.item["guid"] = guid


class FakeFeedGenerator:
    """Minimal feedgen replacement that writes enough XML for RSS tests."""

    def __init__(self) -> None:
        self.podcast = FakePodcastExtension()
        self.channel: dict[str, str] = {}
        self.entries: list[FakeFeedEntry] = []

    def load_extension(self, name: str, atom: bool = True, rss: bool = True) -> None:
        """Accept extension loading calls without additional behavior."""

    def title(self, title: str) -> None:
        """Store the channel title."""

        self.channel["title"] = title

    def link(self, *, href: str, rel: str) -> None:
        """Store channel link metadata by relation type."""

        self.channel[f"link_{rel}"] = href

    def description(self, description: str) -> None:
        """Store the channel description."""

        self.channel["description"] = description

    def language(self, language: str) -> None:
        """Store the channel language."""

        self.channel["language"] = language

    def add_entry(self) -> FakeFeedEntry:
        """Create and return a mutable entry object."""

        entry = FakeFeedEntry()
        self.entries.append(entry)
        return entry

    def rss_file(self, filename: str, *, pretty: bool = False) -> None:
        """Write a minimal RSS document for the collected channel and entries."""

        root = ET.Element("rss")
        channel_element = ET.SubElement(root, "channel")
        for field_name in ("title", "description", "language"):
            field_value = self.channel.get(field_name)
            if field_value:
                ET.SubElement(channel_element, field_name).text = field_value

        for entry in self.entries:
            item_element = ET.SubElement(channel_element, "item")
            if "title" in entry.item:
                ET.SubElement(item_element, "title").text = entry.item["title"]
            if "description" in entry.item:
                ET.SubElement(item_element, "description").text = entry.item[
                    "description"
                ]
            if "pubDate" in entry.item:
                ET.SubElement(item_element, "pubDate").text = entry.item["pubDate"]
            if "guid" in entry.item:
                ET.SubElement(item_element, "guid").text = entry.item["guid"]
            if "enclosure_url" in entry.item:
                ET.SubElement(
                    item_element,
                    "enclosure",
                    {
                        "url": entry.item["enclosure_url"],
                        "length": entry.item.get("enclosure_length", "0"),
                        "type": entry.item.get("enclosure_type", "audio/mpeg"),
                    },
                )

        pathlib.Path(filename).parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(root).write(filename, encoding="utf-8")


feedgen_module = types.ModuleType("feedgen")
feedgen_feed_module = types.ModuleType("feedgen.feed")
feedgen_feed_module.FeedGenerator = FakeFeedGenerator
feedgen_module.feed = feedgen_feed_module
sys.modules.setdefault("feedgen", feedgen_module)
sys.modules.setdefault("feedgen.feed", feedgen_feed_module)

write_rss_module = importlib.import_module("write_rss")
ChannelMeta = write_rss_module.ChannelMeta
EpisodePayload = write_rss_module.EpisodePayload
add_item = write_rss_module.add_item
ensure_base_feed = write_rss_module.ensure_base_feed


def build_channel_meta() -> ChannelMeta:
    """Return minimal channel metadata for RSS writer tests."""

    return ChannelMeta(
        title="Geektime TTS",
        site="https://www.geektime.co.il/",
        desc="Automated TTS of Geektime articles",
        author="CopyPasteFail",
        email="",
        image="",
        feed_url="https://tts-podcast-feeds.pages.dev/feeds/geektime.xml",
    )


def build_episode_payload(*, audio_url: str) -> EpisodePayload:
    """Return a stable episode payload for dedupe-focused RSS tests."""

    return EpisodePayload(
        article_title="Same Article",
        article_pub_utc="2026-03-29T16:32:18+00:00",
        article_link="https://www.geektime.co.il/netflix-and-sony-playstation-price-hike/",
        article_summary_html="<p>Summary</p>",
        article_summary="Summary",
        article_subtitle="",
        article_image_url="",
        audio_url=audio_url,
    )


def parse_feed_item_elements(feed_path: pathlib.Path) -> list[ET.Element]:
    """Return all RSS item elements from the generated feed file."""

    root = ET.parse(feed_path).getroot()
    return root.findall("./channel/item")


def test_add_item_replaces_existing_article_when_audio_url_changes(
    tmp_path: pathlib.Path,
) -> None:
    """Adding the same article with a new audio URL should not duplicate the RSS item.

    Inputs: one existing feed item for an article plus a replacement payload with a new IA URL.
    Outputs: a feed containing one matching item with the replacement enclosure URL.
    Edge cases: simulates the IA_ID_PREFIX mismatch that changes only the uploaded URL.
    """

    feed_path = tmp_path / "feeds" / "geektime.xml"
    channel_meta = build_channel_meta()
    ensure_base_feed(
        str(feed_path),
        channel_meta["title"],
        channel_meta["site"],
        channel_meta["desc"],
        channel_meta["author"],
        channel_meta["email"],
        channel_meta["image"],
        channel_meta["feed_url"],
    )

    add_item(
        str(feed_path),
        channel_meta,
        build_episode_payload(
            audio_url="https://archive.org/download/tts-geektime-v2-abc/episode.mp3"
        ),
    )
    add_item(
        str(feed_path),
        channel_meta,
        build_episode_payload(
            audio_url="https://archive.org/download/tts-geektime-abc/episode.mp3"
        ),
    )

    item_elements = parse_feed_item_elements(feed_path)

    assert len(item_elements) == 1
    enclosure_element = item_elements[0].find("enclosure")
    assert enclosure_element is not None
    assert (
        enclosure_element.get("url")
        == "https://archive.org/download/tts-geektime-abc/episode.mp3"
    )
