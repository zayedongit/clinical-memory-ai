# Evaluation datasets

**Everything in this directory is SYNTHETIC.** No real patient data, no real
transcripts, no real consultations. The cases were written by hand to look like
the consultations this system is built for — Hindi–English code-mixed speech,
partial vitals, negative findings stated as negatives — and every "model
response" is a hand-authored stand-in, not a captured production output.

| File | What it is | How it is used |
|---|---|---|
| `extraction_gold.json` | Synthetic consultations with gold annotations and a recorded model response per case | `scripts/eval_extraction.py` |
| `red_flag_cases.json` | Classic red-flag presentations and benign controls | `scripts/eval_red_flags.py` |

## Why recorded model responses

The extraction evaluation measures **our post-processing pipeline**: entity
normalisation, citation verification, allergy-negation handling, and the
completeness rubric. Freezing the model response makes that measurement
deterministic, free, and runnable in CI with no API key — and it isolates the
thing being measured, because a change in the score then means our code
changed, not that a provider shipped a new checkpoint overnight.

The recorded responses deliberately include the failure modes seen in practice:
a fabricated citation, a paraphrased-but-real quote, a hallucinated vital, and
a negative allergy history phrased as prose.

`scripts/eval_extraction.py --live` runs the same cases through the real
provider chain instead, which measures the model as well. That mode needs an
API key and is not part of CI.
