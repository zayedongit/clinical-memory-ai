-- =====================================================================
-- Prescription support:
--   kb_drugs  — clean prescribing catalogue (brand/generic/strength/form/price)
--   soap_notes.prescription — the drugs prescribed at a visit (jsonb list)
-- =====================================================================

create table if not exists public.kb_drugs (
  id            bigint generated always as identity primary key,
  brand_name    text not null,
  generic_name  text,
  strength      text,
  dosage_form   text,
  pack_size     text,
  mrp           numeric,
  manufacturer  text
);
create index if not exists kb_drugs_brand_trgm   on public.kb_drugs using gin (brand_name gin_trgm_ops);
create index if not exists kb_drugs_generic_trgm on public.kb_drugs using gin (generic_name gin_trgm_ops);

alter table public.kb_drugs enable row level security;
drop policy if exists kb_drugs_read on public.kb_drugs;
create policy kb_drugs_read on public.kb_drugs for select to authenticated using (true);

-- Prescribed items stored with the visit note.
-- [{brand, generic, strength, form, dose, frequency, duration, instructions}]
alter table public.soap_notes add column if not exists prescription jsonb not null default '[]'::jsonb;
