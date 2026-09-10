"""Longitudinal analytics over the confirmed clinical-fact store.

"Longitudinal memory" in the product used to mean *listing* what was recorded
before. This turns that list into measurements: is a vital actually trending,
did it step-change between two visits, which complaints recur, which
medications started and stopped, and which problems have been carried for a
long time without ever being resolved.

**Why these statistics.** Clinic visit series are short (3–12 points), sampled
irregularly, and contain measurement noise and the occasional transcription
error. That rules out anything that assumes normality, even spacing, or n
large enough for a t-test. The methods here are the standard non-parametric
answers to exactly that situation:

* **Mann–Kendall** for whether a monotonic trend exists. It uses only the sign
  of every pairwise comparison, so a single mistyped reading cannot manufacture
  a trend, and it needs no distributional assumption.
* **Theil–Sen** for the slope. It is the median of all pairwise slopes, with a
  ~29% breakdown point, versus least squares which one outlier can rotate
  arbitrarily.
* **Median absolute deviation** for step detection, for the same robustness
  reason.

None of this is a diagnosis. A flagged trend is a prompt to look, and the
output says so.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median

# Below this many points nothing is claimed. Three is the minimum at which
# Mann-Kendall can distinguish a trend from noise at all, and even then only
# a strong one; the p-value reflects that honestly.
MIN_POINTS_FOR_TREND = 3

# Two-sided significance level for declaring a trend.
ALPHA = 0.10

# Vitals where "higher" is the concerning direction, for phrasing only.
_HIGHER_IS_WORSE = {"bp", "bp_sys", "bp_dia", "hr", "temp", "rr", "weight", "glucose"}
_LOWER_IS_WORSE = {"spo2"}

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


# --------------------------------------------------------------------- #
# Parsing readings
# --------------------------------------------------------------------- #
def parse_reading(metric: str, reading: str) -> float | None:
    """Pull a number out of a free-text vital.

    Blood pressure is stored as "138/86". The systolic component is the one
    that carries the clinical signal for trend purposes, so that is what a
    "bp" series tracks; the diastolic is available as its own series.
    """
    text = str(reading or "").strip()
    if not text:
        return None
    m = str(metric or "").lower()
    if "/" in text and m.startswith("bp"):
        head, _, tail = text.partition("/")
        target = tail if m.endswith("dia") else head
        found = _NUM.search(target)
        return float(found.group()) if found else None
    found = _NUM.search(text)
    return float(found.group()) if found else None


def _to_date(value: str | date | datetime | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


# --------------------------------------------------------------------- #
# Mann-Kendall + Theil-Sen
# --------------------------------------------------------------------- #
def mann_kendall(values: list[float]) -> tuple[float, float]:
    """Return (S statistic, two-sided p-value) for a monotonic trend.

    S counts concordant minus discordant pairs. Under the null of no trend S
    is symmetric about zero with variance n(n-1)(2n+5)/18, adjusted for ties.
    The normal approximation with a continuity correction is standard and is
    accurate enough from about n=4; below that the p-value is conservative,
    which is the right direction to err.
    """
    n = len(values)
    if n < 3:
        return 0.0, 1.0

    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            s += _sign(values[j] - values[i])

    # Tie correction: groups of equal values contribute no information.
    counts: dict[float, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    tie_term = sum(t * (t - 1) * (2 * t + 5) for t in counts.values() if t > 1)
    variance = (n * (n - 1) * (2 * n + 5) - tie_term) / 18.0
    if variance <= 0:
        return float(s), 1.0

    if s > 0:
        z = (s - 1) / math.sqrt(variance)
    elif s < 0:
        z = (s + 1) / math.sqrt(variance)
    else:
        z = 0.0
    p = 2 * (1 - _phi(abs(z)))
    return float(s), round(min(max(p, 0.0), 1.0), 4)


def theil_sen_slope(xs: list[float], ys: list[float]) -> float:
    """Median of all pairwise slopes. Units: y per unit x."""
    slopes = [
        (ys[j] - ys[i]) / (xs[j] - xs[i])
        for i in range(len(xs) - 1)
        for j in range(i + 1, len(xs))
        if xs[j] != xs[i]
    ]
    return median(slopes) if slopes else 0.0


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def _phi(z: float) -> float:
    """Standard normal CDF via erf — no scipy dependency for one function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# --------------------------------------------------------------------- #
# Series analysis
# --------------------------------------------------------------------- #
@dataclass
class SeriesAnalysis:
    metric: str
    n: int
    latest: float | None = None
    baseline: float | None = None
    direction: str = "insufficient_data"   # rising | falling | stable | insufficient_data
    significant: bool = False
    p_value: float = 1.0
    slope_per_30d: float | None = None
    change_from_baseline_pct: float | None = None
    step_change: dict | None = None
    note: str = ""
    points: list[dict] = field(default_factory=list)


def analyse_series(metric: str, points: list[dict]) -> SeriesAnalysis:
    """`points` is [{"date": "YYYY-MM-DD", "value": "138/86"}, ...] ascending."""
    parsed: list[tuple[date, float]] = []
    for p in points:
        d = _to_date(p.get("date"))
        v = parse_reading(metric, p.get("value"))
        if d is not None and v is not None:
            parsed.append((d, v))
    parsed.sort(key=lambda t: t[0])

    out = SeriesAnalysis(
        metric=metric, n=len(parsed),
        points=[{"date": d.isoformat(), "value": v} for d, v in parsed],
    )
    if not parsed:
        out.note = "No numeric readings recorded."
        return out

    values = [v for _, v in parsed]
    out.latest, out.baseline = values[-1], values[0]
    if out.baseline:
        out.change_from_baseline_pct = round(100 * (out.latest - out.baseline) / abs(out.baseline), 1)

    out.step_change = _detect_step(values)

    if len(parsed) < MIN_POINTS_FOR_TREND:
        out.note = f"Only {len(parsed)} reading(s) — not enough to assess a trend."
        return out

    s, p = mann_kendall(values)
    out.p_value = p
    out.significant = p < ALPHA and s != 0

    day0 = parsed[0][0]
    xs = [float((d - day0).days) for d, _ in parsed]
    if len(set(xs)) > 1:
        out.slope_per_30d = round(theil_sen_slope(xs, values) * 30, 3)

    if not out.significant:
        out.direction = "stable"
        out.note = f"No significant monotonic trend (Mann-Kendall p={p:.2f}, n={len(values)})."
    else:
        out.direction = "rising" if s > 0 else "falling"
        concerning = (
            (out.direction == "rising" and metric.lower() in _HIGHER_IS_WORSE)
            or (out.direction == "falling" and metric.lower() in _LOWER_IS_WORSE)
        )
        qualifier = " — worth reviewing" if concerning else ""
        per30 = f", {out.slope_per_30d:+g}/30d" if out.slope_per_30d is not None else ""
        out.note = f"{out.direction.capitalize()} trend (p={p:.2f}, n={len(values)}{per30}){qualifier}."
    return out


def _detect_step(values: list[float]) -> dict | None:
    """Flag the latest reading if it sits far outside the prior distribution.

    Uses the median absolute deviation rather than the standard deviation: with
    four or five points, one bad reading inflates the SD enough to hide the
    very jump we are looking for. The 1.4826 factor makes MAD a consistent
    estimator of sigma for normal data, so the threshold reads in familiar
    units.
    """
    if len(values) < 4:
        return None
    prior, latest = values[:-1], values[-1]
    med = median(prior)
    mad = median([abs(v - med) for v in prior]) * 1.4826
    if mad <= 0:
        # A perfectly flat history: any change at all is a step.
        return None if latest == med else {
            "detected": True, "baseline": med, "latest": latest,
            "z": None, "note": "Flat history, then a change at the latest visit.",
        }
    z = (latest - med) / mad
    if abs(z) < 3.0:
        return None
    return {
        "detected": True, "baseline": round(med, 2), "latest": round(latest, 2),
        "z": round(z, 2),
        "note": f"Latest reading is {abs(z):.1f} robust SDs {'above' if z > 0 else 'below'} the prior median.",
    }


# --------------------------------------------------------------------- #
# Fact-level analytics
# --------------------------------------------------------------------- #
def recurrence(facts: list[dict], fact_type: str = "symptom") -> list[dict]:
    """Complaints that keep coming back, with how often and how recently.

    A complaint recorded at three separate visits over six months is a
    different clinical object from the same word said three times in one
    consultation, so occurrences are counted per visit, not per fact row.
    """
    by_value: dict[str, dict] = {}
    for f in facts:
        if f.get("fact_type") != fact_type:
            continue
        value = str(f.get("value") or "").strip()
        if not value:
            continue
        key = value.lower()
        d = _to_date(f.get("asserted_at"))
        entry = by_value.setdefault(key, {"term": value, "visits": set(), "dates": []})
        if f.get("visit_id"):
            entry["visits"].add(f["visit_id"])
        if d:
            entry["dates"].append(d)

    out = []
    for entry in by_value.values():
        dates = sorted(entry["dates"])
        occurrences = len(entry["visits"]) or len(dates)
        if occurrences < 2:
            continue
        gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        out.append({
            "term": entry["term"],
            "occurrences": occurrences,
            "first_seen": dates[0].isoformat() if dates else None,
            "last_seen": dates[-1].isoformat() if dates else None,
            "median_gap_days": int(median(gaps)) if gaps else None,
            "span_days": (dates[-1] - dates[0]).days if len(dates) > 1 else 0,
        })
    out.sort(key=lambda x: (-x["occurrences"], x["term"]))
    return out


def medication_timeline(facts: list[dict]) -> dict:
    """Started / stopped / continued, comparing the two most recent visits.

    Only facts the doctor prescribed (`structured.context == "prescribed"`)
    count as the current regimen. Medications the patient *reported* are
    history, and conflating the two is how a system tells a doctor it stopped
    a drug it never started.
    """
    by_visit: dict[str, set[str]] = {}
    visit_order: list[str] = []
    visit_date: dict[str, date | None] = {}

    for f in facts:
        if f.get("fact_type") != "medication":
            continue
        if (f.get("structured") or {}).get("context") != "prescribed":
            continue
        vid = f.get("visit_id")
        if not vid:
            continue
        if vid not in by_visit:
            by_visit[vid] = set()
            visit_order.append(vid)
            visit_date[vid] = _to_date(f.get("asserted_at"))
        value = str(f.get("value") or "").strip()
        if value:
            by_visit[vid].add(value)

    visit_order.sort(key=lambda v: visit_date.get(v) or date.min)
    if len(visit_order) < 2:
        current = sorted(by_visit.get(visit_order[0], set())) if visit_order else []
        return {"visits_compared": len(visit_order), "current": current,
                "started": [], "stopped": [], "continued": current}

    prev, last = by_visit[visit_order[-2]], by_visit[visit_order[-1]]
    lower_prev = {m.lower(): m for m in prev}
    lower_last = {m.lower(): m for m in last}
    return {
        "visits_compared": 2,
        "current": sorted(last),
        "started": sorted(lower_last[k] for k in lower_last.keys() - lower_prev.keys()),
        "stopped": sorted(lower_prev[k] for k in lower_prev.keys() - lower_last.keys()),
        "continued": sorted(lower_last[k] for k in lower_last.keys() & lower_prev.keys()),
    }


def unresolved(facts: list[dict], *, as_of: date | None = None, stale_days: int = 90) -> list[dict]:
    """Problems and symptoms still marked current but not touched recently.

    This is the failure mode longitudinal memory is supposed to prevent: a
    complaint recorded once, never revisited, and quietly treated as history
    because nobody looked. A fact stays on this list until a later visit
    supersedes it or it is marked resolved.
    """
    today = as_of or date.today()
    latest: dict[tuple[str, str], dict] = {}
    for f in facts:
        if f.get("fact_type") not in ("symptom", "diagnosis"):
            continue
        if f.get("status") != "confirmed":
            continue
        value = str(f.get("value") or "").strip()
        if not value:
            continue
        key = (f["fact_type"], value.lower())
        d = _to_date(f.get("asserted_at"))
        prior = latest.get(key)
        if prior is None or (d or date.min) >= (prior["_date"] or date.min):
            latest[key] = {**f, "_date": d}

    out = []
    for (fact_type, _), f in latest.items():
        if (f.get("clinical_status") or "current") != "current":
            continue
        d = f["_date"]
        age = (today - d).days if d else None
        if age is None or age < stale_days:
            continue
        out.append({
            "fact_type": fact_type,
            "value": f.get("value"),
            "last_asserted": d.isoformat() if d else None,
            "days_since": age,
            "note": f"Recorded as current {age} days ago and not revisited since.",
        })
    out.sort(key=lambda x: -(x["days_since"] or 0))
    return out


def build(facts: list[dict], *, as_of: date | None = None) -> dict:
    """The whole longitudinal picture from one pass over the fact store."""
    series: dict[str, list[dict]] = {}
    for f in facts:
        if f.get("fact_type") != "vital":
            continue
        structured = f.get("structured") or {}
        metric = str(structured.get("metric") or "").strip().lower()
        reading = str(structured.get("reading") or f.get("value") or "").strip()
        if not metric or not reading:
            continue
        series.setdefault(metric, []).append(
            {"date": str(f.get("asserted_at") or "")[:10], "value": reading}
        )
        # Blood pressure carries two signals; track the diastolic separately so
        # an isolated diastolic rise is not averaged away.
        if metric == "bp" and "/" in reading:
            series.setdefault("bp_dia", []).append(
                {"date": str(f.get("asserted_at") or "")[:10], "value": reading}
            )

    trends = {m: analyse_series(m, pts) for m, pts in sorted(series.items())}
    flagged = [
        t.metric for t in trends.values()
        if t.significant or (t.step_change and t.step_change.get("detected"))
    ]

    return {
        "trends": {m: _series_json(t) for m, t in trends.items()},
        "flagged_metrics": flagged,
        "recurring_symptoms": recurrence(facts, "symptom"),
        "recurring_diagnoses": recurrence(facts, "diagnosis"),
        "medications": medication_timeline(facts),
        "unresolved": unresolved(facts, as_of=as_of),
        "method": {
            "trend_test": "Mann-Kendall (two-sided, tie-corrected normal approximation)",
            "slope": "Theil-Sen median pairwise slope, reported per 30 days",
            "step_detection": "Median absolute deviation, |z| >= 3 against the prior readings",
            "alpha": ALPHA,
            "min_points": MIN_POINTS_FOR_TREND,
        },
        "disclaimer": "Statistical prompts for physician review. Not a diagnosis.",
    }


def _series_json(a: SeriesAnalysis) -> dict:
    return {
        "metric": a.metric, "n": a.n, "latest": a.latest, "baseline": a.baseline,
        "direction": a.direction, "significant": a.significant, "p_value": a.p_value,
        "slope_per_30d": a.slope_per_30d,
        "change_from_baseline_pct": a.change_from_baseline_pct,
        "step_change": a.step_change, "note": a.note, "points": a.points,
    }
