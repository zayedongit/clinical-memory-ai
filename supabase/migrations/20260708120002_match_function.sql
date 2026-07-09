-- =====================================================================
-- Free-text -> canonical matcher (Live Consultation Assistant core).
-- Given a messy phrase ("chest pain going to arm"), fuzzy-match it to the
-- KB's canonical vocabulary via pg_trgm, then return the candidate
-- conditions those terms point to, flagging cant-miss (red-flag) links.
-- Exposed to the app as a PostgREST RPC: POST /rest/v1/rpc/match_terms
-- =====================================================================

create or replace function public.match_terms(
  q             text,
  sim_threshold real default 0.30,
  max_terms     integer default 6
)
returns table (
  canonical_id   text,
  label          text,
  kind           text,
  similarity     real,
  condition_id   text,
  condition_name text,
  specialty      text,
  term_type      text,
  cant_miss      boolean
)
language sql
stable
security definer
set search_path = public
as $$
  with matched as (
    select v.canonical_id, v.label, v.kind, similarity(v.label, q) as sim
    from kb_vocabulary v
    where v.label % q                     -- trigram match (uses the GIN index)
    order by sim desc
    limit max_terms
  )
  select m.canonical_id, m.label, m.kind, m.sim,
         c.id, c.name, c.specialty, t.term_type, t.cant_miss
  from matched m
  join kb_term_index t on t.canonical_id = m.canonical_id
  join kb_conditions c on c.id = t.condition_id
  where m.sim >= sim_threshold
  order by m.sim desc, t.cant_miss desc, c.name;
$$;

grant execute on function public.match_terms(text, real, integer) to authenticated;
