-- =====================================================================
-- Formulary: category index so the prescription search can cheaply filter
-- to Pharma (drugs) while consumables/implants live in the same table for
-- a future billing module. Trigram indexes on brand/generic already exist
-- from the KB schema migration.
-- =====================================================================
create index if not exists kb_formulary_category_idx on public.kb_formulary(category);
create index if not exists kb_formulary_subcategory_idx on public.kb_formulary(subcategory);
