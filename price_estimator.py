"""Card-valuation Stage 2: four evidence tiers over normalized completed sales.

Inputs are ASSUMED normalized and verified upstream. All bundled data is fictional.
The L3/L4 numbers are indicative *benchmarks*, not market-validated card prices.

Examples:
  python price_estimator.py --offline --scenario ALL
  python price_estimator.py --dsn 'host=localhost port=5432 dbname=card_valuation user=postgres' --scenario ALL
  python price_estimator.py --dsn '...' --scenario L1_EXACT --save-snapshot
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from statistics import median

import hashlib

# Must stay byte-for-byte compatible with load_data.canonical_key (Stage 1).
IDENTITY_FIELDS = (
    "set_key", "player_key", "card_number", "parallel_code", "serial_limit",
    "image_variant_code", "back_variant_code", "autograph", "relic"
)


def canonical_key(card):
    missing = [k for k in IDENTITY_FIELDS if k not in card]
    if missing:
        raise ValueError(f"Missing normalized identity attributes: {missing}")
    payload = {k: card[k] for k in IDENTITY_FIELDS}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


MIN_EXACT_SALES = 3
MIN_FALLBACK_SALES = 3
MIN_GRADE_RATIO_PAIRS = 5
MAX_VARIANT_LIMIT_RATIO = 2.0
MIN_VARIANT_LIMIT_RATIO = 0.25
ALLOWED_EVIDENCE = ("ASSUMED_VERIFIED", "VERIFIED")


def window_bounds(as_of: date, days: int):
    """Inclusive as_of calendar day, using a UTC demo convention."""
    end = datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return end - timedelta(days=days), end


def percentile(values, fraction):
    """Simple interpolated percentile (not a confidence interval)."""
    arr = sorted(float(x) for x in values)
    if not arr:
        raise ValueError("Cannot compute percentile for empty sequence")
    pos = (len(arr) - 1) * fraction
    low = int(pos)
    high = min(low + 1, len(arr) - 1)
    return arr[low] + (arr[high] - arr[low]) * (pos - low)


def round2(x):
    return round(float(x), 2)


def comparable_band(values):
    values = list(values)

    if len(values) < 2:
        return None, None

    low = percentile(values, 0.25)
    high = percentile(values, 0.75)

    return round2(low), round2(high)


@dataclass
class Estimate:
    scenario: str
    card_id: str
    target_grade: str
    method: str
    estimate_type: str
    estimated_price_usd: float | None
    range_low_usd: float | None
    range_high_usd: float | None
    comparable_count: int
    comparable_card_count: int
    confidence: str
    lookback_days: int
    notes: str
    grade_reference: str | None = None
    grade_multiplier: float | None = None
    grade_ratio_pairs: int | None = None


def make_estimate(target, scenario, grade, method, estimate_type, prices,
                  comparable_count, comparable_card_count, confidence, days,
                  notes, expansion=0.0, grade_reference=None,
                  grade_multiplier=None, grade_ratio_pairs=None):
    center = round2(median(prices))
    lo, hi = comparable_band(prices)
    return Estimate(
        scenario=scenario,
        card_id=str(target["card_id"]),
        target_grade=grade,
        method=method,
        estimate_type=estimate_type,
        estimated_price_usd=center,
        range_low_usd=lo,
        range_high_usd=hi,
        comparable_count=comparable_count,
        comparable_card_count=comparable_card_count,
        confidence=confidence,
        lookback_days=days,
        notes=notes,
        grade_reference=grade_reference,
        grade_multiplier=grade_multiplier,
        grade_ratio_pairs=grade_ratio_pairs,
    )


def grade_sort_key(target_grade, grade):
    """Prefer nearby PSA grades, and don't convert between raw and graded."""
    if not target_grade.startswith("PSA_") or not grade.startswith("PSA_"):
        return (999, grade)
    try:
        return (abs(float(target_grade[4:]) - float(grade[4:])), grade)
    except ValueError:
        return (999, grade)


def variant_limit_compatible(target_limit, candidate_limit):
    if target_limit is None:
        return candidate_limit is None
    if candidate_limit is None:
        return False
    ratio = candidate_limit / target_limit
    return MIN_VARIANT_LIMIT_RATIO <= ratio <= MAX_VARIANT_LIMIT_RATIO


def aggregate_by_card(rows):
    """Equal weight per distinct card variant, avoiding high-volume sale domination."""
    by_card = defaultdict(list)
    for row in rows:
        by_card[row["card_id"]].append(float(row["price"]))
    return [float(median(prices)) for prices in by_card.values()], len(rows), len(by_card)


class PostgresRepository:
    def __init__(self, conn):
        self.conn = conn

    def find_card(self, card):
        key = canonical_key(card)
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT card_id, set_id, player_id, group_key, cohort_key,
                       parallel_code, serial_limit, image_variant_code,
                       back_variant_code, autograph, relic
                FROM card_variants WHERE canonical_key = %s
            """, (key,))
            row = cur.fetchone()
        if row is None:
            return None
        columns = ("card_id", "set_id", "player_id", "group_key", "cohort_key",
                   "parallel_code", "serial_limit", "image_variant_code",
                   "back_variant_code", "autograph", "relic")
        return dict(zip(columns, row))

    def sales(self, card_id, grade, start, end):
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT sale_price_usd FROM completed_sales
                WHERE card_id=%s AND grade_bucket=%s
                  AND verification_status = ANY(%s)
                  AND sold_at >= %s AND sold_at < %s
            """, (card_id, grade, list(ALLOWED_EVIDENCE), start, end))
            return [float(r[0]) for r in cur.fetchall()]

    def grade_buckets(self, card_id, start, end):
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT grade_bucket FROM completed_sales
                WHERE card_id=%s AND verification_status = ANY(%s)
                  AND sold_at >= %s AND sold_at < %s
                  AND grading_company='PSA'
            """, (card_id, list(ALLOWED_EVIDENCE), start, end))
            return [r[0] for r in cur.fetchall()]

    def paired_grade_ratios(self, cohort_key, target_grade, reference_grade, start, end):
        """Target/reference medians using paired *same-card* variant observations."""
        with self.conn.cursor() as cur:
            cur.execute("""
                WITH per_card AS (
                    SELECT s.card_id, s.grade_bucket,
                           COUNT(*) AS sale_count,
                           percentile_cont(0.5)
                               WITHIN GROUP (ORDER BY s.sale_price_usd) AS med
                    FROM completed_sales s
                    JOIN card_variants cv ON cv.card_id=s.card_id
                    WHERE cv.cohort_key=%s
                      AND s.grade_bucket IN (%s,%s)
                      AND s.verification_status = ANY(%s)
                      AND s.sold_at >= %s AND s.sold_at < %s
                    GROUP BY s.card_id, s.grade_bucket
                    HAVING COUNT(*) >= 2
                )
                SELECT t.med / r.med
                FROM per_card t JOIN per_card r ON t.card_id = r.card_id
                WHERE t.grade_bucket=%s AND r.grade_bucket=%s
                  AND r.med > 0
            """, (cohort_key, target_grade, reference_grade,
                  list(ALLOWED_EVIDENCE), start, end, target_grade, reference_grade))
            return [float(r[0]) for r in cur.fetchall()]

    def related_variant_sales(self, target, grade, start, end):
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT s.card_id, s.sale_price_usd, cv.serial_limit
                FROM card_variants cv
                JOIN completed_sales s ON s.card_id=cv.card_id
                WHERE cv.group_key=%s AND cv.set_id=%s
                  AND cv.card_id <> %s
                  AND cv.autograph=%s AND cv.relic=%s
                  AND cv.image_variant_code=%s AND cv.back_variant_code=%s
                  AND s.grade_bucket=%s
                  AND s.verification_status = ANY(%s)
                  AND s.sold_at >= %s AND s.sold_at < %s
            """, (target["group_key"], target["set_id"], target["card_id"],
                  target["autograph"], target["relic"],
                  target["image_variant_code"], target["back_variant_code"],
                  grade, list(ALLOWED_EVIDENCE), start, end))
            return [{"card_id":r[0], "price":float(r[1])} for r in cur.fetchall()
                    if variant_limit_compatible(target["serial_limit"], r[2])]

    def cohort_sales(self, target, grade, start, end):
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT s.card_id, s.sale_price_usd
                FROM card_variants cv
                JOIN completed_sales s ON s.card_id=cv.card_id
                WHERE cv.cohort_key=%s AND cv.set_id=%s
                  AND cv.player_id <> %s
                  AND cv.parallel_code=%s
                  AND cv.serial_limit IS NOT DISTINCT FROM %s
                  AND cv.image_variant_code=%s AND cv.back_variant_code=%s
                  AND cv.autograph=%s AND cv.relic=%s
                  AND s.grade_bucket=%s
                  AND s.verification_status = ANY(%s)
                  AND s.sold_at >= %s AND s.sold_at < %s
            """, (target["cohort_key"], target["set_id"], target["player_id"],
                  target["parallel_code"], target["serial_limit"],
                  target["image_variant_code"], target["back_variant_code"],
                  target["autograph"], target["relic"], grade,
                  list(ALLOWED_EVIDENCE), start, end))
            return [{"card_id":r[0], "price":float(r[1])} for r in cur.fetchall()]

    def save_snapshot(self, result: Estimate, valuation_day):
        # Do not persist L3/L4 comparison benchmarks as direct card valuations.
        if result.method not in ("L1_EXACT", "L2_GRADE_ADJUSTED"):
            return False
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO valuation_snapshots (
                    card_id, grade_bucket, valuation_date, method,
                    estimated_price_usd, range_low_usd, range_high_usd,
                    comparable_count, confidence, lookback_days
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (card_id,grade_bucket,valuation_date)
                DO UPDATE SET
                    method=EXCLUDED.method,
                    estimated_price_usd=EXCLUDED.estimated_price_usd,
                    range_low_usd=EXCLUDED.range_low_usd,
                    range_high_usd=EXCLUDED.range_high_usd,
                    comparable_count=EXCLUDED.comparable_count,
                    confidence=EXCLUDED.confidence,
                    lookback_days=EXCLUDED.lookback_days,
                    calculated_at=NOW()
            """, (int(result.card_id), result.target_grade, valuation_day,
                  result.method, result.estimated_price_usd,
                  result.range_low_usd, result.range_high_usd,
                  result.comparable_count, result.confidence,
                  result.lookback_days))
        return True


class OfflineRepository:
    """Pure-Python replica for tests and demonstrations without DB access."""
    def __init__(self, data_dir: Path):
        self.cards = {}
        self.rows = defaultdict(list)
        with (data_dir / "catalog.jsonl").open(encoding="utf-8") as handle:
            for idx, raw in enumerate(handle, 1):
                card = json.loads(raw)
                self.cards[canonical_key(card)] = {
                    **card,
                    "card_id": idx,
                    "set_id": card["set_key"],
                    "player_id": card["player_key"],
                    "group_key": ":".join((card["sport"], str(card["year"]), card["manufacturer"],card["player_key"].split(":")[-1])),
                    "cohort_key": ":".join((card["sport"], str(card["year"]), card["manufacturer"])),
                }
        with (data_dir / "sales.jsonl").open(encoding="utf-8") as handle:
            for raw in handle:
                sale = json.loads(raw)
                card = self.cards[canonical_key(sale["card"])]
                row = {
                    "card_id": card["card_id"], "card": card,
                    "grade": sale["grade_bucket"], "price": float(sale["sale_price_usd"]),
                    "sold_at": datetime.fromisoformat(sale["sold_at"]),
                }
                self.rows[card["card_id"]].append(row)

    def find_card(self, card):
        return self.cards.get(canonical_key(card))

    def _within(self, rows, grade, start, end):
        return [r for r in rows if start <= r["sold_at"] < end and r["grade"] == grade]

    def sales(self, card_id, grade, start, end):
        return [r["price"] for r in self._within(self.rows[card_id], grade, start, end)]

    def grade_buckets(self, card_id, start, end):
        return list({r["grade"] for r in self.rows[card_id]
                     if start <= r["sold_at"] < end and r["grade"].startswith("PSA_")})

    def paired_grade_ratios(self, cohort_key, target_grade, reference_grade, start, end):
        ratios=[]
        for card in self.cards.values():
            if card["cohort_key"] != cohort_key:
                continue
            tgt=self.sales(card["card_id"], target_grade, start, end)
            src=self.sales(card["card_id"], reference_grade, start, end)
            if len(tgt)>=2 and len(src)>=2 and median(src)>0:
                ratios.append(median(tgt)/median(src))
        return ratios

    def related_variant_sales(self, target, grade, start, end):
        rows=[]
        for c in self.cards.values():
            if c["group_key"] != target["group_key"] or c["set_id"] != target["set_id"]:
                continue
            if c["card_id"] == target["card_id"]:
                continue
            if any(c[f] != target[f] for f in
                   ("autograph","relic","image_variant_code","back_variant_code")):
                continue
            if not variant_limit_compatible(target["serial_limit"], c["serial_limit"]):
                continue
            rows.extend({"card_id":c["card_id"],"price":p}
                        for p in self.sales(c["card_id"],grade,start,end))
        return rows

    def cohort_sales(self, target, grade, start, end):
        rows=[]
        for c in self.cards.values():
            if c["cohort_key"] != target["cohort_key"] or c["set_id"] != target["set_id"]:
                continue
            if c["player_id"] == target["player_id"]:
                continue
            if any(c[f] != target[f] for f in
                   ("parallel_code", "serial_limit", "image_variant_code",
                    "back_variant_code", "autograph", "relic")):
                continue
            rows.extend({"card_id":c["card_id"],"price":p}
                        for p in self.sales(c["card_id"],grade,start,end))
        return rows


def estimate(repo, card, grade, as_of, scenario="CUSTOM"):
    target = repo.find_card(card)
    if target is None:
        return Estimate(scenario, "UNKNOWN", grade, "UNKNOWN_CARD", "NO_VALUATION",
                        None,None,None,0,0,"NONE",180,
                        "Target not in the current canonical catalog; identity resolution required.")

    for days in (90, 180):
        start,end=window_bounds(as_of,days)
        exact=repo.sales(target["card_id"],grade,start,end)
        if len(exact)>=MIN_EXACT_SALES:
            confidence="HIGH" if len(exact)>=5 else "MEDIUM"
            return make_estimate(target, scenario, grade, "L1_EXACT", "MARKET_ESTIMATE",
                                 exact,len(exact),1,confidence,days,
                                 "Median of exact card and exact-grade sales. Band is observed IQR.")

    if grade.startswith("PSA_"):
        for days in (90,180):
            start,end=window_bounds(as_of,days)
            candidates=sorted(repo.grade_buckets(target["card_id"],start,end),
                              key=lambda g:grade_sort_key(grade,g))
            for ref_grade in candidates:
                if ref_grade==grade or not ref_grade.startswith("PSA_"):
                    continue
                source=repo.sales(target["card_id"],ref_grade,start,end)
                if len(source)<MIN_FALLBACK_SALES:
                    continue
                ratios=repo.paired_grade_ratios(target["cohort_key"],grade,ref_grade,start,end)
                if len(ratios)<MIN_GRADE_RATIO_PAIRS:
                    continue
                factor=float(median(ratios))
                prices=[p*factor for p in source]
                return make_estimate(target, scenario, grade, "L2_GRADE_ADJUSTED",
                                     "MODELED_ESTIMATE",prices,len(source),1,"MEDIUM",days,
                                     "Grade factor is median of same-card target/reference ratios "
                                     "in this sport/year/manufacturer cohort. Heuristic ±15% band.",
                                     grade_reference=ref_grade,
                                     grade_multiplier=round2(factor),grade_ratio_pairs=len(ratios))

    for days in (90,180):
        start,end=window_bounds(as_of,days)
        rows=repo.related_variant_sales(target,grade,start,end)
        if len(rows)>=MIN_FALLBACK_SALES:
            medians,count,num_cards=aggregate_by_card(rows)
            return make_estimate(target,scenario,grade,"L3_SIMILAR_VARIANTS",
                                 "INDICATIVE_SIMILAR_CARD_BENCHMARK",medians,count,num_cards,"LOW",days,
                                 "Same player/set and grade; autograph/relic/image/back must agree, "
                                 "rarity limited to 0.25–2× target print run. No price conversion; "
                                 "NOT an exact-variant valuation. Heuristic ±35% band.")

    for days in (90,180):
        start,end=window_bounds(as_of,days)
        rows=repo.cohort_sales(target,grade,start,end)
        if len(rows)>=MIN_FALLBACK_SALES:
            medians,count,num_cards=aggregate_by_card(rows)
            return make_estimate(target,scenario,grade,"L4_OTHER_PLAYERS",
                                 "CATEGORY_BENCHMARK_NOT_CARD_VALUE",medians,count,num_cards,"VERY_LOW",days,
                                 "Different players in same product set, grade and variant. "
                                 "Category benchmark ONLY; should require human review, "
                                 "not an automatic purchase/repricing recommendation. "
                                 "Heuristic ±50% band.")

    return Estimate(scenario,str(target["card_id"]),grade,"NO_COMPS","NO_VALUATION",
                    None,None,None,0,0,"NONE",180,
                    "Insufficient relevant completed sales after 90-/180-day and fallback search.")


def display(result):
    print("\n" + "="*70)
    print(f"{result.scenario} | card_id={result.card_id} | target={result.target_grade}")
    print(f"Method: {result.method} | Confidence: {result.confidence}")
    print(f"Output type: {result.estimate_type}")
    if result.range_low_usd is None or result.range_high_usd is None:
        print("Middle 50% (IQR): Unavailable")
    else:
        print(f"Middle 50% (IQR): ${result.range_low_usd:,.2f} – ${result.range_high_usd:,.2f}")
    print(f"Sales included: {result.comparable_count}; "
          f"unique card variants: {result.comparable_card_count}; "
          f"lookback: {result.lookback_days} days")
    if result.grade_reference:
        print(f"Grade conversion: {result.grade_reference} → {result.target_grade}; "
              f"factor={result.grade_multiplier:.2f} from {result.grade_ratio_pairs} paired variants")
    print("Notes:",result.notes)


def main():
    parser=argparse.ArgumentParser(description="Trading-card valuation prototype")
    parser.add_argument("--dsn",help="PostgreSQL connection string; use --offline for no DB")
    parser.add_argument("--offline",action="store_true",help="Read packaged JSONL without Postgres")
    parser.add_argument("--data-dir",type=Path,default=Path("data"))
    parser.add_argument("--scenario",default="ALL",help="ALL, L1_EXACT, L2_GRADE_FALLBACK, L3_VARIANT_FALLBACK, L4_COHORT_FALLBACK")
    parser.add_argument("--as-of",type=date.fromisoformat,default=date(2026,10,28),
                        help="Demo dataset reference date YYYY-MM-DD (default 2026-10-28)")
    parser.add_argument("--save-snapshot",action="store_true",help="Save result to valuation_snapshots (Postgres only)")
    args=parser.parse_args()
    if args.offline and args.save_snapshot:
        parser.error("--save-snapshot requires PostgreSQL mode")
    if not args.offline and not args.dsn:
        parser.error("Pass --dsn or use --offline")

    scenarios=json.loads((args.data_dir/"demo_targets.json").read_text())
    chosen=list(scenarios) if args.scenario=="ALL" else [args.scenario]
    if any(k not in scenarios for k in chosen):
        parser.error(f"Unknown scenario; choices: ALL, {', '.join(scenarios)}")
    if args.offline:
        repo=OfflineRepository(args.data_dir)
        for name in chosen:
            fixture=scenarios[name]
            display(estimate(repo,fixture["card"],fixture["target_grade_bucket"],args.as_of,name))
    else:
        import psycopg
        with psycopg.connect(args.dsn) as conn:
            repo=PostgresRepository(conn)
            saved_count = 0
            for name in chosen:
                fixture=scenarios[name]
                result=estimate(repo,fixture["card"],fixture["target_grade_bucket"],args.as_of,name)
                display(result)
                if args.save_snapshot and repo.save_snapshot(result,args.as_of):
                    saved_count += 1
        if args.save_snapshot:
            print(f"\nSaved {saved_count} L1/L2 valuations to valuation_snapshots; "
                  "L3/L4 benchmarks intentionally not saved.")


if __name__=="__main__":
    main()
