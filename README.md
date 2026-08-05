# Clinical Memory AI

A web-first clinical documentation platform for general physicians and small clinics.
It turns a doctor–patient conversation into a structured, medico-legally-shaped note,
maintains a longitudinal patient record, and surfaces physician-review-only clinical
decision support — differential diagnosis, investigations and treatment. **The physician
is the decision-maker at every step; nothing is finalized without explicit attestation.**

> Not an autonomous diagnosis or prescription system. All AI output is a draft for physician review.

## What it does

- **AI Scribe** — records the consultation (multilingual / Hinglish), transcribes it, and
  auto-populates a **structured encounter** (chief complaints, history, vitals, examination).
- **Consultation wizard** — a 3-step flow: *Consultation → Prescription → Review & Sign*, with
  a persistent patient banner (UHID, demographics), drafts, and resume.
- **Longitudinal memory** — every visit is stored per patient with trends and history-aware notes.
- **Clinical decision support** — ranked differential (with ICD-10), investigations (urgency),
  and evidence-based treatment with local drug brands + prices. Grounded, physician-review-only.
- **Prescription** — search a real hospital formulary (brands, strengths, MRP, therapeutic class)
  with allergy/duplicate safety checks against the patient's own record.
- **Safety & trust** — recording consent, physician attestation (enforced), audit trail,
  printable prescription / visit record.

## Architecture

| Layer | Tech |
|---|---|
| Frontend | Next.js (App Router), TypeScript, Tailwind |
| Backend | FastAPI (Python 3.12), `uv` |
| Database / Auth / Storage | Supabase (PostgreSQL + Row-Level Security) |
| Speech-to-text | OpenAI `gpt-4o-transcribe` (Sarvam fallback) |
| LLM structuring | Google Gemini |
| Decision support | Clinical Synthesis Clinical Synthesis API (external, guideline-grounded) |

Multi-tenant by design: every clinical table is clinic-scoped via Postgres Row-Level Security.

## Getting started

```bash
# backend
cd backend
cp .env.example .env          # fill in your own keys (see below)
uv sync
uv run fastapi dev app/main.py

# frontend
cd frontend
cp .env.local.example .env.local
pnpm install
pnpm dev
```

Apply the database schema with the Supabase CLI:

```bash
supabase db push
```

### Configuration

All secrets live in `backend/.env` (git-ignored). See `backend/.env.example` for the full list —
Supabase URL/keys, `GEMINI_API_KEY`, `OPENAI_API_KEY`, and the Clinical Synthesis API base URL. **Never commit
real keys.** The frontend uses only the public Supabase anon key.

## Status

Active development. This repository is a working prototype, not a certified medical device.

## License

© Clinical Memory AI. All rights reserved. This source is made public for
viewing and reference only; it is not licensed for reuse, redistribution, or
commercial use without explicit written permission.
