# Clinical Memory AI

**Clinical documentation and longitudinal patient memory for outpatient care.**
Turn a doctor's free-form consultation into structured, reviewable clinical notes — and carry a patient's history forward across every visit.

> ⚕️ **Physician-in-the-loop decision support, not a medical device.** Everything the system produces (structured notes, differential diagnoses, suggested investigations, treatment options) is surfaced *for a licensed clinician to review and decide on*. It does not diagnose or prescribe autonomously.

---

## Why

Outpatient consultations produce rich clinical information that is either lost to free-text notes or never structured well enough to be reused at the next visit. Clinical Memory AI treats documentation as the foundation for **memory**: every clinically meaningful fact is captured in a structured, provenance-tracked store so that a patient's picture stays consistent across encounters — and so that the quality of what's captured can actually be *measured* rather than assumed.

---

## Architecture

```
                    ┌──────────────────────────────┐
                    │  Next.js / TypeScript (web)   │  scribe · patients · lookup
                    └───────────────┬──────────────┘
                                    │  REST (JWT auth)
                    ┌───────────────▼──────────────┐
                    │        FastAPI (Python)       │
                    │  routers: scribe · synthesis  │
                    │  match · visits · patients ·  │
                    │  drugs · conditions · me      │
                    └───┬───────────┬───────────┬───┘
                        │           │           │
             ┌──────────▼──┐  ┌─────▼─────┐  ┌──▼───────────────┐
             │  Supabase / │  │  LLM      │  │  Decision-support │
             │  Postgres   │  │  scribe   │  │  proxy (upstream) │
             │  (RLS +     │  │  (STT +   │  │  DDx · Ix · Rx    │
             │  KB + facts)│  │  Gemini)  │  │  (physician-only) │
             └─────────────┘  └───────────┘  └──────────────────┘
```

- **Frontend** — Next.js + TypeScript + Tailwind. Deployed on Cloudflare (OpenNext).
- **Backend** — FastAPI (Python 3.12), managed with `uv`.
- **Database** — Supabase/Postgres, 13 versioned migrations, Row-Level Security on every table.
- **Contracts** — shared API types in `shared/` keep the frontend and backend in sync.

---

## Key engineering

**Multi-tenant isolation, enforced at the database.**
Every table carries a clinic scope and is protected by Row-Level Security via a `current_clinic_id()` policy — isolation is guaranteed by Postgres itself, not by hoping the application layer filters correctly. Clinical data lives in an **append-only `clinical_facts` provenance core**, so history is never silently mutated. A dedicated `rls_isolation_test.sql` proves two clinics can never read each other's records.

**A clinical knowledge base with fuzzy, safety-aware retrieval.**
The platform runs over a curated knowledge base of **355 conditions** (~2,600 drug entries, an 8k-cluster clinical vocabulary). Free-text symptoms are matched to conditions with Postgres `pg_trgm` trigram similarity (`match_terms`), which:
- anchors candidate terms near the best match, but **always preserves "can't-miss" red-flag links** even when they score below the cutoff — a danger is never silently dropped;
- returns prevalence tier and age/sex applicability so results can be ranked by real-world relevance.

*(The knowledge-base content itself is not bundled in this repo; it is loaded via the ingestion scripts in `backend/scripts/`.)*

**An LLM scribe that produces structured, checkable output.**
Speech-to-text (Sarvam) plus an LLM structuring step (Gemini) map free-form encounter dialogue into structured JSON clinical notes (symptoms, severity, duration, onset, and more) rather than a wall of text — output whose correctness can be scored against a rubric.

**Decision support that fails open.**
The `synthesis` router calls an upstream clinical brain for differential diagnosis, investigations, and empiric treatment — firing the three lanes **in parallel** the moment symptoms are known. The upstream base URL is a server-side secret and is never reachable from the browser. On any upstream error the API returns empty lists instead of a 5xx, so a partial encounter still works mid-consultation.

**Quality is measured, not assumed.**
A CI-safe evaluation harness (`scripts/eval_red_flags.py`) scores the KB grounding on a curated set of classic red-flag presentations:
- **Recall** — does a cardiac chest-pain / meningitis / SAH / PE presentation surface the can't-miss condition?
- **Precision / alarm-fatigue** — do benign presentations *avoid* raising loud false alarms?

It runs directly against Postgres with no LLM and no API key, so it's cheap and can gate CI on a recall threshold.

---

## Repository layout

```
clinical-memory-ai/
├── frontend/      Next.js + TypeScript + Tailwind (scribe, patients, lookup)
├── backend/       FastAPI app
│   ├── app/api/routers/   scribe · synthesis · match · visits · patients · drugs · conditions · me · health
│   ├── app/core/          config, Supabase client
│   ├── scripts/           KB / drug ingestion, red-flag eval
│   └── eval/              red_flag_cases.json
├── supabase/      13 migrations (schema, RLS, KB schema, match functions, SOAP, drugs, safety) + isolation test
├── shared/        cross-app API contracts
└── docs/          specs & runbooks
```

---

## Getting started

**Prerequisites**
- Node.js + `pnpm`
- Python 3.12 + [`uv`](https://github.com/astral-sh/uv)
- A Supabase project (and the Supabase CLI), or local Supabase
- *(Optional, for the scribe)* Gemini and Sarvam API keys

**1. Database**
```bash
cd supabase
supabase link --project-ref <your-project-ref>
supabase db push          # applies the 13 migrations in order
```

**2. Backend**
```bash
cd backend
cp .env.example .env       # fill in Supabase URL/keys; AI keys optional
uv run fastapi dev app/main.py    # http://localhost:8000
```

**3. Frontend**
```bash
cd frontend
cp .env.local.example .env.local  # Supabase URL + anon key, API base URL
pnpm install
pnpm dev                   # http://localhost:3000
```

**4. (Optional) Load the knowledge base**
```bash
cd backend
uv run python scripts/ingest_kb.py
uv run python scripts/ingest_drugs.py
```

**5. Run the red-flag evaluation**
```bash
cd backend
export DATABASE_URL='postgresql://...'   # Supabase session-pooler URI
uv run python scripts/eval_red_flags.py --verbose
```

Secrets are read entirely from environment variables — the only committed env files are the `.env.example` templates. Never commit a real `.env`.

---

## Tech stack

| Layer | Tools |
|---|---|
| Frontend | Next.js, TypeScript, Tailwind, Supabase JS, Cloudflare (OpenNext) |
| Backend | FastAPI, Python 3.12, `uv`, httpx, pydantic-settings, psycopg2 |
| Data | Supabase / PostgreSQL, Row-Level Security, `pg_trgm` |
| AI | Gemini (structuring), Sarvam (STT), upstream clinical decision-support |
| Tooling | ruff, pytest, GitHub Actions CI |

---

## Status

Active MVP, built and maintained solo. Core encounter flow (scribe → structured notes → KB grounding → physician-reviewed decision support), multi-tenant data layer, and the evaluation harness are in place; the knowledge base and safety gates continue to expand.

---

## Disclaimer

This project is clinical **decision-support** software intended to assist licensed healthcare professionals. It is not a certified medical device, makes no autonomous clinical decisions, and must not be used as a substitute for professional medical judgement. The treating clinician remains fully responsible for every diagnosis and treatment decision.
