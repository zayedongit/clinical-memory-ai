"""A small in-process metrics registry, exposed in Prometheus text format.

Deliberately dependency-free and deliberately small. The goal is to answer
the operational questions this system actually raises — *is the AI pipeline
failing, how often does it fall back to another provider, and what is it
costing us* — without adding an observability platform to a prototype.

Semantics and limits, stated plainly:

* Counters and histograms live in this process only. With N workers you get
  N independent series; scrape each one, or accept that a single-process
  deployment is the supported configuration today.
* Nothing is persisted. A restart resets every series.
* Labels are low-cardinality by construction: the only label values are
  provider names, route groups, and outcome strings. Nothing derived from
  patient data or request bodies is ever a label, because label values end
  up in a metrics endpoint that is not access-controlled the way the API is.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

# Latency buckets in seconds. Chosen for this workload: sub-second database
# reads at the low end, multi-second LLM calls in the middle, and a long tail
# for speech-to-text on a several-minute audio segment.
_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

Labels = tuple[tuple[str, str], ...]


def _labels(d: dict[str, str] | None) -> Labels:
    return tuple(sorted((str(k), str(v)) for k, v in (d or {}).items()))


@dataclass
class _Histogram:
    counts: list[int] = field(default_factory=lambda: [0] * (len(_BUCKETS) + 1))
    total: float = 0.0
    n: int = 0

    def observe(self, seconds: float) -> None:
        self.total += seconds
        self.n += 1
        for i, edge in enumerate(_BUCKETS):
            if seconds <= edge:
                self.counts[i] += 1
                return
        self.counts[-1] += 1


class Registry:
    """Thread-safe counters, gauges and histograms."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, Labels], float] = defaultdict(float)
        self._gauges: dict[tuple[str, Labels], float] = {}
        self._hists: dict[tuple[str, Labels], _Histogram] = defaultdict(_Histogram)
        self._help: dict[str, str] = {}

    def describe(self, name: str, help_text: str) -> None:
        self._help[name] = help_text

    def inc(self, name: str, labels: dict[str, str] | None = None, by: float = 1.0) -> None:
        with self._lock:
            self._counters[(name, _labels(labels))] += by

    def gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._gauges[(name, _labels(labels))] = value

    def observe(self, name: str, seconds: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._hists[(name, _labels(labels))].observe(seconds)

    def snapshot(self) -> dict:
        """A JSON-friendly view, used by tests and the engineering dashboard."""
        with self._lock:
            return {
                "counters": {
                    _key(n, lb): v for (n, lb), v in sorted(self._counters.items())
                },
                "gauges": {_key(n, lb): v for (n, lb), v in sorted(self._gauges.items())},
                "histograms": {
                    _key(n, lb): {
                        "count": h.n,
                        "sum_seconds": round(h.total, 4),
                        "avg_seconds": round(h.total / h.n, 4) if h.n else 0.0,
                    }
                    for (n, lb), h in sorted(self._hists.items())
                },
            }

    def render(self) -> str:
        """Prometheus text exposition format."""
        out: list[str] = []
        with self._lock:
            emitted: set[str] = set()

            def header(name: str, kind: str) -> None:
                if name in emitted:
                    return
                emitted.add(name)
                if name in self._help:
                    out.append(f"# HELP {name} {self._help[name]}")
                out.append(f"# TYPE {name} {kind}")

            for (name, lb), value in sorted(self._counters.items()):
                header(name, "counter")
                out.append(f"{name}{_fmt(lb)} {value:g}")
            for (name, lb), value in sorted(self._gauges.items()):
                header(name, "gauge")
                out.append(f"{name}{_fmt(lb)} {value:g}")
            for (name, lb), hist in sorted(self._hists.items()):
                header(name, "histogram")
                cumulative = 0
                for i, edge in enumerate(_BUCKETS):
                    cumulative += hist.counts[i]
                    out.append(f"{name}_bucket{_fmt(lb, le=str(edge))} {cumulative}")
                cumulative += hist.counts[-1]
                out.append(f"{name}_bucket{_fmt(lb, le='+Inf')} {cumulative}")
                out.append(f"{name}_sum{_fmt(lb)} {hist.total:g}")
                out.append(f"{name}_count{_fmt(lb)} {hist.n}")
        return "\n".join(out) + "\n"

    def reset(self) -> None:
        """Tests only."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._hists.clear()


def _key(name: str, labels: Labels) -> str:
    return name if not labels else name + "{" + ",".join(f"{k}={v}" for k, v in labels) + "}"


def _fmt(labels: Labels, **extra: str) -> str:
    items = list(labels) + sorted(extra.items())
    if not items:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in items)
    return "{" + inner + "}"


def _escape(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


registry = Registry()

registry.describe("cma_http_requests_total", "HTTP requests by route group, method and status class.")
registry.describe("cma_http_request_seconds", "HTTP request latency by route group.")
registry.describe("cma_ai_calls_total", "AI provider calls by capability, provider and outcome.")
registry.describe("cma_ai_call_seconds", "AI provider call latency by capability and provider.")
registry.describe("cma_ai_fallbacks_total", "Times a capability fell through to a lower-preference provider.")
registry.describe("cma_ai_tokens_total", "Tokens billed by capability and provider (prompt + completion).")
registry.describe("cma_ai_cost_usd_total", "Estimated USD spend by capability and provider.")
registry.describe("cma_ai_json_repairs_total", "Model responses that needed JSON salvage before parsing.")
registry.describe("cma_citations_total", "Model-supplied quotes checked against the transcript, by verdict.")
registry.describe("cma_rate_limited_total", "Requests rejected by the rate limiter, by route group.")
registry.describe("cma_audit_write_failures_total", "Audit-log writes that did not land.")


class Timer:
    """``with Timer('cma_ai_call_seconds', {'provider': 'gemini'}):``"""

    def __init__(self, name: str, labels: dict[str, str] | None = None) -> None:
        self.name, self.labels = name, labels
        self.started = 0.0
        self.elapsed = 0.0

    def __enter__(self) -> Timer:
        self.started = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed = time.perf_counter() - self.started
        registry.observe(self.name, self.elapsed, self.labels)
