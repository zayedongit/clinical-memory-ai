# Backend — Clinical Memory AI

FastAPI service. Everything the browser cannot be trusted with: authorisation,
attestation, provider keys, the audit trail, and the clinical rules.

```bash
uv sync --frozen --group dev --group ml
cp .env.example .env          # fill in your Supabase keys
uv run fastapi dev app/main.py
```

The server refuses to start without `SUPABASE_URL`, `SUPABASE_ANON_KEY` and
`SUPABASE_SERVICE_ROLE_KEY` — a misconfigured server should fail at boot, not on
a patient's first request. Everything else is optional and degrades gracefully.

## Layout

| Path | What lives there |
|---|---|
| `app/core/` | Settings, Supabase access, safe PostgREST filters, metrics, rate limiting, spend cap, logging |
| `app/ai/` | LLM provider chain, speech-to-text chain, prompts, JSON repair, citation verification |
| `app/clinical/` | Documentation completeness rubric, longitudinal statistics, escalation-risk wrapper |
| `app/ml/` | Feature engineering, synthetic cohort, exported model artefact, pure-Python inference |
| `app/api/routers/` | HTTP endpoints |
| `eval/` | Synthetic datasets and committed evaluation results |
| `scripts/` | Model training, evaluation harnesses, data ingestion, demo seed |
| `tests/` | Unit and integration tests; `tests/db/` runs against real PostgreSQL |

## Commands

```bash
uv run pytest -q                     # 366 tests (db tests skip without a database)
uv run ruff check .

# Database tests need a scratch PostgreSQL. They create and drop their own
# databases from the committed migrations, so never point this at real data.
export CMA_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres
uv run pytest tests/db -q

uv run python scripts/eval_red_flags.py       # sensitivity + false-alarm rate
uv run python scripts/eval_extraction.py      # extraction + citation grounding
uv run --group ml python scripts/train_risk_model.py
```

## Notes worth knowing before changing things

**Never add a second use of `service_role`** without a very good reason. It
bypasses Row-Level Security entirely; the one existing use resolves
`auth_uid → users` during authentication, before the caller's clinic is known.

**Never build a PostgREST filter by interpolation.** Use `app/core/pgrst.py`.
Filters are a text grammar, not bound parameters.

**Never write a clinical record outside `finalize_visit()`.** Attestation, role,
immutability and optimistic locking are enforced there, in one transaction.

**Adding a feature to the risk model invalidates the artefact.** Retrain with
`scripts/train_risk_model.py`; inference refuses to serve a model whose feature
order no longer matches the code.
