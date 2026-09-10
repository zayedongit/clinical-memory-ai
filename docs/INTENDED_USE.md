# Intended Use & Limitations

_This statement defines what Clinical Memory AI is designed to do, who may use it, and the boundaries of its function. It is the reference for regulatory posture, clinical safety, and user-facing disclaimers. It is not legal advice._

## Intended use

Clinical Memory AI is a **documentation and decision-support tool for licensed physicians**. It:

- transcribes a consultation the physician records (with patient consent);
- structures that transcript into a draft clinical note (SOAP), extracted entities, and vitals;
- maintains a longitudinal, doctor-confirmed record of the patient across visits;
- surfaces **physician-review-only** clinical considerations — differential diagnoses, suggested investigations, treatment options, red-flag prompts, and completeness checks.

Every AI output is a **draft**. It has no clinical effect until a licensed physician reviews, edits as needed, and explicitly attests to it.

## Intended users

Licensed general physicians and their authorised clinic staff, operating within their scope of practice. It is **not** intended for use by patients, unlicensed persons, or as a substitute for professional medical judgement.

## What it is NOT

- **Not an autonomous diagnostic system.** It does not diagnose. It suggests considerations a physician evaluates.
- **Not an autonomous prescribing system.** It does not prescribe. The physician selects, adjusts, and signs every prescription.
- **Not a medical device making automated clinical decisions.** No output acts on a patient without a physician in the loop.
- **Not a monitoring or emergency system.** It does not provide real-time alerts for deteriorating patients and must not be relied on for time-critical care.

## The physician remains responsible

The attending physician is the responsible clinician for every decision. Attestation records that the physician has taken ownership of the note and its clinical content. The audit log records who attested, and when.

## Known limitations (must be understood by users)

- **Transcription can err**, especially on drug names, doses, accents, and in noisy environments. Physicians must verify all safety-critical content against what was actually said.
- **AI structuring can omit or misattribute information.** Extracted entities are a starting point, not a source of truth.
- **Decision support may be incomplete or unavailable.** When the decision-support service cannot be reached, the system fails open (returns nothing); absence of a red flag does not mean absence of risk.
- **Drug-safety checking is partial.** The system performs two narrow checks: a documented-allergy conflict (including drug-family matching, so a penicillin allergy catches amoxicillin) and a duplicate-ingredient check against the patient's current prescriptions. It does **not** perform drug-drug interaction checking, renal or hepatic dose adjustment, dose-ceiling checking, or paediatric dosing. A prescriber who believes otherwise will stop performing those checks themselves, which is why the boundary is stated in the interface and not only here.

- **The escalation-risk prompt is not clinically validated.** It is trained on synthetic data (see `docs/MODEL_CARD.md`), it answers only "does this case warrant a second look", and it is presented as a documentation prompt. It is not a triage category and must not be used to decide that a patient does *not* need attention: a low score is the absence of a prompt, not the presence of reassurance. Patients under 13 are not scored at all, because every vital-sign threshold in it is an adult range.

- **Longitudinal trend detection is a prompt, not a finding.** Trends are reported with the statistical test and p-value that produced them, over short and irregularly sampled series. A flagged trend means "look at this", not "this is clinically significant".

## Data protection posture (India)

This product processes sensitive personal health data and is designed to align with the Digital Personal Data Protection Act, 2023:

- **Lawful basis & consent** — recording consent is captured per consultation; broader processing consent and patient-rights flows (access, correction, erasure request, grievance) are part of the compliance roadmap.
- **Purpose limitation** — data is used to document care and provide decision support to the treating clinic only; it is not sold or used for advertising.
- **Data residency** — production deployment targets India-resident infrastructure for patient data; any interoperability with ABDM/ABHA will follow ABDM data-handling requirements.
- **Retention & integrity** — clinical records are retained (soft-deleted with a mandatory reason, never destroyed; `DELETE` is revoked at the database level) consistent with medical record-keeping norms. The audit log is append-only, enforced by a database trigger that applies to every role, and hash-chained so that a rewrite is detectable via `verify_audit_chain()`. It is tamper-**evident**, not tamper-proof: a database owner retains the ability to rewrite history, and the chain's guarantee is that doing so becomes visible.
- **Breach handling** — a data-breach detection and notification process is part of the operational roadmap.

## Prescription validity

A prescription generated by the system is valid only when it carries the prescribing physician's identity — name, registration number (SMC/NMC), and qualifications — the clinic's details, and the physician's authorisation. These fields are captured on the physician and clinic profiles and printed on every prescription.

## Attestation and correction

A note has no clinical effect until a user whose role is `doctor` attests to it. That requirement is enforced in the database, inside the same transaction that writes the record, so it cannot be bypassed by the application or by anything else holding a database session.

Once attested, the clinical content of a note is **immutable**: a database trigger blocks every clinical column. Corrections are recorded as **amendments**, appended with their author, timestamp and a mandatory reason, leaving the originally signed text exactly as it was signed. This mirrors how a paper record is amended, and preserves the record of what was decided at the time.

Safety warnings presented at sign-off that the physician chooses to override are recorded with the reason given, so an ignored warning is auditable rather than silent.

## Change control

Material changes to the decision-support logic, the models used, or the clinical rules are recorded so that any note can be interpreted against the version of the system that produced it.

The escalation-risk model carries a version and the held-out metrics it shipped with, returned alongside every score. Its training is deterministic and reproduced in CI, so the artefact serving any given note can be rebuilt exactly.

## Verification status

No claim in this document has been independently audited or certified. The evaluations referenced are computed on synthetic data and are published in `backend/eval/results/`, regenerated on every push. No clinical validation, penetration test, or compliance audit has been performed.
