-- =====================================================================
-- Completeness gate at sign-off.
-- Stores the safety checks shown to the physician at signing and any
-- explicit overrides (with reason) so an ignored red flag / missing
-- must-not-miss test / allergy conflict is auditable, not silent.
-- Additive + idempotent.
-- =====================================================================
alter table public.soap_notes add column if not exists sign_off jsonb not null default '{}'::jsonb;

-- Shape (documentation only):
-- {
--   "warnings":  [{"kind":"red_flag|missing_investigation|allergy_conflict","label":"..."}],
--   "overrides": [{"kind":"...","label":"...","reason":"physician's justification"}],
--   "passed":    true            -- true when no unresolved warnings remained
-- }
