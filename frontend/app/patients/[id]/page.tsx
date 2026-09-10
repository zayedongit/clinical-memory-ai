"use client";

import { use, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { supabase } from "../../../lib/supabaseClient";
import { apiGet, apiPost, errorMessage } from "../../../lib/api";

type Patient = { id: string; name: string; uhid: string | null; gender: string | null; phone: string | null; dob: string | null; height_cm: number | null; weight_kg: number | null };
type VisitRow = { id: string; date: string | null; status: string; summary: string };
type RedFlag = { finding: string; concern: string; urgency: string; action: string; source?: string };
type Considerations = { red_flags?: RedFlag[]; missing_information?: string[]; suggested_investigations?: { test: string; rationale: string }[]; completeness_pct?: number };
type Note = {
  transcript: string | null; dialogue: { speaker: string; text: string }[];
  subjective: string; objective: string; assessment: string; plan: string;
  entities: Record<string, string[]>; follow_up_questions: { question: string; likelihood_pct: number; severity: string }[];
  prescription?: { brand: string; generic: string | null; strength: string | null; form: string | null; dose: string; frequency: string; duration: string; instructions: string }[];
  clinical_considerations?: Considerations; attested?: boolean;
  vitals?: Record<string, string>;
};
type VisitFull = { id: string; date: string | null; note: Note | null; consent_given?: boolean; consent_method?: string | null };
type Summary = {
  visit_count: number; problems: string[]; medications: string[]; allergies: string[];
  recurring_symptoms: { term: string; occurrences: number; last_seen: string | null; median_gap_days: number | null }[];
  allergy_status: "documented" | "documented_none" | "not_recorded";
  flagged_metrics: string[];
  since_last: { new_symptoms?: string[]; resolved_symptoms?: string[]; new_medications?: string[]; stopped_medications?: string[] };
};

type Trend = {
  metric: string; n: number; latest: number | null; baseline: number | null;
  direction: "rising" | "falling" | "stable" | "insufficient_data";
  significant: boolean; p_value: number; slope_per_30d: number | null;
  change_from_baseline_pct: number | null;
  step_change: { detected: boolean; baseline: number; latest: number; z: number | null; note: string } | null;
  note: string; points: { date: string; value: number }[];
};
type Analytics = {
  available: boolean;
  trends: Record<string, Trend>;
  flagged_metrics: string[];
  recurring_symptoms: { term: string; occurrences: number; median_gap_days: number | null; span_days: number }[];
  medications: { current: string[]; started: string[]; stopped: string[]; continued: string[]; visits_compared: number };
  unresolved: { fact_type: string; value: string; days_since: number; note: string }[];
  method: { trend_test: string; slope: string; step_detection: string; alpha: number; min_points: number };
  disclaimer: string;
};

const METRIC_LABEL: Record<string, string> = {
  bp: "BP (systolic)", bp_dia: "BP (diastolic)", hr: "Heart rate", spo2: "SpO\u2082",
  temp: "Temperature", rr: "Resp. rate", weight: "Weight", height: "Height",
};

const DIRECTION_STYLE: Record<string, string> = {
  rising: "bg-amber-100 text-amber-800",
  falling: "bg-blue-100 text-blue-800",
  stable: "bg-slate-100 text-slate-600",
  insufficient_data: "bg-slate-50 text-slate-400",
};

function Sparkline({ points }: { points: { value: number }[] }) {
  const values = points.map((p) => p.value).filter((v) => Number.isFinite(v));
  if (values.length < 2) return null;
  const w = 72, h = 20;
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1, step = w / (values.length - 1);
  const d = values
    .map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - min) / span) * h).toFixed(1)}`)
    .join(" ");
  return (
    <svg width={w} height={h} className="text-blue-500" role="img" aria-label="trend sparkline">
      <polyline points={d} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

/**
 * Longitudinal analytics. Every claim is labelled with the test that produced
 * it and its p-value, because "BP is rising" and "BP is rising (Mann-Kendall
 * p=0.01, n=6)" are different statements, and only the second one is checkable.
 */
function AnalyticsPanel({ a }: { a: Analytics }) {
  const trends = Object.values(a.trends).filter((t) => t.n >= 2);
  const hasContent =
    trends.length > 0 || a.unresolved.length > 0 ||
    a.medications.started.length > 0 || a.medications.stopped.length > 0;
  if (!hasContent) return null;

  return (
    <section className="glass mb-6 rounded-2xl p-4" aria-label="Longitudinal analytics">
      <div className="mb-2 flex items-baseline justify-between">
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Longitudinal analytics</p>
        <span className="text-[10px] text-slate-400">{a.method.trend_test}</span>
      </div>

      {trends.length > 0 && (
        <div className="space-y-1.5">
          {trends.map((t) => (
            <div key={t.metric} className="flex flex-wrap items-center gap-2 text-sm">
              <span className="w-28 shrink-0 text-slate-500">{METRIC_LABEL[t.metric] ?? t.metric}</span>
              <Sparkline points={t.points} />
              <span className="font-medium text-slate-800">{t.latest ?? "—"}</span>
              <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${DIRECTION_STYLE[t.direction]}`}>
                {t.direction.replace("_", " ")}
              </span>
              {t.significant && <span className="text-[11px] text-slate-500">p={t.p_value}</span>}
              {t.step_change?.detected && (
                <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800">
                  step change
                </span>
              )}
              <span className="text-[11px] text-slate-400">{t.note}</span>
            </div>
          ))}
        </div>
      )}

      {(a.medications.started.length > 0 || a.medications.stopped.length > 0) && (
        <p className="mt-2 border-t border-slate-200/70 pt-2 text-sm text-slate-600">
          <span className="font-medium text-slate-500">Medication changes:</span>{" "}
          {[
            a.medications.started.length ? `started ${a.medications.started.join(", ")}` : "",
            a.medications.stopped.length ? `stopped ${a.medications.stopped.join(", ")}` : "",
          ].filter(Boolean).join(" · ")}
        </p>
      )}

      {a.unresolved.length > 0 && (
        <div className="mt-2 border-t border-slate-200/70 pt-2">
          <p className="text-xs font-medium text-amber-800">Recorded as current but not revisited</p>
          <ul className="mt-1 space-y-0.5 text-sm text-slate-700">
            {a.unresolved.slice(0, 5).map((u, i) => (
              <li key={i}>{u.value} <span className="text-slate-400">· {u.days_since} days ago</span></li>
            ))}
          </ul>
        </div>
      )}

      <p className="mt-2 text-[11px] text-slate-400">{a.disclaimer}</p>
    </section>
  );
}

function ageFrom(dob: string | null): string {
  if (!dob) return "—";
  const y = new Date(dob).getFullYear();
  if (!y) return "—";
  return `${new Date().getFullYear() - y}`;
}
function fmt(d: string | null): string {
  return d ? new Date(d).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "—";
}

export default function PatientDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const [patient, setPatient] = useState<Patient | null>(null);
  const [visits, setVisits] = useState<VisitRow[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [full, setFull] = useState<Record<string, VisitFull>>({});
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const [delReason, setDelReason] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    const [p, v, s, a] = await Promise.all([
      apiGet(`/patients/${id}`),
      apiGet(`/patients/${id}/visits`),
      apiGet(`/patients/${id}/summary`),
      apiGet(`/patients/${id}/analytics`),
    ]);
    if (p.ok) setPatient(await p.json());
    if (v.ok) setVisits((await v.json()).items || []);
    if (s.ok) setSummary(await s.json());
    if (a.ok) { const data = await a.json(); if (data.available) setAnalytics(data); }
    setLoading(false);
  }, [id]);

  useEffect(() => {
    (async () => {
      const { data } = await supabase.auth.getSession();
      if (!data.session) return router.push("/login");
      await load();
    })();
  }, [router, load]);

  async function toggle(vid: string) {
    if (open === vid) { setOpen(null); return; }
    setOpen(vid);
    if (!full[vid]) {
      const r = await apiGet(`/visits/${vid}`);
      if (r.ok) {
        const data = (await r.json()) as VisitFull;
        setFull((f) => ({ ...f, [vid]: data }));
      }
    }
  }

  // Removal is a soft delete with a mandatory reason: a clinical record is a
  // medico-legal document, so it is retained and hidden, and *why* it was
  // hidden is part of the audit trail.
  async function del(vid: string) {
    if (!delReason.trim()) return;
    setErr(null);
    const r = await apiPost(`/visits/${vid}/delete`, { reason: delReason.trim() });
    if (!r.ok) { setErr(await errorMessage(r)); return; }
    setConfirmDel(null);
    setDelReason("");
    setVisits((vs) => vs.filter((v) => v.id !== vid));
    setOpen(null);
  }

  if (loading) return <main className="p-10 text-sm text-slate-500">Loading…</main>;

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header className="mb-6 flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">{patient?.name}</h1>
          {patient?.uhid && <p className="mt-0.5 text-xs font-mono text-slate-400">{patient.uhid}</p>}
          <p className="mt-1 text-sm text-slate-500">
            {[patient?.gender, `Age ${ageFrom(patient?.dob ?? null)}`,
              patient?.height_cm ? `${patient.height_cm} cm` : null,
              patient?.weight_kg ? `${patient.weight_kg} kg` : null,
              patient?.phone].filter(Boolean).join(" · ")}
          </p>
        </div>
        <div className="flex items-center gap-4">
          <Link href={`/consult?patient=${id}`} className="text-sm font-medium text-blue-600 hover:text-blue-700">New consultation</Link>
          <Link href="/patients" className="text-sm text-slate-500 hover:text-slate-900">← Patients</Link>
        </div>
      </header>

      {summary && summary.visit_count > 0 && (
        <div className="glass mb-6 rounded-2xl p-4">
          <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Longitudinal overview</p>
          {summary.problems.length > 0 && <p className="mb-1 text-sm text-slate-600"><span className="font-medium text-slate-500">Problems:</span> {summary.problems.join(", ")}</p>}
          {summary.medications.length > 0 && <p className="mb-1 text-sm text-slate-600"><span className="font-medium text-slate-500">Medications:</span> {summary.medications.join(", ")}</p>}
          {summary.allergies.length > 0
            ? <p className="mb-1 text-sm text-red-600"><span className="font-medium">Allergies:</span> {summary.allergies.join(", ")}</p>
            : summary.allergy_status === "documented_none"
              ? <p className="mb-1 text-sm text-slate-600"><span className="font-medium text-slate-500">Allergies:</span> none known <span className="text-slate-400">(documented)</span></p>
              : <p className="mb-1 text-sm text-amber-700"><span className="font-medium">Allergies:</span> not recorded</p>}
          {summary.recurring_symptoms.length > 0 && <p className="mb-1 text-sm text-slate-600"><span className="font-medium text-slate-500">Recurring:</span> {summary.recurring_symptoms.map((r) => `${r.term} ×${r.occurrences}`).join(", ")}</p>}
          {(() => {
            const sl = summary.since_last || {};
            const bits = [
              sl.new_symptoms?.length ? `new: ${sl.new_symptoms.join(", ")}` : "",
              sl.resolved_symptoms?.length ? `resolved: ${sl.resolved_symptoms.join(", ")}` : "",
              sl.new_medications?.length ? `started: ${sl.new_medications.join(", ")}` : "",
              sl.stopped_medications?.length ? `stopped: ${sl.stopped_medications.join(", ")}` : "",
            ].filter(Boolean);
            return bits.length ? <p className="mt-2 border-t border-slate-200/70 pt-2 text-sm text-slate-600"><span className="font-medium text-slate-500">Since last visit:</span> {bits.join(" · ")}</p> : null;
          })()}
        </div>
      )}

      {analytics && <AnalyticsPanel a={analytics} />}

      {err && <p className="mb-4 rounded-xl border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{err}</p>}

      <h2 className="mb-2 text-sm font-semibold text-slate-700">Visit history</h2>
      <div className="glass overflow-hidden rounded-2xl">
        {visits.length === 0 ? (
          <p className="p-6 text-sm text-slate-400">No visits yet. Record a consultation and save it here.</p>
        ) : (
          <ul className="divide-y divide-slate-200/70">
            {visits.map((v) => (
              <li key={v.id}>
                <div className="flex items-center justify-between px-5 py-3.5">
                  <button onClick={() => toggle(v.id)} className="min-w-0 flex-1 text-left">
                    <span className="text-sm font-medium text-slate-900">{fmt(v.date)}</span>
                    <span className="ml-3 text-xs text-slate-500">{v.summary || "Visit"}</span>
                  </button>
                  <button onClick={() => setConfirmDel(v.id)} className="ml-3 shrink-0 text-xs text-slate-400 hover:text-red-600">Delete</button>
                </div>

                {confirmDel === v.id && (
                  <div className="border-t border-slate-200/70 bg-amber-50/70 px-5 py-3 text-sm">
                    <p className="font-medium text-amber-900">Remove this record from view?</p>
                    <p className="mt-0.5 text-xs text-amber-800">
                      Clinical records are retained, not destroyed. This visit will be hidden and the
                      reason you give is recorded in the audit trail.
                    </p>
                    <input
                      value={delReason}
                      onChange={(e) => setDelReason(e.target.value)}
                      placeholder="Reason (e.g. duplicate entry, recorded against the wrong patient)"
                      aria-label="Reason for removing this record"
                      className="field mt-2 w-full"
                    />
                    <div className="mt-2 flex gap-2">
                      <button
                        onClick={() => del(v.id)}
                        disabled={delReason.trim().length < 3}
                        className="rounded-lg bg-amber-600 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
                      >
                        Remove &amp; record reason
                      </button>
                      <button
                        onClick={() => { setConfirmDel(null); setDelReason(""); }}
                        className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}

                {open === v.id && (
                  <div className="border-t border-slate-200/70 bg-slate-50/50 px-5 py-4 text-sm">
                    <div className="mb-3 flex justify-end">
                      <Link href={`/visits/${v.id}/print`} className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-blue-600 hover:bg-blue-50">Print / Export PDF</Link>
                    </div>
                    {!full[v.id]?.note ? <p className="text-slate-400">Loading…</p> : <VisitNote note={full[v.id].note!} visit={full[v.id]} />}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </main>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div className="mb-2">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</p>
      <p className="text-slate-700">{value}</p>
    </div>
  );
}

const urgencyTag: Record<string, string> = { emergency: "bg-red-600 text-white", urgent: "bg-amber-500 text-white", routine: "bg-slate-400 text-white" };

function VisitNote({ note, visit }: { note: Note; visit?: VisitFull }) {
  const cc = note.clinical_considerations || {};
  const redFlags = cc.red_flags || [];
  return (
    <div>
      {redFlags.length > 0 && (
        <div className="mb-3 space-y-1.5">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Red flags raised <span className="font-normal normal-case text-slate-400">(reviewed by physician)</span></p>
          {redFlags.map((f, i) => (
            <div key={i} className="rounded-lg border border-red-200 bg-red-50 px-3 py-2">
              <div className="flex items-start justify-between gap-2">
                <span className="text-sm font-medium text-slate-800">⚠ {f.finding}</span>
                <span className="flex shrink-0 items-center gap-1">
                  {f.source === "kb" && <span className="rounded bg-indigo-100 px-1.5 py-0.5 text-[9px] font-bold uppercase text-indigo-700">ICMR KB</span>}
                  <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${urgencyTag[f.urgency] || urgencyTag.routine}`}>{f.urgency}</span>
                </span>
              </div>
              {f.concern && <p className="mt-0.5 text-xs text-slate-600">{f.concern}</p>}
            </div>
          ))}
        </div>
      )}
      {note.vitals && Object.values(note.vitals).some(Boolean) && (
        <div className="mb-2">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Vitals</p>
          <p className="text-slate-700">{Object.entries({ BP: note.vitals.bp, HR: note.vitals.hr, Temp: note.vitals.temp, "SpO₂": note.vitals.spo2, RR: note.vitals.rr }).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join("  ·  ")}</p>
        </div>
      )}
      <Field label="Subjective" value={note.subjective} />
      <Field label="Objective" value={note.objective} />
      <Field label="Assessment" value={note.assessment} />
      <Field label="Plan" value={note.plan} />
      {note.prescription && note.prescription.length > 0 && (
        <div className="mb-2">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Prescription</p>
          <ul className="mt-1 space-y-1">
            {note.prescription.map((p, i) => (
              <li key={i} className="text-slate-700">
                <span className="font-medium">{p.brand}</span>
                <span className="text-slate-500"> {[p.generic, p.strength, p.form].filter(Boolean).join(" · ")}</span>
                <span className="text-slate-600"> — {[p.dose, p.frequency, p.duration].filter(Boolean).join(", ")}{p.instructions ? ` (${p.instructions})` : ""}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {note.follow_up_questions?.length > 0 && (
        <div className="mb-2">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Suggested follow-ups</p>
          <ul className="list-disc pl-5 text-slate-600">
            {note.follow_up_questions.map((f, i) => <li key={i}>{f.question} <span className="text-slate-400">({f.likelihood_pct}%, {f.severity})</span></li>)}
          </ul>
        </div>
      )}
      {note.transcript && <details className="mt-2"><summary className="cursor-pointer text-xs text-slate-400">Transcript</summary><p className="mt-1 whitespace-pre-wrap text-xs text-slate-500">{note.transcript}</p></details>}
      <div className="mt-3 flex flex-wrap gap-2 border-t border-slate-200/70 pt-2 text-[11px]">
        {note.attested && <span className="rounded bg-emerald-100 px-1.5 py-0.5 font-medium text-emerald-700">✓ Physician-attested</span>}
        {visit?.consent_given && <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-500">Recording consent: {visit.consent_method || "given"}</span>}
      </div>
    </div>
  );
}
