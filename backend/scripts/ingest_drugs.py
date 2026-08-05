"""Load Drugs-2.xlsx into kb_drugs (clean prescribing catalogue). Re-runnable.

Env:
  DATABASE_URL  Supabase session-pooler URI (with password).
  DRUGS_XLSX    Path to Drugs-2.xlsx.

Run (from backend/):  uv run python scripts/ingest_drugs.py
"""
import os
from pathlib import Path

XLSX = Path(os.getenv("DRUGS_XLSX", os.path.expanduser("~/Desktop/Clinical Memory AI/Drugs-2.xlsx")))
DRY = os.getenv("DRY_RUN") == "1"


def _clean(x):
    if x is None:
        return None
    s = str(x).strip()
    return s or None


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def rows():
    import openpyxl
    ws = openpyxl.load_workbook(XLSX, read_only=True, data_only=True).worksheets[0]
    first = True
    for r in ws.iter_rows(values_only=True):
        if first:  # header: Package Name, Generic Name, Strength, Dosage Form, Package Size, MRP, Price to Pharmacy, Manufacturer
            first = False
            continue
        if not r or not r[0]:
            continue
        brand, generic, strength, form, pack, mrp, _pharm, manu = (list(r) + [None] * 8)[:8]
        yield (_clean(brand), _clean(generic), _clean(strength), _clean(form), _clean(pack), _num(mrp), _clean(manu))


def main():
    print(f"DRUGS_XLSX = {XLSX}")
    data = list(rows())
    print(f"drugs: {len(data)}")
    if DRY:
        print("DRY_RUN — no writes.")
        return

    import psycopg2
    from psycopg2.extras import execute_values
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("truncate kb_drugs restart identity;")
    for i in range(0, len(data), 2000):
        execute_values(cur, "insert into kb_drugs (brand_name,generic_name,strength,dosage_form,pack_size,mrp,manufacturer) values %s", data[i:i + 2000])
    conn.commit()
    cur.close()
    conn.close()
    print("✅ kb_drugs loaded.")


if __name__ == "__main__":
    main()
