DECLARE cur_invoice STRING DEFAULT FORMAT_DATE('%Y%m', CURRENT_DATE());

WITH base AS (
  -- Only the current invoice month, only Text-to-Speech,
  -- and only SKUs that count characters
  SELECT
    DATE(usage_start_time) AS day,
    LOWER(sku.description) AS sku_desc,
    CAST(usage.amount AS NUMERIC) AS chars
  FROM `{billing_export_table}`
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
  SELECT 'wavenet_or_neural2' AS voice_group, CAST({free_tier_premium} AS NUMERIC) AS free_tier_chars UNION ALL
  SELECT 'standard', CAST({free_tier_standard} AS NUMERIC) UNION ALL
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
