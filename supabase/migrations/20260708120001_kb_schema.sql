-- =====================================================================
-- Clinical Memory AI — Knowledge Base schema
-- Global, read-only reference data (355 conditions, term indices,
-- vocabulary, drug resolver, hospital formulary). Not clinic-scoped:
-- every clinic reads the same KB. Written only by the offline ingest
-- script (service role, which bypasses RLS).
-- =====================================================================

-- pg_trgm powers the fuzzy free-text -> canonical matcher.
create extension if not exists pg_trgm;

-- ---------------------------------------------------------------------
-- kb_conditions : one row per condition; full doc kept in `record`.
-- ---------------------------------------------------------------------
create table if not exists public.kb_conditions (
  id              text primary key,                 -- re-namespaced, e.g. cma:stable-angina-ba40-1
  name            text not null,
  synonyms        text[] not null default '{}',
  icd             text[] not null default '{}',
  specialty       text,
  category        text,
  acuity          text,
  cant_miss       boolean,
  prevalence_tier text,
  age_min         integer,
  age_max         integer,
  sex             text,
  record          jsonb not null default '{}'::jsonb,  -- features/investigations/treatment/followup/…
  provenance      jsonb not null default '{}'::jsonb,
  version         text
);
create index if not exists kb_conditions_specialty_idx on public.kb_conditions(specialty);
create index if not exists kb_conditions_name_trgm_idx on public.kb_conditions using gin (name gin_trgm_ops);

-- ---------------------------------------------------------------------
-- kb_term_index : the assistant's lookup engine.
-- Flattened from the 4 index files (symptom/sign/redflag/riskfactor).
-- canonical_id -> conditions, with cant_miss flags.
-- ---------------------------------------------------------------------
create table if not exists public.kb_term_index (
  id            bigint generated always as identity primary key,
  term_type     text not null check (term_type in ('symptom','sign','redflag','riskfactor')),
  canonical_id  text not null,
  condition_id  text not null references public.kb_conditions(id) on delete cascade,
  cant_miss     boolean not null default false,
  weight        numeric,
  discriminating boolean,
  action        text
);
create index if not exists kb_term_index_canonical_idx on public.kb_term_index(canonical_id);
create index if not exists kb_term_index_condition_idx on public.kb_term_index(condition_id);
create index if not exists kb_term_index_type_idx      on public.kb_term_index(term_type);

-- ---------------------------------------------------------------------
-- kb_vocabulary : controlled vocabulary + normalization target.
-- ---------------------------------------------------------------------
create table if not exists public.kb_vocabulary (
  canonical_id    text primary key,
  kind            text,
  label           text not null,
  synonyms        text[] not null default '{}',
  snomed          text,
  icd             text,
  condition_count integer
);
create index if not exists kb_vocab_label_trgm_idx on public.kb_vocabulary using gin (label gin_trgm_ops);

-- ---------------------------------------------------------------------
-- kb_drug_generic : brand -> generic (INN) resolution.
-- ---------------------------------------------------------------------
create table if not exists public.kb_drug_generic (
  id          bigint generated always as identity primary key,
  brand_name  text not null,
  generic_inn text,
  resolved    boolean not null default false
);
create index if not exists kb_drug_generic_brand_trgm_idx on public.kb_drug_generic using gin (brand_name gin_trgm_ops);

-- ---------------------------------------------------------------------
-- kb_spelling_bridge : spelling variants -> canonical (e.g. acetaminophen->paracetamol)
-- ---------------------------------------------------------------------
create table if not exists public.kb_spelling_bridge (
  variant   text primary key,
  canonical text not null
);

-- ---------------------------------------------------------------------
-- kb_formulary : hospital brand/generic/price catalogue (autocomplete).
-- ---------------------------------------------------------------------
create table if not exists public.kb_formulary (
  id            bigint generated always as identity primary key,
  brand_name    text not null,
  generic_name  text,
  dose_size     text,
  mrp           numeric,
  unit_per_pack text,
  uom_pack_type text,
  category      text,
  subcategory   text
);
create index if not exists kb_formulary_brand_trgm_idx   on public.kb_formulary using gin (brand_name gin_trgm_ops);
create index if not exists kb_formulary_generic_trgm_idx on public.kb_formulary using gin (generic_name gin_trgm_ops);

-- ---------------------------------------------------------------------
-- RLS: KB is shared reference data — any signed-in user may read it.
-- Writes happen only via the service role (which bypasses RLS).
-- ---------------------------------------------------------------------
alter table public.kb_conditions     enable row level security;
alter table public.kb_term_index     enable row level security;
alter table public.kb_vocabulary     enable row level security;
alter table public.kb_drug_generic   enable row level security;
alter table public.kb_spelling_bridge enable row level security;
alter table public.kb_formulary      enable row level security;

create policy kb_conditions_read     on public.kb_conditions     for select to authenticated using (true);
create policy kb_term_index_read     on public.kb_term_index     for select to authenticated using (true);
create policy kb_vocabulary_read     on public.kb_vocabulary     for select to authenticated using (true);
create policy kb_drug_generic_read   on public.kb_drug_generic   for select to authenticated using (true);
create policy kb_spelling_bridge_read on public.kb_spelling_bridge for select to authenticated using (true);
create policy kb_formulary_read      on public.kb_formulary      for select to authenticated using (true);
