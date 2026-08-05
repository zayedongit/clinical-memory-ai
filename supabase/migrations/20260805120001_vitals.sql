-- =====================================================================
-- Structured vitals on each note — sharpens Clinical Synthesis's synthesis (it accepts
-- a vitals payload) and is the basis for numeric trend memory (BP 140→160
-- across visits). Free-form jsonb: {bp, hr, temp, spo2, rr}.
-- =====================================================================
alter table public.soap_notes add column if not exists vitals jsonb not null default '{}'::jsonb;
