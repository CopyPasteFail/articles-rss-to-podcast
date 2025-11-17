from __future__ import annotations

from typing import Any, Iterable


class Tag:
    name: str | None
    parent: Tag | None

    def find_all(
        self,
        name: Any = ...,
        attrs: dict[str, Any] | None = ...,
        recursive: bool = ...,
        string: Any = ...,
        limit: int | None = ...,
        **kwargs: Any,
    ) -> list[Tag]: ...

    def get(self, key: str, default: Any | None = ...) -> Any: ...

    def get_text(self, separator: str | None = ..., strip: bool = ...) -> str: ...

    def decompose(self) -> None: ...

    def extract(self) -> Tag: ...

    def replace_with(self, value: Any) -> Tag: ...


class NavigableString:
    parent: Tag | None

    def extract(self) -> NavigableString: ...


class BeautifulSoup(Tag):
    def __init__(
        self,
        markup: str | bytes | Iterable[str | bytes] | None = ...,
        features: str | None = ...,
        **kwargs: Any,
    ) -> None: ...

