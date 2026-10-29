"""Deterministic synthetic catalog + completed sales for the card valuation MVP.

Usage: python generate_data.py --count 10000 --out-dir data
No external data/API calls, no dependencies beyond the Python standard library.
Everything generated is FICTIONAL and should not be presented as real sales.
"""
import argparse
import json
import math
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

SETS = [
    ("BASEBALL", 2025, "TOPPS", "CHROME"),
    ("BASEBALL", 2026, "TOPPS", "HOLIDAY"),
    ("BASKETBALL", 2025, "PANINI", "PRIZM"),
    ("BASKETBALL", 2026, "PANINI", "SELECT"),
    ("FOOTBALL", 2025, "PANINI", "PRIZM"),
    ("FOOTBALL", 2026, "PANINI", "SELECT"),
]

# Variant template: parallel, print-run limit, image variation, back variation, autograph, relic
VARIANTS = [
    ("BASE", None, "NONE", "NONE", False, False),
    ("GREEN", 99, "NONE", "NONE", False, False),
    ("ORANGE", 25, "NONE", "NONE", False, False),
    ("BLUE", 150, "NONE", "NONE", False, False),
    ("GREEN", 99, "NONE", "NONE", True, False),
    ("GREEN", None, "SP", "GREEN_BACK", False, False),
]
VARIANT_FACTORS = [1.0, 2.4, 4.3, 1.8, 6.0, 3.2]
GRADE_CHOICES = [("RAW", None, "NEAR_MINT"), ("PSA", 8.0, None),
                 ("PSA", 9.0, None), ("PSA", 10.0, None)]
GRADE_FACTORS = {"RAW_NEAR_MINT": 1.0, "PSA_8": 1.25,
                 "PSA_9": 1.75, "PSA_10": 3.3}


def bucket(company, grade, condition):
    return f"RAW_{condition}" if company == "RAW" else f"{company}_{int(grade)}"


def make_catalog():
    cards = []
    for sport, year, manufacturer, set_name in SETS:
        for n in range(1, 31):
            player_key = f"{sport}:PLAYER_{n:03}"
            set_key = f"{sport}:{year}:{manufacturer}:{set_name}"
            prefix = {"CHROME": "C", "HOLIDAY": "H", "PRIZM": "P", "SELECT": "S"}[set_name]
            for variant_index, (parallel, serial_limit, image, back, auto, relic) in enumerate(VARIANTS):
                cards.append({
                    "sport": sport, "year": year, "manufacturer": manufacturer,
                    "set_name": set_name, "set_key": set_key,
                    "player_key": player_key,
                    "player_name": f"Demo {sport.title()} Player {n:03}",
                    "card_number": f"{prefix}{n:03}",
                    "parallel_code": parallel, "serial_limit": serial_limit,
                    "image_variant_code": image, "back_variant_code": back,
                    "autograph": auto, "relic": relic,
                    "_variant_index": variant_index, "_player_number": n,
                })
    return cards


def public_card(card):
    return {k: v for k, v in card.items() if not k.startswith("_")}


def grade_choice(rng):
    return rng.choice(GRADE_CHOICES)


def make_sale(card, number, rng, as_of, grade_override=None, price_override=None):
    company, grade, condition = grade_override or grade_choice(rng)
    grade_bucket = bucket(company, grade, condition)
    player_idx = card["_player_number"]
    base_price = {"BASEBALL": 38, "BASKETBALL": 30, "FOOTBALL": 24}[card["sport"]]
    player_factor = 0.7 + (player_idx % 13) * 0.14
    variant_factor = VARIANT_FACTORS[card["_variant_index"]]
    random_noise = math.exp(rng.gauss(0, 0.15))
    amount = round(base_price * player_factor * variant_factor * GRADE_FACTORS[grade_bucket] * random_noise, 2)
    if price_override is not None:
        amount = round(price_override, 2)
    days_ago = rng.randint(1, 175)
    dt = datetime.combine(as_of, time(0, 0), timezone.utc) - timedelta(
        days=days_ago, hours=rng.randint(0, 22), minutes=rng.randint(0, 59))
    title = (f'{card["year"]} {card["manufacturer"]} {card["set_name"]} '
             f'{card["player_name"]} #{card["card_number"]} {card["parallel_code"]}'
             f'{" AUTO" if card["autograph"] else ""} '
             f'{grade_bucket}')
    return {
        "source": "SYNTHETIC_DEMO",
        "external_sale_id": f"SIM-{number:06}",
        "sold_at": dt.isoformat(),
        "sale_price_usd": amount,
        "grading_company": company,
        "numeric_grade": grade,
        "raw_condition": condition,
        "grade_bucket": grade_bucket,
        "original_title": title,
        "card": public_card(card),
    }


def write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, sort_keys=True) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=20261028)
    ap.add_argument("--as-of", default="2026-10-28", help="Date anchoring the preceding 180-day sales window")
    ap.add_argument("--out-dir", type=Path, default=Path("data"))
    args = ap.parse_args()
    if args.count < 40:
        ap.error("--count must be at least 40 to preserve the four demo scenarios")
    rng = random.Random(args.seed)
    as_of = date.fromisoformat(args.as_of)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    cards = make_catalog()

    def find(player, variant, year=2026, sport="BASEBALL"):
        return next(c for c in cards if c["sport"] == sport and c["year"] == year
                    and c["_player_number"] == player and c["_variant_index"] == variant)

    # These targets intentionally exercise the valuation fallback hierarchy.
    l1 = find(1, 1)  # Exact card + PSA 9 has several confirmed sales
    l2 = find(2, 1)  # Exact card exists, but only PSA 10 has sales
    l3 = find(3, 1)  # Target card has NO sales; other same-player variants do
    l4 = find(4, 1)  # No 2026 TOPPS sales at all for this player
    fixtures = {
        "L1_EXACT": {"card": public_card(l1), "target_grade_bucket": "PSA_9",
                     "expected": "Exact card + exact PSA grade"},
        "L2_GRADE_FALLBACK": {"card": public_card(l2), "target_grade_bucket": "PSA_9",
                              "expected": "Same card has PSA 10 sales but not PSA 9"},
        "L3_VARIANT_FALLBACK": {"card": public_card(l3), "target_grade_bucket": "PSA_9",
                                "expected": "Same player/year/manufacturer has different variants"},
        "L4_COHORT_FALLBACK": {"card": public_card(l4), "target_grade_bucket": "PSA_9",
                               "expected": "Other players in 2026 TOPPS cohort only"},
    }
    sales = []

    def add_fixed(card, grades, prices):
        for grade, price in zip(grades, prices):
            sales.append(make_sale(card, len(sales) + 1, rng, as_of, grade_override=grade,
                                   price_override=price))

    psa9 = ("PSA", 9.0, None)
    psa10 = ("PSA", 10.0, None)
    add_fixed(l1, [psa9] * 8, [108, 112, 116, 120, 122, 125, 129, 131])
    add_fixed(l2, [psa10] * 6, [210, 225, 240, 230, 250, 245])
    add_fixed(find(3, 2), [psa9] * 8, [165, 170, 175, 180, 185, 190, 195, 200])
    add_fixed(find(5, 1), [psa9] * 8, [100, 102, 105, 110, 113, 120, 125, 130])

    # Keep scenario constraints true even after adding random records.
    eligible = [c for c in cards
                if c is not l3
                and not (c["sport"] == "BASEBALL" and c["year"] == 2026
                         and c["_player_number"] == 4)]
    while len(sales) < args.count:
        card = rng.choice(eligible)
        grade = grade_choice(rng)
        if card is l2 and bucket(*grade) == "PSA_9":
            grade = psa10
        sales.append(make_sale(card, len(sales) + 1, rng, as_of, grade_override=grade))

    write_jsonl(args.out_dir / "catalog.jsonl", map(public_card, cards))
    write_jsonl(args.out_dir / "sales.jsonl", sales)
    (args.out_dir / "demo_targets.json").write_text(
        json.dumps(fixtures, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(cards):,} catalog variants and {len(sales):,} sales.")
    print(f"Files written to: {args.out_dir.resolve()}")
    for key, value in fixtures.items():
        print(f"  {key}: {value['card']['player_key']} / {value['target_grade_bucket']}")
    print("All card identities and sale prices are fictional.")


if __name__ == "__main__":
    main()
