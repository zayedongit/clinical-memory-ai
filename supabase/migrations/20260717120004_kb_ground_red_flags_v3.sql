-- =====================================================================
-- kb_ground_red_flags v3 — rank by term specificity, not raw match count.
--
--   v2 let generic conditions that match common words (Acute Diarrhea,
--   Ante-Natal Management ... all match "abdominal pain / vomiting / fever")
--   crowd out specific dangerous conditions (Appendicitis, DKA).
--
--   v3 scores each finding->condition match by how SPECIFIC the matched term
--   is, using signals already in kb_term_index:
--       cant_miss term      -> 4
--       redflag term        -> 3
--       sign / discriminating-> 2
--       plain symptom        -> 1
--   A condition's score = sum over distinct findings of its best term score.
--   Rank by score, so "peritonitis -> appendicitis" beats "vomiting -> diarrhea".
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
  score           numeric,
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
  matched as (
    select t.condition_id, f.q,
           coalesce(t.cant_miss, false) as cm,
           (case
              when coalesce(t.cant_miss, false)     then 4
              when t.term_type = 'redflag'          then 3
              when t.term_type = 'sign'             then 2
              when coalesce(t.discriminating, false) then 2
              else 1
            end)::numeric as term_score
    from f
    join kb_vocabulary v
      on v.label % f.q and similarity(v.label, f.q) >= sim_threshold
    join kb_term_index t
      on t.canonical_id = v.canonical_id
  ),
  best_per_finding as (             -- strongest term per (condition, finding)
    select condition_id, q,
           max(term_score) as fscore,
           bool_or(cm)     as cm
    from matched
    group by condition_id, q
  ),
  conds as (
    select condition_id,
           count(distinct q) as matched_count,
           sum(fscore)       as score,
           bool_or(cm)       as any_cantmiss
    from best_per_finding
    group by condition_id
  ),
  rf_conds as (                     -- must have red-flag guidance to show
    select c.*
    from conds c
    where c.any_cantmiss
       or exists (select 1 from kb_term_index t2
                  where t2.condition_id = c.condition_id and t2.term_type = 'redflag')
  ),
  ranked as (
    select condition_id, matched_count, any_cantmiss, score
    from rf_conds
    order by score desc, any_cantmiss desc, matched_count desc
    limit max_conditions
  )
  select c.id, c.name, c.acuity, c.prevalence_tier, r.any_cantmiss, r.matched_count, r.score,
         coalesce(v.label, t.canonical_id) as redflag_label,
         t.action
  from ranked r
  join kb_conditions c  on c.id = r.condition_id
  join kb_term_index t  on t.condition_id = c.id and t.term_type = 'redflag'
  left join kb_vocabulary v on v.canonical_id = t.canonical_id
  order by r.score desc, r.any_cantmiss desc, c.name, redflag_label;
$$;

grant execute on function public.kb_ground_red_flags(text[], real, integer) to authenticated;
