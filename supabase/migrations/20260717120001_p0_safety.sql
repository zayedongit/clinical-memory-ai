-- =====================================================================
-- P0 safety & trust layer
--   1. Patient consent to record the consultation (stored on the visit).
--   2. Clinical Considerations Engine output (red flags, missing info,
--      suggested investigations, completeness) — physician-review-only,
--      stored alongside the note.
--   3. Physician attestation: the doctor's explicit "I reviewed & approve"
--      recorded on the note (AI content is never permanent without it).
-- All additive + idempotent; no data loss.
-- =====================================================================

-- 1. Consent (on the visit — one recording session = one consent).
alter table public.visits add column if not exists consent_given  boolean not null default false;
alter table public.visits add column if not exists consent_at     timestamptz;
alter table public.visits add column if not exists consent_method text;   -- 'verbal' | 'written'

-- 2. Considerations engine output (physician-review-only assistance).
alter table public.soap_notes add column if not exists clinical_considerations jsonb not null default '{}'::jsonb;

-- 3. Physician attestation on the note.
alter table public.soap_notes add column if not exists attested     boolean not null default false;
alter table public.soap_notes add column if not exists attested_at  timestamptz;
alter table public.soap_notes add column if not exists attested_by  uuid references public.users(id);
