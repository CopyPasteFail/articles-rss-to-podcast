"""The audio child must use its parent's article instead of fetching the feed again."""

from __future__ import annotations

import json
import pathlib

import pytest

import one_episode


def test_prepared_entry_runs_without_a_source_request(tmp_path, monkeypatch) -> None:
    entry = {
        "article_link": "https://example.com/article",
        "article_title": "Prepared article",
        "article_summary": "Body",
        "article_summary_html": "<p>Prepared body</p>",
        "article_author": "Author",
        "article_pub_utc": "2026-11-24T12:00:00+00:00",
        "article_subtitle": "Prepared subtitle",
        "article_image_url": "https://example.com/image.jpg",
    }
    entry_file = tmp_path / "entry.json"
    entry_file.write_text(json.dumps(entry), encoding="utf-8")

    def no_select() -> one_episode.EntryMeta:
        raise AssertionError("A prepared entry must not fetch or select from RSS")

    def content(source, link, **kwargs):
        assert source.content == [{"value": entry["article_summary_html"]}]
        assert kwargs["allow_fetch"] is True
        return "Prepared body", "<p>Prepared body</p>", "", ""

    def synthesize(segments, output: pathlib.Path) -> None:
        output.write_bytes(b"test MP3")

    monkeypatch.setattr(one_episode, "OUT", tmp_path)
    monkeypatch.setattr(one_episode, "RSS_URL", "")
    monkeypatch.setattr(one_episode, "TARGET_LINK", entry["article_link"])
    monkeypatch.setattr(one_episode, "select_entry", no_select)
    monkeypatch.setattr(one_episode, "resolve_article_content", content)
    monkeypatch.setattr(one_episode, "synthesize_ssml", synthesize)
    monkeypatch.setattr(one_episode, "normalize_mp3", lambda path: None)
    one_episode.main(["--entry-file", str(entry_file)])

    sidecars = list(tmp_path.glob("*.mp3.rssmeta.json"))
    assert len(sidecars) == 1
    sidecar = json.loads(sidecars[0].read_text())
    for field in ("article_link", "article_title", "article_author", "article_pub_utc"):
        assert sidecar[field] == entry[field]
    assert sidecar["article_subtitle"] == "Prepared subtitle"
    assert sidecar["article_image_url"] == entry["article_image_url"]
    assert sidecar["tts_generated"] is True


@pytest.mark.parametrize(
    "payload", ["{broken", "[]", '{"article_link": "https://example.com/article"}']
)
def test_invalid_prepared_entry_stops_before_synthesis(
    payload: str, tmp_path, monkeypatch
) -> None:
    path = tmp_path / "entry.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(SystemExit, match="Invalid prepared entry"):
        one_episode.load_entry_file(path)
