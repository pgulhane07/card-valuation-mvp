"""Import synthetic catalog and completed-sale JSONL into PostgreSQL.

Dependencies: pip install -r requirements.txt
Run: python load_data.py --dsn 'postgresql://postgres:password@localhost:5432/card_valuation'

This demonstrates normalized-DTO -> find-or-create card identity -> sale insert.
No fuzzy matching or image verification is performed in the prototype.
"""
import argparse
import hashlib
import json
from pathlib import Path

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError as exc:
    raise SystemExit("Install PostgreSQL driver: pip install -r requirements.txt") from exc

IDENTITY_FIELDS = (
    "set_key", "player_key", "card_number", "parallel_code", "serial_limit",
    "image_variant_code", "back_variant_code", "autograph", "relic"
)
REQUIRED_FIELDS = (
    "sport", "year", "manufacturer", "set_name", "set_key", "player_key",
    "player_name", "card_number", "parallel_code", "image_variant_code",
    "back_variant_code", "autograph", "relic"
)


def validate_card(card):
    missing = [field for field in REQUIRED_FIELDS if field not in card or card[field] is None]
    # serial_limit must be present, but explicit null means 'known unnumbered' in this DEMO DTO.
    if "serial_limit" not in card:
        missing.append("serial_limit")
    if missing:
        raise ValueError(f"Unresolved card identity: {missing}")
    if card["serial_limit"] is not None and card["serial_limit"] <= 0:
        raise ValueError("serial_limit must be positive or null")


def canonical_key(card):
    validate_card(card)
    payload = {key: card[key] for key in IDENTITY_FIELDS}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def group_key(card):
    # player_key is SPORT:PLAYER_###, so use just the canonical player component here.
    player_component = card["player_key"].split(":", 1)[-1]
    return f'{card["sport"]}:{card["year"]}:{card["manufacturer"]}:{player_component}'


def cohort_key(card):
    return f'{card["sport"]}:{card["year"]}:{card["manufacturer"]}'


def get_id(cur, cache, cache_key, insert_sql, select_sql, parameters):
    if cache_key in cache:
        return cache[cache_key]
    cur.execute(insert_sql, parameters)
    cur.execute(select_sql, (cache_key,))
    result = cur.fetchone()
    if result is None:
        raise RuntimeError(f"Cannot resolve entity key: {cache_key}")
    cache[cache_key] = result[0]
    return result[0]


def get_card_id(cur, card, player_ids, set_ids, card_ids):
    key = canonical_key(card)
    if key in card_ids:
        return card_ids[key]

    player_id = get_id(
        cur, player_ids, card["player_key"],
        "INSERT INTO players (player_key,full_name,sport) VALUES (%s,%s,%s) ON CONFLICT (player_key) DO NOTHING",
        "SELECT player_id FROM players WHERE player_key=%s",
        (card["player_key"], card["player_name"], card["sport"]))

    set_id = get_id(
        cur, set_ids, card["set_key"],
        """INSERT INTO card_sets (set_key,sport,release_year,manufacturer,set_name)
           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (set_key) DO NOTHING""",
        "SELECT set_id FROM card_sets WHERE set_key=%s",
        (card["set_key"], card["sport"], card["year"], card["manufacturer"], card["set_name"]))

    cur.execute("""
        INSERT INTO card_variants (
            canonical_key, group_key, cohort_key, set_id, player_id,
            card_number, parallel_code, serial_limit,
            image_variant_code, back_variant_code, autograph, relic
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (canonical_key) DO NOTHING
    """, (
        key, group_key(card), cohort_key(card), set_id, player_id,
        card["card_number"], card["parallel_code"], card["serial_limit"],
        card["image_variant_code"], card["back_variant_code"],
        card["autograph"], card["relic"]
    ))
    cur.execute("SELECT card_id FROM card_variants WHERE canonical_key=%s", (key,))
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("Unable to resolve card variant")
    card_ids[key] = row[0]
    return row[0]


def read_jsonl(path):
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON in {path}:{number}") from exc


def validate_sale(sale):
    required = ("source", "external_sale_id", "sold_at", "sale_price_usd",
                "grading_company", "grade_bucket", "card")
    missing = [field for field in required if field not in sale]
    if missing:
        raise ValueError(f"Missing sale fields: {missing}")
    if float(sale["sale_price_usd"]) <= 0:
        raise ValueError("Sale price must be positive")
    expected_bucket = ("RAW_" + str(sale["raw_condition"])) if sale["grading_company"] == "RAW" else (
        str(sale["grading_company"]) + "_" + str(int(sale["numeric_grade"])))
    if expected_bucket != sale["grade_bucket"]:
        raise ValueError("Inconsistent grading_company, grade, grade_bucket")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True, help="PostgreSQL connection URL")
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    args = ap.parse_args()
    player_ids, set_ids, card_ids = {}, {}, {}

    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            catalog_count = 0
            for card in read_jsonl(args.data_dir / "catalog.jsonl"):
                get_card_id(cur, card, player_ids, set_ids, card_ids)
                catalog_count += 1

            insert_sql = """
                INSERT INTO completed_sales (
                    source, external_sale_id, card_id, sold_at, sale_price_usd,
                    grading_company, numeric_grade, raw_condition, grade_bucket,
                    raw_payload
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source, external_sale_id) DO NOTHING
            """
            batch = []
            sales_read = 0
            for sale in read_jsonl(args.data_dir / "sales.jsonl"):
                validate_sale(sale)
                card_id = get_card_id(cur, sale["card"], player_ids, set_ids, card_ids)
                batch.append((
                    sale["source"], sale["external_sale_id"], card_id,
                    sale["sold_at"], sale["sale_price_usd"],
                    sale["grading_company"], sale["numeric_grade"],
                    sale["raw_condition"], sale["grade_bucket"], Jsonb(sale)
                ))
                sales_read += 1
                if len(batch) >= 1000:
                    cur.executemany(insert_sql, batch)
                    batch.clear()
            if batch:
                cur.executemany(insert_sql, batch)

            cur.execute("SELECT COUNT(*) FROM completed_sales WHERE source='SYNTHETIC_DEMO'")
            total_stored = cur.fetchone()[0]
        # commits on clean context-manager exit

    print(f"Catalog input entries: {catalog_count:,}")
    print(f"Unique variants resolved in this load: {len(card_ids):,}")
    print(f"Sales input entries: {sales_read:,}")
    print(f"Total SYNTHETIC_DEMO sales now stored: {total_stored:,}")
    print("Safe to rerun: duplicate external sale IDs are ignored.")


if __name__ == "__main__":
    main()
