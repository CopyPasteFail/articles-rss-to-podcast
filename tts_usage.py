"""Query GCP billing export so the pipeline can report Text-to-Speech usage."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from decimal import Decimal
from typing import (
    Any,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypedDict,
    Union,
    cast,
)
import os

from google.cloud import bigquery
from google.cloud.bigquery.table import Row as BigQueryRow


SQL_TEMPLATE_PATH = pathlib.Path(__file__).with_name("tts_usage.sql")
BILLING_EXPORT_TABLE_ENV_NAME = "BILLING_EXPORT_TABLE"
BILLING_PROJECT_ENV_NAME = "TTS_BILLING_PROJECT_ID"
FREE_TIER_STANDARD = int(os.environ.get("FREE_TIER_STANDARD", "4000000"))
FREE_TIER_PREMIUM = int(os.environ.get("FREE_TIER_PREMIUM", "1000000"))


def _billing_export_table() -> str:
    """Return the configured fully qualified BigQuery billing export table."""
    table_name = os.environ.get(BILLING_EXPORT_TABLE_ENV_NAME, "").strip()
    if not table_name:
        raise RuntimeError(
            f"{BILLING_EXPORT_TABLE_ENV_NAME} is not set; skipping billing query"
        )
    return table_name


def _billing_project_id() -> str | None:
    """Return the project used to run the billing query."""
    return (
        os.environ.get(BILLING_PROJECT_ENV_NAME, "").strip()
        or os.environ.get("GCP_PROJECT_ID", "").strip()
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        or None
    )


def _render_sql() -> str:
    """Render the billing SQL from current environment config."""
    return SQL_TEMPLATE_PATH.read_text(encoding="utf-8").format(
        billing_export_table=_billing_export_table(),
        free_tier_premium=FREE_TIER_PREMIUM,
        free_tier_standard=FREE_TIER_STANDARD,
    )


@dataclass
class UsageRow:
    """Flattened view of the SQL results used for summary/by_group/daily tables."""

    section: str
    label: Optional[str]
    characters: int
    free_tier_remaining: Optional[int]


class UsageSummary(TypedDict):
    """High-level usage numbers shown when the pipeline finishes a run."""

    characters: int
    free_tier_remaining: int


class UsageGroup(TypedDict):
    """Per-voice-group stats so we know when each tier hits its limit."""

    label: Optional[str]
    characters: int
    free_tier_remaining: int


class UsageDaily(TypedDict):
    """Daily breakdown for operators wondering when spend spiked."""

    label: Optional[str]
    characters: int


class UsageReport(TypedDict):
    """Convenience container returned by fetch_tts_usage."""

    summary: UsageSummary
    by_group: List[UsageGroup]
    daily: List[UsageDaily]


_EMPTY_SUMMARY: UsageSummary = {"characters": 0, "free_tier_remaining": 0}
_EMPTY_GROUPS: Tuple[UsageGroup, ...] = ()
_EMPTY_DAILY: Tuple[UsageDaily, ...] = ()

Numeric = Union[int, float, Decimal]


def _int_or_zero(value: Optional[Numeric]) -> int:
    """Guard against NULL numeric fields coming back from BigQuery."""
    return 0 if value is None else int(value)


def _optional_int(value: Optional[Numeric]) -> Optional[int]:
    """Convert numeric values to ints while preserving None."""
    return None if value is None else int(value)


def _rows_from_query(result: Iterable[BigQueryRow]) -> List[UsageRow]:
    """Turn the BigQuery response into friendly UsageRow objects."""
    rows: List[UsageRow] = []
    for row in result:
        section = cast(str, row["section"])
        label = cast(Optional[str], row["label"])
        characters_value = cast(Optional[Numeric], row["characters"])
        free_tier_value = cast(Optional[Numeric], row["free_tier_remaining"])
        rows.append(
            UsageRow(
                section=section,
                label=label,
                characters=_int_or_zero(characters_value),
                free_tier_remaining=_optional_int(free_tier_value),
            )
        )
    return rows


def fetch_tts_usage(client: Optional[bigquery.Client] = None) -> UsageReport:
    """Run the billing SQL so pipeline.py can update stats and CLI can print them."""
    provided_client = client is not None
    client = client or bigquery.Client(project=_billing_project_id())
    try:
        query_job = client.query(_render_sql())
        rows = _rows_from_query(query_job.result())
    finally:
        if not provided_client:
            client.close()

    summary = next((r for r in rows if r.section == "summary_total"), None)
    by_group = [r for r in rows if r.section == "by_group"]
    daily = [r for r in rows if r.section == "daily"]

    return {
        "summary": {
            "characters": summary.characters if summary else 0,
            "free_tier_remaining": (summary.free_tier_remaining or 0) if summary else 0,
        },
        "by_group": [
            {
                "label": r.label,
                "characters": r.characters,
                "free_tier_remaining": r.free_tier_remaining or 0,
            }
            for r in by_group
        ],
        "daily": [
            {
                "label": r.label,
                "characters": r.characters,
            }
            for r in daily
        ],
    }


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    """Pretty-print tabular data for the CLI report."""
    # rows = list of iterables (strings or numbers)
    string_rows: List[List[str]] = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in string_rows:
        for i, c in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(c))
            else:
                widths.append(len(c))

    def fmt(row: Sequence[str]) -> str:
        return "  ".join(f"{val:<{widths[i]}}" for i, val in enumerate(row))

    print(fmt(headers))
    for r in string_rows:
        print(fmt(r))


def print_usage_report(data: Mapping[str, Any]) -> None:
    """Simple CLI front-end for fetch_tts_usage (used locally and in automation)."""
    summary_value = cast(Optional[UsageSummary], data.get("summary"))
    summary: UsageSummary = (
        summary_value if summary_value is not None else _EMPTY_SUMMARY
    )
    by_group_value = cast(Optional[Sequence[UsageGroup]], data.get("by_group"))
    by_group: Sequence[UsageGroup] = (
        by_group_value if by_group_value is not None else _EMPTY_GROUPS
    )
    daily_value = cast(Optional[Sequence[UsageDaily]], data.get("daily"))
    daily: Sequence[UsageDaily] = (
        daily_value if daily_value is not None else _EMPTY_DAILY
    )

    print("=== Invoice month total ===")
    print(f"characters: {summary['characters']}")
    print(f"free_tier_remaining: {summary['free_tier_remaining']}")
    print()

    print("=== By group (invoice month) ===")
    if not by_group:
        print("none")
    else:
        headers = ["group", "used", "free_left"]
        rows: List[Sequence[Any]] = [
            (r["label"], r["characters"], r["free_tier_remaining"]) for r in by_group
        ]
        _print_table(headers, rows)
    print()

    print("=== Daily (invoice month) ===")
    if not daily:
        print("none")
    else:
        headers = ["day", "chars"]
        rows = [(r["label"], r["characters"]) for r in daily]
        _print_table(headers, rows)


def main():
    """CLI entry point for ad-hoc inspection."""
    data = fetch_tts_usage()
    print_usage_report(data)


if __name__ == "__main__":
    main()
