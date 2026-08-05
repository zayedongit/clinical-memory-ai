"""Offline one-off loader: ingest the clinical KB into Supabase Postgres.

Reads the kb/ JSON files + the hospital formulary xlsx, normalizes them, and
loads the six kb_* tables. Re-runnable (truncates first). Condition IDs are
re-namespaced from their original prefix to `cma:`.

Env:
  DATABASE_URL  Supabase *session pooler* connection URI (contains password).
                Supabase dashboard -> Settings -> Database -> Connection string
                -> Session pooler.  (Do NOT commit this.)
  KB_DIR        Path to the kb/ folder.
  KB_XLSX       Path to hospital_baseline...xlsx.
  DRY_RUN=1     Parse + count only; do not connect to the DB.

Run (from backend/):  uv run python scripts/ingest_kb.py
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

KB_DIR = Path(os.getenv("KB_DIR", os.path.expanduser("~/Desktop/Clinical Memory AI/kb")))
KB_XLSX = Path(os.getenv("KB_XLSX", os.path.expanduser("~/Desktop/Clinical Memory AI/hospital_baseline26072024.xlsx")))
DRY_RUN = os.getenv("DRY_RUN") == "1"

TERM_FILES = {
    "symptom": "indices/symptom_index.json",
    "sign": "indices/sign_index.json",
    "redflag": "indices/redflag_index.json",
    "riskfactor": "indices/riskfactor_index.json",
}


def cma(cid: str) -> str:
    """Re-namespace a condition id: 'src:x' -> 'cma:x'."""
    if cid and ":" in cid:
        return "cma:" + cid.split(":", 1)[1]
    return cid


def load_conditions() -> tuple[list[tuple], set[str]]:
    rows, valid = [], set()
    for f in glob.glob(str(KB_DIR / "conditions" / "*.json")):
        d = json.load(open(f))
        cid = cma(d.get("id", ""))
        if not cid:
            continue
        valid.add(cid)
        age = d.get("age_applicability") or {}
        prev = d.get("prevalence_india") or {}
        rows.append((
            cid,
            d.get("condition_name") or "(unnamed)",
            d.get("synonyms") or [],
            d.get("icd10") or [],
            d.get("specialty") or None,
            d.get("category") or None,
            d.get("acuity") or None,
            d.get("cant_miss"),
            prev.get("tier") or None,
            age.get("min_years"),
            age.get("max_years"),
            d.get("sex_applicability") or None,
            json.dumps(d),                       # record
            json.dumps(d.get("provenance") or {}),
            (d.get("provenance") or {}).get("version"),
        ))
    return rows, valid


def load_vocab() -> list[tuple]:
    d = json.load(open(KB_DIR / "vocab" / "vocabulary.json"))
    return [(
        cid, v.get("kind"), v.get("label") or cid,
        v.get("synonyms") or [], v.get("snomed"), v.get("icd"),
        v.get("condition_count"),
    ) for cid, v in d.items()]


def load_terms(valid: set[str]) -> tuple[list[tuple], int]:
    rows, skipped = [], 0
    for term_type, rel in TERM_FILES.items():
        d = json.load(open(KB_DIR / rel))
        for canonical_id, entries in d.items():
            for e in entries:
                cid = cma(e.get("condition_id", ""))
                if cid not in valid:
                    skipped += 1
                    continue
                rows.append((
                    term_type, canonical_id, cid,
                    bool(e.get("cant_miss")), e.get("weight"),
                    e.get("discriminating"), e.get("action") or None,
                ))
    return rows, skipped


def load_drugs() -> list[tuple]:
    d = json.load(open(KB_DIR / "drug_resolver.json"))
    b2g = d.get("brand_to_generic") or {}
    return [(brand, generic, generic is not None) for brand, generic in b2g.items()]


def load_spelling() -> list[tuple]:
    d = json.load(open(KB_DIR / "drug_resolver.json"))
    out: dict[str, str] = {}
    out.update(d.get("spelling_bridge") or {})
    out.update(d.get("salt_base") or {})
    return list(out.items())


def iter_formulary():
    import openpyxl
    wb = openpyxl.load_workbook(KB_XLSX, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    first = True
    for r in ws.iter_rows(values_only=True):
        if first:  # header: brand_name,dose_size,generic_name,mrp,unit_per_pack,uom_pack_type,category_name,subcategory_name
            first = False
            continue
        if not r or not r[0]:
            continue
        brand, dose, generic, mrp, upp, uom, cat, sub = (list(r) + [None] * 8)[:8]
        def _clean(x):
            return None if x is None or str(x).strip().lower() in ("", "null") else str(x).strip()
        try:
            mrp_v = float(mrp) if mrp not in (None, "", "null") else None
        except (TypeError, ValueError):
            mrp_v = None
        yield (_clean(brand), _clean(generic), _clean(dose), mrp_v, _clean(upp), _clean(uom), _clean(cat), _clean(sub))


def main() -> None:
    print(f"KB_DIR = {KB_DIR}")
    print(f"KB_XLSX = {KB_XLSX}")

    conditions, valid = load_conditions()
    vocab = load_vocab()
    terms, skipped = load_terms(valid)
    drugs = load_drugs()
    spelling = load_spelling()

    print(f"conditions      : {len(conditions)}")
    print(f"vocabulary      : {len(vocab)}")
    print(f"term_index      : {len(terms)}  (skipped {skipped} orphans w/o a loaded condition)")
    print(f"drug_generic    : {len(drugs)}")
    print(f"spelling_bridge : {len(spelling)}")

    if DRY_RUN:
        n = sum(1 for _ in iter_formulary())
        print(f"formulary       : {n}  (dry-run count)")
        print("DRY_RUN — no database writes.")
        return

    import psycopg2
    from psycopg2.extras import execute_values

    dsn = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()

    print("truncating kb_* tables…")
    cur.execute("truncate kb_term_index, kb_conditions, kb_vocabulary, "
                "kb_drug_generic, kb_spelling_bridge, kb_formulary restart identity cascade;")

    def bulk(sql: str, rows: list[tuple], size: int = 2000):
        for i in range(0, len(rows), size):
            execute_values(cur, sql, rows[i:i + size])

    print("loading conditions…")
    bulk("insert into kb_conditions (id,name,synonyms,icd,specialty,category,acuity,cant_miss,"
         "prevalence_tier,age_min,age_max,sex,record,provenance,version) values %s", conditions)
    print("loading vocabulary…")
    bulk("insert into kb_vocabulary (canonical_id,kind,label,synonyms,snomed,icd,condition_count) values %s", vocab)
    print("loading term_index…")
    bulk("insert into kb_term_index (term_type,canonical_id,condition_id,cant_miss,weight,discriminating,action) values %s", terms)
    print("loading drug_generic…")
    bulk("insert into kb_drug_generic (brand_name,generic_inn,resolved) values %s", drugs)
    print("loading spelling_bridge…")
    bulk("insert into kb_spelling_bridge (variant,canonical) values %s", spelling)

    print("loading formulary (streamed)…")
    batch, total = [], 0
    for row in iter_formulary():
        batch.append(row)
        if len(batch) >= 2000:
            execute_values(cur, "insert into kb_formulary (brand_name,generic_name,dose_size,mrp,"
                           "unit_per_pack,uom_pack_type,category,subcategory) values %s", batch)
            total += len(batch)
            batch = []
    if batch:
        execute_values(cur, "insert into kb_formulary (brand_name,generic_name,dose_size,mrp,"
                       "unit_per_pack,uom_pack_type,category,subcategory) values %s", batch)
        total += len(batch)
    print(f"formulary       : {total}")

    conn.commit()
    cur.close()
    conn.close()
    print("✅ KB ingested.")


if __name__ == "__main__":
    main()
