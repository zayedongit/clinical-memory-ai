-- =====================================================================
-- Structured consultation-wizard state, so an in-progress draft can be
-- resumed with every field (encounter, vitals, chosen diagnosis,
-- investigations, prescription) restored exactly as left.
-- =====================================================================
alter table public.soap_notes add column if not exists wizard jsonb not null default '{}'::jsonb;
