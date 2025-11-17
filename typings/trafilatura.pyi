from __future__ import annotations

from typing import Any


def fetch_url(url: str, decode: bool = ..., no_ssl: bool = ..., config: Any | None = ...) -> str | None: ...


def extract(
    filecontent: str,
    url: str | None = ...,
    record_id: str | None = ...,
    *,
    output_format: str = ...,
    **kwargs: Any,
) -> str | None: ...

