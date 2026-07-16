-- =====================================================================
-- kb_ground_red_flags(findings)
--   Grounds the Considerations Engine in the curated ICMR-derived KB.
--   Given the presenting symptoms, find the CAN'T-MISS conditions those
--   symptoms point at, and return each condition's documented red-flag
--   features + suggested action. This makes the safety-critical red flags
--   come from curated data, not only the LLM.
--
--   Matching mirrors match_terms: trigram match each finding to the
--   controlled vocabulary, follow can't-miss term links to conditions,
--   rank conditions by how many distinct findings they explain.
-- =====================================================================

create or replace function public.kb_ground_red_flags(
  findings      text[],
  sim_threshold real default 0.45,
  max_conditions integer default 6
)
returns table (
  condition_id   text,
  condition_name text,
  acuity         text,
  prevalence_tier text,
  matched_count  integer,
  redflag_label  text,
  action         text
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
  -- each finding -> can't-miss conditions it points at
  hits as (
    select distinct t.condition_id, f.q
    from f
    join kb_vocabulary v
      on v.label % f.q and similarity(v.label, f.q) >= sim_threshold
    join kb_term_index t
      on t.canonical_id = v.canonical_id and t.cant_miss = true
  ),
  conds as (
    select condition_id, count(distinct q) as matched_count
    from hits
    group by condition_id
  ),
  ranked as (
    select condition_id, matched_count
    from conds
    order by matched_count desc
    limit max_conditions
  )
  select c.id, c.name, c.acuity, c.prevalence_tier, r.matched_count,
         coalesce(v.label, t.canonical_id) as redflag_label,
         t.action
  from ranked r
  join kb_conditions c  on c.id = r.condition_id
  join kb_term_index t  on t.condition_id = c.id and t.term_type = 'redflag'
  left join kb_vocabulary v on v.canonical_id = t.canonical_id
  order by r.matched_count desc, c.name, redflag_label;
$$;

grant execute on function public.kb_ground_red_flags(text[], real, integer) to authenticated;
