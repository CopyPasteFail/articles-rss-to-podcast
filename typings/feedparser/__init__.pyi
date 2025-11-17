from typing import Any


class FeedParserDict(dict[str, Any]):
    entries: list[Any]


def parse(
    url_file_stream_or_string: Any,
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
