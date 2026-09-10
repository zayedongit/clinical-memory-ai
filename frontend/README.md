# Frontend — Clinical Memory AI

Next.js 16 (App Router), TypeScript, Tailwind. The physician's screen.

```bash
pnpm install
cp .env.local.example .env.local     # fill in
pnpm dev                             # http://localhost:3000
```

The backend must be running (`make dev-backend` from the repository root).

## Layout

| Path | What lives there |
|---|---|
| `app/consult/` | The consultation wizard: live scribe → prescription → review & sign |
| `app/patients/` | Patient list, patient record, longitudinal analytics panel |
| `app/consultations/` | Clinic dashboard |
| `app/scribe/` | Standalone scribe and live-consultation views |
| `app/visits/[id]/print/` | Printable prescription / visit record |
| `lib/api.ts` | The only place that talks to the backend; token attachment and error translation |
| `lib/safety.ts` | Prescription allergy and duplicate-ingredient checks |
| `tests/` | Vitest + Testing Library |

## Commands

```bash
pnpm exec tsc --noEmit
pnpm run lint
pnpm test                # 36 tests
pnpm run build           # works with no credentials — see below
```

## Notes worth knowing before changing things

**Client-side auth checks shape the UI only.** Every page checks for a session
and redirects, but that is convenience. The backend re-derives identity from the
token on every request and is the sole authority on what a caller may do.

**The Supabase client is created lazily.** Creating it at module scope made
`next build` fail on any checkout without credentials, because Next evaluates
client-component modules during prerender. Building a project must not require
production secrets.

**Error messages come from `errorMessage()`.** The backend distinguishes 409
(someone else edited this) from 403 (your role may not) from 429 (slow down).
Collapsing them into a status code throws the distinction away at the moment the
clinician needs it.
