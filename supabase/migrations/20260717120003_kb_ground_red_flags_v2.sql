-- =====================================================================
-- kb_ground_red_flags v2
--   The v1 filter (term link must be cant_miss=true) missed conditions
--   whose can't-miss flag was never set during ingest (Stroke, DKA, Acute
--   GI Bleed, Asthma, ...) even though they carry rich red-flag terms.
--
--   v2 grounds on a signal we always have: a condition is worth a red-flag
--   screen if it actually CARRIES redflag-type terms. cant_miss is now used
--   only to rank + to drive alert urgency (loud vs quiet), not to gate.
--
--   Returns any_cantmiss so the app can grade urgency:
--     emergent acuity -> EMERGENCY, cant_miss -> URGENT, else -> routine NOTE.
-- =====================================================================

drop function if exists public.kb_ground_red_flags(text[], real, integer);

create function public.kb_ground_red_flags(
  findings       text[],
  sim_threshold  real default 0.45,
  max_conditions integer default 6
)
returns table (
  condition_id    text,
  condition_name  text,
  acuity          text,
  prevalence_tier text,
  any_cantmiss    boolean,
  matched_count   integer,
  redflag_label   text,
  action          text
)
language sql
stable
security definer
set search_path = public
as $$
  with f as (
    select distinct lower(trim(x)) as q
    from unnest(findings) x
    where length(trim(x)) >= 2
  ),
  -- each finding -> any condition it points at (via any term type)
  matched as (
    select distinct t.condition_id, f.q, coalesce(t.cant_miss, false) as cm
    from f
    join kb_vocabulary v
      on v.label % f.q and similarity(v.label, f.q) >= sim_threshold
    join kb_term_index t
      on t.canonical_id = v.canonical_id
  ),
  conds as (
    select condition_id,
           count(distinct q)          as matched_count,
           bool_or(cm)                as any_cantmiss
    from matched
    group by condition_id
  ),
  -- keep only conditions that actually carry red-flag guidance to show
  rf_conds as (
    select c.condition_id, c.matched_count, c.any_cantmiss
    from conds c
    where exists (
      select 1 from kb_term_index t2
      where t2.condition_id = c.condition_id and t2.term_type = 'redflag'
    )
  ),
  ranked as (
    select condition_id, matched_count, any_cantmiss
    from rf_conds
    order by any_cantmiss desc, matched_count desc
    limit max_conditions
  )
  select c.id, c.name, c.acuity, c.prevalence_tier, r.any_cantmiss, r.matched_count,
         coalesce(v.label, t.canonical_id) as redflag_label,
         t.action
  from ranked r
  join kb_conditions c  on c.id = r.condition_id
  join kb_term_index t  on t.condition_id = c.id and t.term_type = 'redflag'
  left join kb_vocabulary v on v.canonical_id = t.canonical_id
  order by r.any_cantmiss desc, r.matched_count desc, c.name, redflag_label;
$$;

grant execute on function public.kb_ground_red_flags(text[], real, integer) to authenticated;
