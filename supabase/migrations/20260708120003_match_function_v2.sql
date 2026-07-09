-- =====================================================================
-- match_terms v2 — anchor to the best match to stop over-broad results.
-- Problem in v1: "eye pain" fuzzy-matched six different pain-family terms
-- (pain / loin pain / back pain / …), each dragging in unrelated conditions.
-- Fix: keep only terms whose similarity is close to the BEST match
-- (relative cutoff) and above an absolute floor. So a near-exact term
-- dominates and weak partials are dropped.
-- =====================================================================

create or replace function public.match_terms(
  q             text,
  sim_threshold real default 0.45,   -- absolute floor
  max_terms     integer default 5
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
  with scored as (
    select v.canonical_id, v.label, v.kind, similarity(v.label, q) as sim
    from kb_vocabulary v
    where v.label % q
  ),
  best as (select coalesce(max(sim), 0)::real as m from scored),
  matched as (
    select s.canonical_id, s.label, s.kind, s.sim
    from scored s, best b
    where s.sim >= greatest(sim_threshold, b.m * 0.72)   -- anchor near the top match
    order by s.sim desc
    limit max_terms
  )
  select m.canonical_id, m.label, m.kind, m.sim,
         c.id, c.name, c.specialty, t.term_type, t.cant_miss
  from matched m
  join kb_term_index t on t.canonical_id = m.canonical_id
  join kb_conditions c on c.id = t.condition_id
  order by t.cant_miss desc, m.sim desc, c.name;
$$;
