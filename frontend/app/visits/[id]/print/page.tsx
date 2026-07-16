"use client";

import { use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "../../../../lib/supabaseClient";
import { apiGet } from "../../../../lib/api";

type Rx = { brand: string; generic: string | null; strength: string | null; form: string | null; dose: string; frequency: string; duration: string; instructions: string };
type Note = {
  subjective: string; objective: string; assessment: string; plan: string;
  entities?: Record<string, string[]>; prescription?: Rx[]; attested?: boolean;
};
type Visit = { id: string; date: string | null; patient_id: string; consent_given?: boolean; consent_method?: string | null; note: Note | null };
type Patient = { id: string; name: string; gender: string | null; phone: string | null; dob: string | null; height_cm: number | null; weight_kg: number | null };

function ageFrom(dob: string | null): string {
  if (!dob) return "—";
  const y = new Date(dob).getFullYear();
  return y ? `${new Date().getFullYear() - y}` : "—";
}
function fmtDate(d: string | null): string {
  return d ? new Date(d).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" }) : "—";
}

export default function PrintVisit({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const [visit, setVisit] = useState<Visit | null>(null);
  const [patient, setPatient] = useState<Patient | null>(null);
  const [clinic, setClinic] = useState<string>("Clinical Memory AI");
  const [doctor, setDoctor] = useState<string>("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      const { data } = await supabase.auth.getSession();
      if (!data.session) { router.push("/login"); return; }
      setDoctor(data.session.user.email || "");
      const v = await apiGet(`/visits/${id}`);
      if (v.ok) {
        const vj = (await v.json()) as Visit;
        setVisit(vj);
        const [p, me] = await Promise.all([apiGet(`/patients/${vj.patient_id}`), apiGet(`/me`)]);
        if (p.ok) setPatient(await p.json());
        if (me.ok) { const m = await me.json(); if (m.clinic_name) setClinic(m.clinic_name); }
      }
      setLoading(false);
    })();
  }, [id, router]);

  if (loading) return <main className="p-10 text-sm text-slate-500">Loading…</main>;
  if (!visit || !visit.note) return <main className="p-10 text-sm text-slate-500">Visit not found.</main>;

  const n = visit.note;
  const rx = n.prescription || [];
  const meta = [patient?.gender, `Age ${ageFrom(patient?.dob ?? null)}`,
    patient?.height_cm ? `${patient.height_cm} cm` : null,
    patient?.weight_kg ? `${patient.weight_kg} kg` : null,
    patient?.phone].filter(Boolean).join("  ·  ");

  return (
    <div className="mx-auto max-w-[820px] px-6 py-8 text-slate-900">
      <style>{`
        @media print {
          .no-print { display: none !important; }
          @page { size: A4; margin: 15mm; }
          html, body { background: #fff; }
        }
      `}</style>

      {/* toolbar (screen only) */}
      <div className="no-print mb-6 flex items-center justify-between">
        <button onClick={() => router.back()} className="text-sm text-slate-500 hover:text-slate-900">← Back</button>
        <button onClick={() => window.print()} className="rounded-xl bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700">Print / Save as PDF</button>
      </div>

      <article className="rounded-lg border border-slate-200 p-8 print:border-0 print:p-0">
        {/* header */}
        <header className="flex items-start justify-between border-b-2 border-slate-800 pb-3">
          <div>
            <h1 className="text-xl font-bold tracking-tight">{clinic}</h1>
            <p className="text-xs text-slate-500">Consultation record &amp; prescription</p>
          </div>
          <div className="text-right text-xs text-slate-600">
            <p>Date: <span className="font-medium text-slate-900">{fmtDate(visit.date)}</span></p>
            {doctor && <p className="mt-0.5">Attending: {doctor}</p>}
          </div>
        </header>

        {/* patient bar */}
        <section className="mt-4 flex items-baseline justify-between">
          <h2 className="text-lg font-semibold">{patient?.name || "Patient"}</h2>
          <p className="text-xs text-slate-600">{meta}</p>
        </section>

        {/* clinical summary */}
        <div className="mt-5 grid grid-cols-1 gap-4">
          {n.subjective && <Block label="Complaint / History">{n.subjective}</Block>}
          {n.objective && <Block label="Examination">{n.objective}</Block>}
          {n.assessment && <Block label="Assessment / Provisional diagnosis" mono>{n.assessment}</Block>}
        </div>

        {/* Rx */}
        <section className="mt-6">
          <div className="mb-1 flex items-baseline gap-2">
            <span className="text-2xl font-serif font-bold italic">℞</span>
            <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Prescription</span>
          </div>
          {rx.length === 0 ? (
            <p className="text-sm text-slate-400">No medication prescribed.</p>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-y border-slate-300 text-left text-[11px] uppercase tracking-wide text-slate-500">
                  <th className="py-1.5 pr-2 font-semibold">#</th>
                  <th className="py-1.5 pr-2 font-semibold">Medicine</th>
                  <th className="py-1.5 pr-2 font-semibold">Dosage</th>
                  <th className="py-1.5 pr-2 font-semibold">Frequency</th>
                  <th className="py-1.5 pr-2 font-semibold">Duration</th>
                  <th className="py-1.5 font-semibold">Instructions</th>
                </tr>
              </thead>
              <tbody>
                {rx.map((r, i) => (
                  <tr key={i} className="border-b border-slate-100 align-top">
                    <td className="py-2 pr-2 text-slate-400">{i + 1}</td>
                    <td className="py-2 pr-2">
                      <div className="font-medium">{r.brand}</div>
                      <div className="text-xs text-slate-500">{[r.generic, r.strength, r.form].filter(Boolean).join(" · ")}</div>
                    </td>
                    <td className="py-2 pr-2">{r.dose || "—"}</td>
                    <td className="py-2 pr-2">{r.frequency || "—"}</td>
                    <td className="py-2 pr-2">{r.duration || "—"}</td>
                    <td className="py-2">{r.instructions || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        {/* advice */}
        {n.plan && <div className="mt-6"><Block label="Advice / Plan">{n.plan}</Block></div>}

        {/* footer: attestation + disclaimer */}
        <footer className="mt-10 border-t border-slate-200 pt-4 text-[11px] text-slate-500">
          <div className="flex items-end justify-between">
            <div className="max-w-[60%]">
              <p>Documented with AI assistance and <span className="font-medium text-slate-700">reviewed &amp; approved by the treating physician</span>.
                {visit.consent_given && " Patient consented to recording of the consultation."}</p>
              <p className="mt-1">This is not an autonomous AI diagnosis or prescription; the physician is the responsible clinician.</p>
            </div>
            <div className="text-center">
              <div className="mb-1 h-10 w-44 border-b border-slate-400"></div>
              <p className="text-slate-700">{doctor || "Physician signature"}</p>
              {n.attested && <p className="text-emerald-600">✓ Attested</p>}
            </div>
          </div>
        </footer>
      </article>
    </div>
  );
}

function Block({ label, children, mono }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return (
    <div>
      <p className="mb-0.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500">{label}</p>
      <p className={`whitespace-pre-wrap text-sm text-slate-800 ${mono ? "leading-relaxed" : ""}`}>{children}</p>
    </div>
  );
}
