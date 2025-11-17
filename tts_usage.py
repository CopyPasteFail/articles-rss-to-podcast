# tts_usage.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple, TypedDict, Union, cast
import os

from google.cloud import bigquery
from google.cloud.bigquery.table import Row as BigQueryRow

# Fully qualified table name (keep as-is or read from env)
FQTN = os.environ.get(
    "BILLING_EXPORT_TABLE",
    "rss-hebrew-podcast-omer.billing_export.gcp_billing_export_v1_010406_CE1277_E64516",
)

# Free-tier defaults (override with env if Google changes them)
FREE_TIER_STANDARD = int(os.environ.get("FREE_TIER_STANDARD", "4000000"))
FREE_TIER_PREMIUM = int(os.environ.get("FREE_TIER_PREMIUM", "1000000"))  # WaveNet/Neural2

SQL = f"""
DECLARE cur_invoice STRING DEFAULT FORMAT_DATE('%Y%m', CURRENT_DATE());

WITH base AS (
  -- Only the current invoice month, only Text-to-Speech,
  -- and only SKUs that count characters
  SELECT
    DATE(usage_start_time) AS day,
    LOWER(sku.description) AS sku_desc,
    CAST(usage.amount AS NUMERIC) AS chars
  FROM `{FQTN}`
  WHERE invoice.month = cur_invoice
    AND LOWER(service.description) LIKE '%text-to-speech%'
    AND LOWER(sku.description) LIKE '%count of characters%'
),
by_group AS (
  SELECT
    CASE
      WHEN sku_desc LIKE '%wavenet%' OR sku_desc LIKE '%neural2%' THEN 'wavenet_or_neural2'
      WHEN sku_desc LIKE '%standard%' THEN 'standard'
      ELSE 'other'
    END AS voice_group,
    SUM(chars) AS chars_mtd
  FROM base
  GROUP BY 1
),
limits AS (
  SELECT 'wavenet_or_neural2' AS voice_group, CAST({FREE_TIER_PREMIUM} AS NUMERIC) AS free_tier_chars UNION ALL
  SELECT 'standard', CAST({FREE_TIER_STANDARD} AS NUMERIC) UNION ALL
  SELECT 'other', CAST(0 AS NUMERIC)
),
group_with_limits AS (
  SELECT
    l.voice_group,
    COALESCE(g.chars_mtd, 0) AS chars_mtd,
    l.free_tier_chars,
    GREATEST(l.free_tier_chars - COALESCE(g.chars_mtd, 0), 0) AS free_tier_remaining
  FROM limits l
  LEFT JOIN by_group g USING (voice_group)
),
daily AS (
  -- daily for the invoice month (same filter as base)
  SELECT day, SUM(chars) AS chars
  FROM base
  GROUP BY day
)
-- 1) invoice-month total across all groups and sum of remaining free-tier
SELECT 'summary_total' AS section, NULL AS label,
       SUM(chars_mtd) AS characters, SUM(free_tier_remaining) AS free_tier_remaining
FROM group_with_limits

UNION ALL
-- 2) per-group usage and remaining vs the proper bucket limit
SELECT 'by_group' AS section, voice_group AS label,
       chars_mtd AS characters, free_tier_remaining
FROM group_with_limits

UNION ALL
-- 3) daily breakdown for the invoice month
SELECT 'daily' AS section, CAST(day AS STRING) AS label,
       chars AS characters, NULL AS free_tier_remaining
FROM daily

ORDER BY section, label
"""

@dataclass
class UsageRow:
    section: str
    label: Optional[str]
    characters: int
    free_tier_remaining: Optional[int]

class UsageSummary(TypedDict):
    characters: int
    free_tier_remaining: int


class UsageGroup(TypedDict):
    label: Optional[str]
    characters: int
    free_tier_remaining: int


class UsageDaily(TypedDict):
    label: Optional[str]
    characters: int


class UsageReport(TypedDict):
    summary: UsageSummary
    by_group: List[UsageGroup]
    daily: List[UsageDaily]


_EMPTY_SUMMARY: UsageSummary = {"characters": 0, "free_tier_remaining": 0}
_EMPTY_GROUPS: Tuple[UsageGroup, ...] = ()
_EMPTY_DAILY: Tuple[UsageDaily, ...] = ()

Numeric = Union[int, float, Decimal]


def _int_or_zero(value: Optional[Numeric]) -> int:
    return 0 if value is None else int(value)


def _optional_int(value: Optional[Numeric]) -> Optional[int]:
    return None if value is None else int(value)


def _rows_from_query(result: Iterable[BigQueryRow]) -> List[UsageRow]:
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
    """Return current billing-cycle usage grouped for CLI and programmatic use."""
    provided_client = client is not None
    client = client or bigquery.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    try:
        query_job = client.query(SQL)
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
            } for r in by_group
        ],
        "daily": [
            {
                "label": r.label,
                "characters": r.characters,
            } for r in daily
        ],
    }


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
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
    summary_value = cast(Optional[UsageSummary], data.get("summary"))
    summary: UsageSummary = summary_value if summary_value is not None else _EMPTY_SUMMARY
    by_group_value = cast(Optional[Sequence[UsageGroup]], data.get("by_group"))
    by_group: Sequence[UsageGroup] = by_group_value if by_group_value is not None else _EMPTY_GROUPS
    daily_value = cast(Optional[Sequence[UsageDaily]], data.get("daily"))
    daily: Sequence[UsageDaily] = daily_value if daily_value is not None else _EMPTY_DAILY

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
    data = fetch_tts_usage()
    print_usage_report(data)

if __name__ == "__main__":
    main()
