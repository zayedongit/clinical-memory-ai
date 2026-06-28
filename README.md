# Clinical Memory AI

Web-first clinical documentation + longitudinal patient memory (MVP).

## Structure
- `frontend/` — Next.js + TypeScript + Tailwind
- `backend/`  — FastAPI (Python)
- `supabase/` — database migrations & config
- `shared/`   — cross-app API contracts
- `docs/`     — specs & runbooks

## Local development
- Frontend: `cd frontend && pnpm dev`  → http://localhost:3000
- Backend:  `cd backend && uv run fastapi dev app/main.py`  → http://localhost:8000
