-- =====================================================================
-- Patient registration details (Clinical Synthesis-style): address + locality so a
-- patient record is a proper demographic entry, not just name + phone.
-- =====================================================================
alter table public.patients add column if not exists address text;
alter table public.patients add column if not exists pincode text;
alter table public.patients add column if not exists city    text;
alter table public.patients add column if not exists state   text;
