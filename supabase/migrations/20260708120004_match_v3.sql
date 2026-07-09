-- =====================================================================
-- match_terms v3
--   * red-flag preservation: cant-miss links are kept even when their
--     term is below the anchor cutoff (never silently hide a danger).
--   * returns prevalence_tier + age_min/age_max/sex so the app can
--     rank by relevance and apply age/sex awareness.
-- + condition_redflags(cid): red-flag terms for a single condition
--   (used by the condition detail panel).
-- =====================================================================

-- Return type changed (added columns), so the old function must be dropped first.
drop function if exists public.match_terms(text, real, integer);

create function public.match_terms(
  q             text,
  sim_threshold real default 0.45,
  max_terms     integer default 6
)
returns table (
  canonical_id    text,
  label           text,
  kind            text,
  similarity      real,
  condition_id    text,
  condition_name  text,
  specialty       text,
  term_type       text,
  cant_miss       boolean,
  prevalence_tier text,
  age_min         integer,
  age_max         integer,
  sex             text
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
  toprank as (                                   -- anchored near the best match
    select s.canonical_id
    from scored s, best b
    where s.sim >= greatest(sim_threshold, b.m * 0.72)
    order by s.sim desc
    limit max_terms
  ),
  joined as (
    select s.canonical_id, s.label, s.kind, s.sim,
           t.term_type, t.cant_miss as tcm,
           c.id as cid, c.name as cname, c.specialty,
           c.prevalence_tier, c.age_min, c.age_max, c.sex
    from scored s
    join kb_term_index t on t.canonical_id = s.canonical_id
    join kb_conditions c on c.id = t.condition_id
    where s.canonical_id in (select canonical_id from toprank)   -- anchored terms
       or (t.cant_miss and s.sim >= 0.40)                        -- OR any red-flag link
  )
  select canonical_id, label, kind, sim,
         cid, cname, specialty, term_type, tcm,
         prevalence_tier, age_min, age_max, sex
  from joined
  order by tcm desc, sim desc, cname;
$$;

grant execute on function public.match_terms(text, real, integer) to authenticated;

-- Red-flag terms for a single condition (for the detail panel).
create or replace function public.condition_redflags(cid text)
returns table (label text)
language sql
stable
security definer
set search_path = public
as $$
  select distinct coalesce(v.label, t.canonical_id) as label
  from kb_term_index t
  left join kb_vocabulary v on v.canonical_id = t.canonical_id
  where t.condition_id = cid and t.term_type = 'redflag';
$$;

grant execute on function public.condition_redflags(text) to authenticated;
