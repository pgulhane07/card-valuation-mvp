-- Inspect ingested records and canonical identities.
SELECT (SELECT COUNT(*) FROM players) AS players,
       (SELECT COUNT(*) FROM card_sets) AS sets,
       (SELECT COUNT(*) FROM card_variants) AS card_variants,
       (SELECT COUNT(*) FROM completed_sales) AS completed_sales;

-- All variants for one broad player group.
SELECT cv.card_id, cv.group_key, cv.card_number, cv.parallel_code,
       cv.serial_limit, cv.image_variant_code, cv.back_variant_code,
       cv.autograph, cv.relic
FROM card_variants cv
WHERE cv.group_key='BASEBALL:2026:TOPPS:PLAYER_001'
ORDER BY cv.card_number, cv.parallel_code, cv.autograph;

-- L1: exact card variant + PSA 9 transactions in the previous 180 days.
-- Change the cut-off when running the demo at a different date.
WITH target AS (
    SELECT cv.card_id
    FROM card_variants cv
    JOIN players p ON p.player_id=cv.player_id
    JOIN card_sets cs ON cs.set_id=cv.set_id
    WHERE p.player_key='BASEBALL:PLAYER_001'
      AND cs.set_key='BASEBALL:2026:TOPPS:HOLIDAY'
      AND cv.parallel_code='GREEN' AND cv.serial_limit=99
      AND cv.image_variant_code='NONE' AND cv.back_variant_code='NONE'
      AND cv.autograph=FALSE AND cv.relic=FALSE
)
SELECT s.external_sale_id, s.sold_at, s.grade_bucket, s.sale_price_usd
FROM completed_sales s JOIN target t ON s.card_id=t.card_id
WHERE s.grade_bucket='PSA_9'
  AND s.sold_at >= TIMESTAMPTZ '2026-10-28 00:00:00+00' - INTERVAL '180 days'
ORDER BY s.sold_at DESC;

-- L3: when exact sales are unavailable, collect related variants
-- for a single player (not an unadjusted pooled price estimate).
SELECT cv.card_id, cv.parallel_code, cv.serial_limit, cv.autograph,
       s.grade_bucket, s.sale_price_usd, s.sold_at
FROM card_variants cv JOIN completed_sales s ON s.card_id=cv.card_id
WHERE cv.group_key='BASEBALL:2026:TOPPS:PLAYER_003'
  AND s.grade_bucket='PSA_9'
  AND cv.autograph=FALSE AND cv.relic=FALSE
ORDER BY s.sold_at DESC LIMIT 30;

-- L4: search the manufacturer/year/sport cohort without the player.
-- Treat these as a category reference only, not a direct player valuation.
SELECT cv.card_id, cv.group_key, cv.parallel_code, s.sale_price_usd
FROM card_variants cv JOIN completed_sales s ON s.card_id=cv.card_id
WHERE cv.cohort_key='BASEBALL:2026:TOPPS'
  AND s.grade_bucket='PSA_9'
  AND cv.autograph=FALSE AND cv.relic=FALSE
ORDER BY s.sold_at DESC LIMIT 30;
