# Clinical Memory AI — Case Study

> Save this to the repo as `docs/CASE_STUDY.md` and link it from the README.

---

## What it is

A clinical documentation and decision-support platform for physicians and small clinics. It records a
consultation, turns it into a structured, medico-legally-shaped note, carries patient history across
visits, and offers guideline-grounded suggestions the physician reviews and signs.

Built solo. Reviewed weekly with a qualified physician who checks the clinical output against how a
consultation actually runs.

It is a working local prototype, not a deployed product, and not a certified medical device. Every AI
output is a draft a physician reviews, edits, and explicitly attests before it enters the record.

---

## The problem

A doctor in a short consultation has to listen, examine, recall the patient's history, decide on
investigations, and write a prescription — while also writing notes. Notes get rushed, history gets
lost between visits, and there is usually no record of *where* any given piece of information came
from.

That last part is the constraint that shaped most of this system. In a clinical record, "the AI said
so" is not an acceptable provenance. Every fact needs an origin, and every AI-generated line needs a
human who accepted it.

---

## Working against systems I did not build

Almost nothing in the critical path is mine. That was the interesting part.

| System | What I had to work around |
|---|---|
| OpenAI `gpt-4o-transcribe` (+ Sarvam fallback) | Chunked speech-to-text on Hindi–English code-mixed audio; the latency floor of the whole live experience |
| Google Gemini → OpenAI | Structured extraction; strict-JSON output that isn't always strict, and a provider that started returning 401 |
| Supabase Auth + PostgREST | Authorization enforced by passing the caller's JWT through to the database rather than trusting my own code |
| External clinical synthesis API | Guideline-grounded differentials and treatment — a dependency I do not control and did not write |
| 100k-item hospital formulary | Real drug data with brands, strengths, MRP, therapeutic class, ingested and made searchable |

None of these could be changed to suit me. The design work was deciding what happens when each one
behaves badly.

---

## The hard parts, named

**1. Tenant isolation that survives my own bugs.**
Multiple clinics share one database. The obvious approach is filtering by `clinic_id` in application
code, and the obvious failure is forgetting it on one query.

So isolation lives in the database instead. Every clinic-scoped table has Row-Level Security policies
restricting rows to `clinic_id = current_clinic_id()`, where `current_clinic_id()` resolves the
caller's clinic from their token. The backend forwards the user's JWT to PostgREST, so the database
evaluates the rule using the caller's own identity. If my application code asks for the wrong clinic's
rows, it gets nothing back.

There is an isolation test in SQL (`supabase/tests/rls_isolation_test.sql`) rather than a claim in a
README. Two paths deliberately bypass RLS with the service key — looking up a user during auth, and
writing the audit log — and both are server-side only.

**2. Provenance that is append-only.**
Approved clinical information is written to `clinical_facts` as individual rows, each tagged with a
type (diagnosis / medication / allergy / vital), a source (AI-extracted vs doctor-confirmed), and a
status (proposed / confirmed / superseded). Nothing is edited in place — re-finalising a visit
supersedes the old facts rather than overwriting them. That table is what powers the longitudinal
memory, instead of re-parsing free text on every read.

The audit log goes further: a database trigger blocks `UPDATE` and `DELETE` outright, and each row
carries a SHA-256 hash chained to the previous row.

I would not call that tamper-proof. It is append-only and **tamper-evident** — the trigger blocks
edits and the chain makes edits detectable, but a database owner with full admin rights could disable
the trigger. It is a strong control, not a guarantee, and I would rather say so.

**3. Measuring clinical correctness instead of eyeballing it.**
"The notes look good" is not a quality bar when the failure mode is a missed red flag. So there is an
evaluation harness (`backend/eval/`, `backend/scripts/eval_red_flags.py`) that scores the decision support
against classic can't-miss presentations and reports recall alongside a no-false-alarm precision
metric. Both evaluations now run in CI as hard gates. They did not at the time this was written, and the
harness had drifted badly enough to be worse than nothing — see the addendum.

Recall and precision pull against each other here, and the direction matters: a missed red flag is
worse than a spurious one, but a system that flags everything gets ignored, which is the same as
missing everything.

---

## The AI decisions

**Which model, and what happens when it fails.** Extraction runs Gemini first and falls through to
OpenAI at runtime; speech-to-text runs OpenAI first and falls through to Sarvam. Both are ordered
chains, both fail over on transient errors, and a non-transient failure like a bad key skips the rest
of that vendor's models rather than retrying them.

That is the state after the audit. Before it, neither was true: the LLM lane rotated between Gemini
models only, and speech-to-text picked a provider from *which key was configured* and returned 502 on
failure — so a doctor's speech was lost while a healthy second provider sat configured and unused.

**What happens when the output is malformed.** A salvage step (`_salvage_json`) recovers truncated or
fenced JSON. If it still cannot parse, the feature returns empty rather than crashing the request.

**What happens when the model is confidently wrong.** This is the one I care most about. The model is
required to quote the exact words from the transcript that support each extracted field, and the code
then verifies each quote actually appears in the transcript, dropping any it invented. A fabricated
citation cannot survive the check.

**What happens at the boundary.** Suggestions fail *open* — if the synthesis service is down, the
consultation continues with an explicit "unavailable" banner, because a doctor should not be blocked
by my sidebar. Attestation fails *closed* — the backend refuses to save a completed note without the
physician's sign-off. The rule is that a missing helper degrades, but a missing human blocks.

One caveat I keep in front of me: absence of suggestions is not an all-clear.

**Latency shaping.** The live scribe runs two lanes. A light, frequent lane sends the recent transcript
and refreshes side-panel suggestions (targeting roughly 3.5–4s, bounded by chunked STT rather than the
model). A heavier, less frequent lane sends the full transcript to fill the structured note. The
note-fill is non-destructive: it fills empty fields and adds new complaints, and never overwrites what
the doctor typed. I have not benchmarked exact latency and will not quote numbers I did not measure.

---

## What broke

**The Gemini key was returning 401, and the symptom looked like a model problem.**

Structured extraction started coming back empty. That reads like a prompt or model failure, and the
tempting move was to start rewriting prompts.

Instead I called each provider directly and printed the raw response. Gemini returned **HTTP 401**
on the endpoints the code uses; OpenAI returned a normal result. The problem was authentication, one
layer below where the symptom appeared.

That diagnosis is where this story used to end, and the ending was wrong: I described the fix as "an
OpenAI fallback", but no such fallback existed in the code. The LLM lane rotated between *Gemini*
models and stopped there. A later audit caught it — see the addendum. The failover is real now, and
the shape of that mistake is worth keeping: I had described the fix I intended rather than the one I
shipped.

**Our own rate limiter blocked our own live scribe.**

The live flow calls `/scribe/transcribe`, `/scribe/live` (the suggestions lane) and `/scribe/extract`
many times a minute by design. The per-IP limiter on the AI endpoints treated that as abuse.

Again the symptom lied: nothing errored, the note and suggestions just came back empty. Tracing a
single consultation request by request showed the limiter rejecting our own calls. The real lesson
was that I had set the limit against imagined client behaviour rather than the behaviour the app
actually has.

The fix I originally described here — exempting the three live endpoints — is not what the code did:
they stayed in the *stricter* AI bucket. The audit corrected both the story and the limiter.

It is also an in-memory limiter, so it counts per process — across N instances the effective limit
becomes N times what is configured. That is unfixed and listed below.

---

## What I'd fix given another week

> **Note:** this list was written before the audit. Items 1–5 and 8–12 are now done; see the
> addendum and the README for what shipped. Items 6 and 7 — deployment, and coding the diagnoses —
> remain open, and they are still the two that matter most.


1. **Make the visit save transactional.** The visit, note, `clinical_facts` and audit entry are written
   as separate calls. A mid-way failure leaves a partial save. Move it into one Postgres function.
2. **Add optimistic locking on finalize.** Two clinicians finalizing the same visit is last-write-wins
   today. Records need a version, and a stale save should be rejected with a reload prompt.
3. **Make backend tests blocking in CI.** The backend test step runs `continue-on-error: true`. That
   was reasonable while the suite was thin and is the wrong thing to leave in place. First tests I'd
   write: unit tests for the JSON-salvage and quote-verification helpers, an integration test for the
   save flow, the RLS test in CI, and one end-to-end browser test.
4. **Move the rate limiter to Redis**, for the reason above.
5. **Add metrics and tracing.** There is structured JSON logging with per-request IDs that avoids
   logging bodies and redacts sensitive fields to keep PHI out of logs, and Sentry is wired but off.
   There are no metrics and no tracing — the gap I would feel first in production.
6. **Deploy it.** A Cloudflare path is configured for the frontend but unverified, and the backend is
   not containerized or hosted.
7. **Code the diagnoses properly.** Differential suggestions carry ICD-10 codes, but saved diagnoses
   and prescriptions are largely free text. That limits everything analytical downstream.

And five more I'd want fixed before this went anywhere near real patients:

8. **The `q` search filter is interpolated into a PostgREST filter string.** It needs validating and
   escaping properly. This is the one I'd do first if the timeline were shorter than a week.
9. **No spending cap on the paid AI and speech calls.** A runaway loop is currently a billing event.
10. **CORS is permissive in development** and must be locked to the real origin before deployment.
11. **No fine-grained roles.** There's a `doctor` / `staff` field on users, but permissions beyond
    clinic scoping aren't implemented.
12. **Route protection is client-side only** — a session check and redirect in the browser. I would
    not describe it as server-side protection, because it isn't.

---

## What I did not build

The external clinical synthesis API is not mine. It is also a single point of dependency, alongside
Supabase, and I would treat both as risks in any real deployment.


---

## Addendum: the audit

This case study was written against an earlier state of the repository. A later full audit —
rebuilding the database from the committed migrations, walking the OpenAPI schema, and testing the
claims one at a time — found that several statements above described intended behaviour rather than
shipped behaviour. Correcting them mattered more than the code fixes, because a false claim in a
README is a claim someone might rely on.

**Claims that were not backed by the code**

| Claim | Reality |
|---|---|
| "Runtime OpenAI fallback" for structuring | The lane rotated between Gemini models only. No cross-vendor failover existed |
| `backend/scripts/diag_ai.py` "stayed in the repo" | The file did not exist |
| "I exempted the three live endpoints" from rate limiting | They were in the *stricter* AI bucket |
| Allergy and "duplicate-therapy" checks | Only a loose two-way substring allergy check, which fired on unrelated drugs |
| "Measured clinical quality" from the red-flag harness | The harness scored a knowledge-base function that had already been removed from the request path, and needed a live database, so it could not run in CI |

**Defects the audit found**

* `visits.status` allowed only `('draft','approved')` while the application wrote `'in_progress'` —
  every draft save would have failed on a database built from the committed migrations. The schema
  only worked because the live project had drifted.
* `soap_notes` had RLS enabled with no `UPDATE` policy. PostgREST returns 204 for an update matching
  zero rows, so finalising a draft reported success and silently wrote nothing.
* The audit hash chain selected its predecessor with `order by at desc, id desc`. `at` is the
  transaction timestamp and is identical for every row in one transaction, so the tie broke on a
  random UUID and rows chained out of insertion order — reachable in normal use, because
  `finalize_visit()` writes its audit row inside the clinical transaction.
* Patient search interpolated raw input into a PostgREST filter string.
* `await file.read()` on the audio endpoint had no size ceiling.
* The rate limiter's bucket dictionary grew without bound.
* A JSON `null` in a model's symptom array reached the physician as the literal word "None".
* With decision support unavailable, the consultation dead-ended: the only route to Review & Sign
  went through selecting a primary diagnosis, and there was nothing to select.

**The lesson worth keeping.** Every one of these was invisible from the outside. The application ran.
The tests passed — there was one test. What made them visible was building the system from its own
committed artefacts: applying the migrations to an empty database, walking the route table, running
the evaluation against the code that actually ships. A claim is only as good as the check that
regenerates it, which is why every number in the README now comes from a file in `eval/results/` that
CI regenerates on every push.
