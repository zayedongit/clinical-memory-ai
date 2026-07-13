"use client";

import { use, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { supabase } from "../../../lib/supabaseClient";
import { apiDelete, apiGet } from "../../../lib/api";

type Patient = { id: string; name: string; gender: string | null; phone: string | null; dob: string | null; height_cm: number | null; weight_kg: number | null };
type VisitRow = { id: string; date: string | null; status: string; summary: string };
type Note = {
  transcript: string | null; dialogue: { speaker: string; text: string }[];
  subjective: string; objective: string; assessment: string; plan: string;
  entities: Record<string, string[]>; follow_up_questions: { question: string; likelihood_pct: number; severity: string }[];
};
type VisitFull = { id: string; date: string | null; note: Note | null };
type Summary = {
  visit_count: number; problems: string[]; medications: string[]; allergies: string[];
  recurring_symptoms: { term: string; count: number }[];
  since_last: { new_symptoms?: string[]; resolved_symptoms?: string[]; new_medications?: string[]; stopped_medications?: string[] };
};

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
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    const [p, v, s] = await Promise.all([apiGet(`/patients/${id}`), apiGet(`/patients/${id}/visits`), apiGet(`/patients/${id}/summary`)]);
    if (p.ok) setPatient(await p.json());
    if (v.ok) setVisits((await v.json()).items || []);
    if (s.ok) setSummary(await s.json());
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

  async function del(vid: string) {
    const r = await apiDelete(`/visits/${vid}`);
    setConfirmDel(null);
    if (r.ok) { setVisits((vs) => vs.filter((v) => v.id !== vid)); setOpen(null); }
  }

  if (loading) return <main className="p-10 text-sm text-slate-500">Loading…</main>;

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header className="mb-6 flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">{patient?.name}</h1>
          <p className="mt-1 text-sm text-slate-500">
            {[patient?.gender, `Age ${ageFrom(patient?.dob ?? null)}`,
              patient?.height_cm ? `${patient.height_cm} cm` : null,
              patient?.weight_kg ? `${patient.weight_kg} kg` : null,
              patient?.phone].filter(Boolean).join(" · ")}
          </p>
        </div>
        <div className="flex items-center gap-4">
          <Link href={`/scribe?patient=${id}`} className="text-sm font-medium text-blue-600 hover:text-blue-700">New consultation</Link>
          <Link href="/patients" className="text-sm text-slate-500 hover:text-slate-900">← Patients</Link>
        </div>
      </header>

      {summary && summary.visit_count > 0 && (
        <div className="glass mb-6 rounded-2xl p-4">
          <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Longitudinal overview</p>
          {summary.problems.length > 0 && <p className="mb-1 text-sm text-slate-600"><span className="font-medium text-slate-500">Problems:</span> {summary.problems.join(", ")}</p>}
          {summary.medications.length > 0 && <p className="mb-1 text-sm text-slate-600"><span className="font-medium text-slate-500">Medications:</span> {summary.medications.join(", ")}</p>}
          {summary.allergies.length > 0 && <p className="mb-1 text-sm text-red-600"><span className="font-medium">Allergies:</span> {summary.allergies.join(", ")}</p>}
          {summary.recurring_symptoms.length > 0 && <p className="mb-1 text-sm text-slate-600"><span className="font-medium text-slate-500">Recurring:</span> {summary.recurring_symptoms.map((r) => `${r.term} ×${r.count}`).join(", ")}</p>}
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
                  <div className="flex items-center justify-between gap-3 border-t border-slate-200/70 bg-red-50/60 px-5 py-3 text-sm">
                    <span className="text-red-700">Are you sure you want to delete this record?</span>
                    <span className="flex gap-2">
                      <button onClick={() => del(v.id)} className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-medium text-white">Yes, delete</button>
                      <button onClick={() => setConfirmDel(null)} className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600">Cancel</button>
                    </span>
                  </div>
                )}

                {open === v.id && (
                  <div className="border-t border-slate-200/70 bg-slate-50/50 px-5 py-4 text-sm">
                    {!full[v.id]?.note ? <p className="text-slate-400">Loading…</p> : <VisitNote note={full[v.id].note!} />}
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

function VisitNote({ note }: { note: Note }) {
  return (
    <div>
      <Field label="Subjective" value={note.subjective} />
      <Field label="Objective" value={note.objective} />
      <Field label="Assessment" value={note.assessment} />
      <Field label="Plan" value={note.plan} />
      {note.follow_up_questions?.length > 0 && (
        <div className="mb-2">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Suggested follow-ups</p>
          <ul className="list-disc pl-5 text-slate-600">
            {note.follow_up_questions.map((f, i) => <li key={i}>{f.question} <span className="text-slate-400">({f.likelihood_pct}%, {f.severity})</span></li>)}
          </ul>
        </div>
      )}
      {note.transcript && <details className="mt-2"><summary className="cursor-pointer text-xs text-slate-400">Transcript</summary><p className="mt-1 whitespace-pre-wrap text-xs text-slate-500">{note.transcript}</p></details>}
    </div>
  );
}
