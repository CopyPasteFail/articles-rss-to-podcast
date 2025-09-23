# tts_usage.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Any
import os

from google.cloud import bigquery

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

def _rows_from_query(result: Iterable[bigquery.table.Row]) -> List[UsageRow]:
    rows: List[UsageRow] = []
    for row in result:
        rows.append(
            UsageRow(
                section=row["section"],
                label=row["label"],
                characters=int(row["characters"] or 0),
                free_tier_remaining=int(row["free_tier_remaining"]) if row["free_tier_remaining"] is not None else None,
            )
        )
    return rows

def fetch_tts_usage(client: Optional[bigquery.Client] = None) -> Dict[str, Any]:
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

def _print_table(headers, rows):
    # rows = list of iterables (strings or numbers)
    rows = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(c))
            else:
                widths.append(len(c))
    def fmt(row):
        return "  ".join(f"{val:<{widths[i]}}" for i, val in enumerate(row))
    print(fmt(headers))
    for r in rows:
        print(fmt(r))

def print_usage_report(data):
    summary = data.get("summary", {})
    by_group = data.get("by_group", [])
    daily = data.get("daily", [])

    print("=== Invoice month total ===")
    print(f"characters: {summary.get('characters', 0)}")
    print(f"free_tier_remaining: {summary.get('free_tier_remaining', 0)}")
    print()

    print("=== By group (invoice month) ===")
    if not by_group:
        print("none")
    else:
        headers = ["group", "used", "free_left"]
        rows = [
            [r.get("label"), str(r.get("characters", 0)), str(r.get("free_tier_remaining", 0))]
            for r in by_group
        ]
        _print_table(headers, rows)
    print()

    print("=== Daily (invoice month) ===")
    if not daily:
        print("none")
    else:
        headers = ["day", "chars"]
        rows = [[r.get("label"), str(r.get("characters", 0))] for r in daily]
        _print_table(headers, rows)


def main():
    data = fetch_tts_usage()
    print_usage_report(data)

if __name__ == "__main__":
    main()
