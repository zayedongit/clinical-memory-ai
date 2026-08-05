#!/usr/bin/env python3
"""Load the real hospital catalogue (hospital_baseline26072024.xlsx) into
public.kb_formulary — Pharma + Consumables + Implants + Equipment, with MRP
and therapeutic subcategory. Clean reload (truncates the table first).

Env:
  DATABASE_URL   session-pooler URI (postgres.<ref>:<pw>@...pooler...:5432/postgres)
  FORMULARY_XLSX default: ~/Desktop/Clinical Memory AI/hospital_baseline26072024.xlsx
  DRY_RUN=1      parse + count only, no DB writes

Usage:
  cd backend && uv run python scripts/ingest_formulary.py
"""
import os
from pathlib import Path

import openpyxl

DEFAULT_XLSX = str(Path.home() / "Desktop" / "Clinical Memory AI" / "hospital_baseline26072024.xlsx")

# xlsx column -> kb_formulary column
# headers: brand_name, dose_size, generic_name, mrp, unit_per_pack, uom_pack_type,
#          category_name, subcategory_name
COLS = ["brand_name", "generic_name", "dose_size", "mrp", "unit_per_pack",
        "uom_pack_type", "category", "subcategory"]


def _s(v) -> str | None:
    if v is None:
        return None
    t = str(v).strip()
    if t == "" or t.lower() == "null":
        return None
    return t


def _num(v):
    t = _s(v)
    if t is None:
        return None
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return None


def rows_from_xlsx(path: str):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    it = ws.iter_rows(values_only=True)
    next(it)  # header
    for r in it:
        brand = _s(r[0])
        if not brand:
            continue  # brand_name is NOT NULL
        yield (
            brand,          # brand_name
            _s(r[2]),       # generic_name  (col index 2)
            _s(r[1]),       # dose_size     (col index 1)
            _num(r[3]),     # mrp
            _s(r[4]),       # unit_per_pack
            _s(r[5]),       # uom_pack_type
            _s(r[6]),       # category      (category_name)
            _s(r[7]),       # subcategory   (subcategory_name)
        )


def main() -> int:
    path = os.getenv("FORMULARY_XLSX", DEFAULT_XLSX)
    if not Path(path).exists():
        print(f"ERROR: xlsx not found: {path}")
        return 2

    data = list(rows_from_xlsx(path))
    print(f"parsed formulary items: {len(data)}")
    # quick category tally
    tally: dict[str, int] = {}
    for row in data:
        tally[row[6] or "NA"] = tally.get(row[6] or "NA", 0) + 1
    print("by category:", tally)

    if os.getenv("DRY_RUN") == "1":
        print("DRY_RUN=1 — no DB writes.")
        return 0

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: set DATABASE_URL (session-pooler URI).")
        return 2

    import psycopg2
    from psycopg2.extras import execute_values

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("truncate table public.kb_formulary restart identity;")
    execute_values(
        cur,
        f"insert into public.kb_formulary ({', '.join(COLS)}) values %s",
        data,
        page_size=1000,
    )
    conn.commit()
    cur.execute("select count(*) from public.kb_formulary;")
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    print(f"✅ kb_formulary loaded: {total} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
