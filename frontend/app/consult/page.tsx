"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { supabase } from "../../lib/supabaseClient";
import { apiGet, apiPost, apiUpload } from "../../lib/api";

function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const buf = new ArrayBuffer(44 + samples.length * 2); const view = new DataView(buf);
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  str(36, "data"); view.setUint32(40, samples.length * 2, true);
  let o = 44; for (let i = 0; i < samples.length; i++) { const v = Math.max(-1, Math.min(1, samples[i])); view.setInt16(o, v < 0 ? v * 0x8000 : v * 0x7fff, true); o += 2; }
  return new Blob([view], { type: "audio/wav" });
}
function flatten(chunks: Float32Array[]): Float32Array { const len = chunks.reduce((a, c) => a + c.length, 0); const out = new Float32Array(len); let off = 0; for (const c of chunks) { out.set(c, off); off += c.length; } return out; }
type Extracted = { chief_complaints: Complaint[]; hpi: string; past_history: string; allergies: string; medications: string; general_exam: string; systemic_exam: string; vitals: Record<string, string> };

type Patient = { id: string; name: string; uhid: string | null; gender: string | null; phone: string | null; dob: string | null; height_cm: number | null; weight_kg: number | null };
type Vitals = { weight: string; height: string; bp_sys: string; bp_dia: string; hr: string; spo2: string; temp: string; rr: string };
type Complaint = { text: string; duration: string };
type Encounter = { complaints: Complaint[]; hpi: string; past_history: string; allergies: string; medications: string; general_exam: string; systemic_exam: string };
type Ddx = { diagnosis: string; likelihood: string; reasoning: string; icd10: string };
type Investigation = { investigation: string; urgency: string; rationale: string; mnm_floor: boolean };
type TxDrug = { drug: string; dose: string; route: string; frequency: string; duration: string; brands: string[]; dose_needs_doctor: boolean; dose_flag: string };
type Treatment = { diagnosis: string; first_line: TxDrug[]; non_pharmacological: string[] };
type DS = { available: boolean; differential_diagnosis: Ddx[]; must_not_miss: { diagnosis: string }[]; investigations: Investigation[]; treatment: Treatment[] };
type RxItem = { drug: string; brand: string; dose: string; frequency: string; duration: string; route: string };

type Section = "vitals" | "complaints" | "history" | "general" | "systemic";
const SECTIONS: { key: Section; label: string }[] = [
  { key: "vitals", label: "Vitals" }, { key: "complaints", label: "Chief Complaints" },
  { key: "history", label: "History" }, { key: "general", label: "General Examination" },
  { key: "systemic", label: "Systemic Examination" },
];
const urgTag: Record<string, string> = { immediate: "bg-red-600 text-white", urgent: "bg-amber-500 text-white", routine: "bg-slate-400 text-white" };
const likeTag: Record<string, string> = { high: "bg-red-100 text-red-700", moderate: "bg-amber-100 text-amber-700", medium: "bg-amber-100 text-amber-700", low: "bg-slate-100 text-slate-600" };

function ageFrom(dob: string | null): string { if (!dob) return "—"; const y = new Date(dob).getFullYear(); return y ? `${new Date().getFullYear() - y}y` : "—"; }

export default function ConsultWizard() {
  const router = useRouter();
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [section, setSection] = useState<Section>("vitals");
  const [patient, setPatient] = useState<Patient | null>(null);
  const [loading, setLoading] = useState(true);

  const [vitals, setVitals] = useState<Vitals>({ weight: "", height: "", bp_sys: "", bp_dia: "", hr: "", spo2: "", temp: "", rr: "" });
  const [enc, setEnc] = useState<Encounter>({ complaints: [], hpi: "", past_history: "", allergies: "", medications: "", general_exam: "", systemic_exam: "" });
  const [cText, setCText] = useState(""); const [cDur, setCDur] = useState("");

  // step 2 synthesis
  const [ds, setDs] = useState<DS | null>(null);
  const [dsBusy, setDsBusy] = useState<string | null>(null);
  const [sub, setSub] = useState<"dx" | "ix" | "tx">("dx");
  const [primaryDx, setPrimaryDx] = useState<Ddx | null>(null);
  const [selIx, setSelIx] = useState<Set<number>>(new Set());
  const [rx, setRx] = useState<RxItem[]>([]);
  // step 3
  const [attested, setAttested] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [scribeOpen, setScribeOpen] = useState(false);
  const [draftVisitId, setDraftVisitId] = useState<string | null>(null);
  const [draftSaved, setDraftSaved] = useState(false);

  function applyExtract(ex: Extracted) {
    setEnc((e) => ({
      complaints: ex.chief_complaints?.length ? ex.chief_complaints : e.complaints,
      hpi: ex.hpi || e.hpi, past_history: ex.past_history || e.past_history,
      allergies: ex.allergies || e.allergies, medications: ex.medications || e.medications,
      general_exam: ex.general_exam || e.general_exam, systemic_exam: ex.systemic_exam || e.systemic_exam,
    }));
    const mv = ex.vitals || {};
    setVitals((v) => {
      const n = { ...v };
      if (mv.bp) { const [s, d] = mv.bp.split("/"); if (s) n.bp_sys = s.trim(); if (d) n.bp_dia = d.trim(); }
      for (const k of ["hr", "temp", "spo2", "rr", "weight", "height"] as const) if (mv[k]) (n as Record<string, string>)[k] = mv[k];
      return n;
    });
    setScribeOpen(false); setSection("complaints");
  }

  useEffect(() => {
    (async () => {
      const { data } = await supabase.auth.getSession();
      if (!data.session) { router.push("/login"); return; }
      const params = new URLSearchParams(window.location.search);
      const vid = params.get("visit");
      if (vid) await loadDraft(vid);
      else { const pid = params.get("patient"); if (pid) await attach(pid); }
      setLoading(false);
    })();
  }, [router]);

  async function attach(pid: string) {
    const r = await apiGet(`/patients/${pid}`);
    if (r.ok) { const p = await r.json(); setPatient(p); setVitals((v) => ({ ...v, weight: p.weight_kg ? String(p.weight_kg) : v.weight, height: p.height_cm ? String(p.height_cm) : v.height })); }
  }

  const bmi = useMemo(() => { const w = parseFloat(vitals.weight), h = parseFloat(vitals.height); return w && h ? (w / ((h / 100) ** 2)).toFixed(1) : ""; }, [vitals.weight, vitals.height]);
  const done: Record<Section, boolean> = {
    vitals: !!(vitals.bp_sys || vitals.hr || vitals.temp || vitals.spo2 || vitals.weight),
    complaints: enc.complaints.length > 0, history: !!(enc.hpi || enc.past_history || enc.allergies || enc.medications),
    general: !!enc.general_exam, systemic: !!enc.systemic_exam,
  };

  function addComplaint() { if (!cText.trim()) return; setEnc({ ...enc, complaints: [...enc.complaints, { text: cText.trim(), duration: cDur.trim() }] }); setCText(""); setCDur(""); }
  function cleanVitals(): Record<string, string> { const o: Record<string, string> = {}; if (vitals.bp_sys || vitals.bp_dia) o.bp = `${vitals.bp_sys}/${vitals.bp_dia}`; if (vitals.hr) o.hr = vitals.hr; if (vitals.temp) o.temp = vitals.temp; if (vitals.spo2) o.spo2 = vitals.spo2; if (vitals.rr) o.rr = vitals.rr; return o; }

  async function goToPrescription() {
    setStep(2); setSub("dx"); setErr(null);
    if (ds || enc.complaints.length === 0) return;
    setDsBusy("Analysing with Clinical Synthesis…");
    const r = await apiPost("/synthesis/decision-support", {
      chief_complaints: enc.complaints.map((c) => c.text), age: ageFrom(patient?.dob ?? null).replace("y", ""),
      gender: patient?.gender || undefined, patient_weight: vitals.weight || undefined, vitals: cleanVitals(),
    });
    setDsBusy(null);
    if (r.ok) { const d = await r.json(); setDs(d); if (!d.available) setErr("Clinical Synthesis decision support is unavailable right now."); }
    else setErr("Couldn't reach decision support.");
  }

  async function confirmDx() {
    if (!primaryDx) return;
    setSub("ix"); setDsBusy("Confirming diagnosis…");
    const r = await apiPost("/synthesis/confirm", {
      chief_complaints: enc.complaints.map((c) => c.text), confirmed_diagnoses: [primaryDx.diagnosis],
      age: ageFrom(patient?.dob ?? null).replace("y", ""), gender: patient?.gender || undefined, patient_weight: vitals.weight || undefined, vitals: cleanVitals(),
    });
    setDsBusy(null);
    if (r.ok) { const c = await r.json(); setDs((prev) => prev ? { ...prev, investigations: c.investigations?.length ? c.investigations : prev.investigations, treatment: c.treatment?.length ? c.treatment : prev.treatment } : prev); }
  }

  function addTx(d: TxDrug) { setRx((prev) => prev.some((x) => x.drug === d.drug) ? prev : [...prev, { drug: d.drug, brand: d.brands[0] || "", dose: d.dose, frequency: d.frequency, duration: d.duration, route: d.route }]); }

  function composeNote() {
    const complaintsTxt = enc.complaints.map((c) => `${c.text}${c.duration ? ` (${c.duration})` : ""}`).join("; ");
    const subjective = [complaintsTxt && `Chief complaints: ${complaintsTxt}.`, enc.hpi, enc.past_history && `Past history: ${enc.past_history}`, enc.allergies && `Allergies: ${enc.allergies}`, enc.medications && `Current meds: ${enc.medications}`].filter(Boolean).join("\n");
    const objective = [enc.general_exam && `General: ${enc.general_exam}`, enc.systemic_exam && `Systemic: ${enc.systemic_exam}`].filter(Boolean).join("\n");
    const ixTxt = [...selIx].map((i) => ds?.investigations[i]?.investigation).filter(Boolean).join("; ");
    const plan = [ixTxt && `Investigations: ${ixTxt}.`, rx.length && `Rx: ${rx.map((r) => `${r.drug} ${r.dose} ${r.frequency} ${r.duration}`.trim()).join("; ")}.`].filter(Boolean).join("\n");
    return {
      soap: { subjective, objective, assessment: primaryDx ? `${primaryDx.diagnosis}${primaryDx.icd10 ? ` (${primaryDx.icd10})` : ""}` : "", plan },
      entities: { symptoms: enc.complaints.map((c) => c.text), allergies: enc.allergies ? [enc.allergies] : [], diagnoses: primaryDx ? [primaryDx.diagnosis] : [] },
      prescription: rx.map((r) => ({ brand: r.brand || r.drug, generic: r.drug, strength: r.dose || null, form: null, dose: r.dose, frequency: r.frequency, duration: r.duration, instructions: r.route ? `Route: ${r.route}` : "" })),
      vitals: cleanVitals(),
      wizard: { step, section, enc, vitals, ds, sub, primaryDx, selIx: [...selIx], rx },
    };
  }

  async function loadDraft(vid: string) {
    const r = await apiGet(`/visits/${vid}`);
    if (!r.ok) return;
    const v = await r.json();
    setDraftVisitId(vid);
    if (v.patient_id) await attach(v.patient_id);
    const w = v.note?.wizard;
    if (w && Object.keys(w).length) {
      if (w.enc) setEnc(w.enc);
      if (w.vitals) setVitals(w.vitals);
      if (w.ds) setDs(w.ds);
      if (w.primaryDx) setPrimaryDx(w.primaryDx);
      if (Array.isArray(w.selIx)) setSelIx(new Set(w.selIx));
      if (Array.isArray(w.rx)) setRx(w.rx);
      if (w.section) setSection(w.section);
      if (w.sub) setSub(w.sub);
      if (w.step) setStep(w.step);
    }
  }

  async function saveDraft() {
    if (!patient) { setErr("No patient attached."); return; }
    setSaving(true); setErr(null); setDraftSaved(false);
    const r = await apiPost("/scribe/save", { patient_id: patient.id, visit_id: draftVisitId ?? undefined, status: "in_progress", ...composeNote() });
    setSaving(false);
    if (!r.ok) { setErr("Couldn't save the draft."); return; }
    setDraftVisitId((await r.json()).visit_id); setDraftSaved(true);
  }

  async function save() {
    if (!patient) { setErr("No patient attached."); return; }
    if (!attested) { setErr("Please attest the note to sign it."); return; }
    setSaving(true); setErr(null);
    const r = await apiPost("/scribe/save", {
      patient_id: patient.id, visit_id: draftVisitId ?? undefined, status: "completed", attested: true,
      consent_given: true, consent_method: "verbal", ...composeNote(),
    });
    setSaving(false);
    if (!r.ok) { setErr(`Save failed (${r.status}).`); return; }
    setSaved((await r.json()).visit_id);
  }

  if (loading) return <main className="p-10 text-sm text-slate-500">Loading…</main>;
  if (!patient) return <PatientPicker onPick={attach} />;

  if (saved) return (
    <main className="mx-auto max-w-2xl px-4 py-16 text-center">
      <div className="glass rounded-2xl p-8">
        <p className="text-lg font-semibold text-slate-900">Consultation signed &amp; saved ✓</p>
        <div className="mt-4 flex justify-center gap-3">
          <Link href={`/visits/${saved}/print`} className="btn-primary">Print / PDF</Link>
          <Link href={`/patients/${patient.id}`} className="rounded-xl border border-slate-200 px-4 py-2.5 text-sm text-slate-600">Patient record</Link>
          <Link href="/consultations" className="rounded-xl border border-slate-200 px-4 py-2.5 text-sm text-slate-600">Consultations</Link>
        </div>
      </div>
    </main>
  );

  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div><h1 className="text-2xl font-semibold tracking-tight text-slate-900">Clinical Consultation</h1>
          <p className="text-sm text-slate-500">Structured clinical note + prescription builder</p></div>
        <div className="flex items-center gap-3 text-sm">
          <button onClick={() => setScribeOpen(true)} className="flex items-center gap-1.5 rounded-xl bg-emerald-600 px-3 py-1.5 font-medium text-white hover:bg-emerald-500">◉ Scribe</button>
          <button onClick={saveDraft} disabled={saving} className="rounded-xl border border-slate-200 px-3 py-1.5 text-slate-600 hover:bg-slate-50 disabled:opacity-40">Save draft</button>
          {draftSaved && <span className="text-xs text-emerald-600">Saved ✓</span>}
          <Link href="/consultations" className="text-slate-500 hover:text-slate-900">Consultations</Link>
        </div>
      </div>

      {scribeOpen && <ScribeOverlay onApply={applyExtract} onClose={() => setScribeOpen(false)} />}

      <Steps step={step} />
      <div className="mb-5 flex items-center justify-between rounded-xl bg-slate-900 px-4 py-2.5 text-sm text-white">
        <span className="font-medium">{patient.uhid && <span className="font-mono">{patient.uhid}</span>} · {patient.name}, {ageFrom(patient.dob)}/{patient.gender || "—"}{patient.phone ? ` · ${patient.phone}` : ""}</span>
        <span className="rounded bg-white/15 px-2 py-0.5 text-[11px]">ABHA not linked</span>
      </div>

      {err && <p className="mb-4 text-sm text-red-600">{err}</p>}

      {step === 1 && (
        <div className="grid gap-4 md:grid-cols-[220px_1fr]">
          <nav className="glass h-fit rounded-2xl p-2">
            <p className="px-2 py-1 text-[10px] font-bold uppercase tracking-wide text-slate-400">Encounter</p>
            {SECTIONS.map((s) => (
              <button key={s.key} onClick={() => setSection(s.key)} className={`flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-sm ${section === s.key ? "bg-blue-50 font-medium text-blue-700" : "text-slate-600 hover:bg-slate-50"}`}>
                <span className={`grid h-4 w-4 shrink-0 place-items-center rounded-full border text-[9px] ${done[s.key] ? "border-emerald-500 bg-emerald-500 text-white" : "border-slate-300 text-transparent"}`}>✓</span>{s.label}
              </button>
            ))}
          </nav>
          <section className="glass rounded-2xl p-5">
            {section === "vitals" && (<div><SectionHead title="Vitals" note="All fields optional" />
              <div className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Field label="Weight (kg)" v={vitals.weight} on={(x) => setVitals({ ...vitals, weight: x })} ph="e.g. 70" />
                <Field label="Height (cm)" v={vitals.height} on={(x) => setVitals({ ...vitals, height: x })} ph="e.g. 170" />
                <div><label className="text-xs text-slate-500">BMI</label><div className="field mt-0.5 w-full bg-slate-50 text-slate-600">{bmi || "auto"}</div></div>
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                <Field label="BP Sys" v={vitals.bp_sys} on={(x) => setVitals({ ...vitals, bp_sys: x })} ph="120" />
                <Field label="BP Dia" v={vitals.bp_dia} on={(x) => setVitals({ ...vitals, bp_dia: x })} ph="80" />
                <Field label="HR (bpm)" v={vitals.hr} on={(x) => setVitals({ ...vitals, hr: x })} ph="60–100" />
                <Field label="SpO₂ (%)" v={vitals.spo2} on={(x) => setVitals({ ...vitals, spo2: x })} ph="95–100" />
                <Field label="Temp (°F)" v={vitals.temp} on={(x) => setVitals({ ...vitals, temp: x })} ph="97–99.5" />
                <Field label="RR (/min)" v={vitals.rr} on={(x) => setVitals({ ...vitals, rr: x })} ph="12–20" />
              </div>
              <Advance onClick={() => setSection("complaints")} label="Continue to Chief Complaints" /></div>)}

            {section === "complaints" && (<div><SectionHead title="Chief Complaints" />
              <div className="mb-3 flex flex-wrap gap-2">
                <input value={cText} onChange={(e) => setCText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addComplaint()} placeholder="Complaint (e.g. sharp right ankle pain)" className="field min-w-[220px] flex-1" />
                <input value={cDur} onChange={(e) => setCDur(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addComplaint()} placeholder="Duration (e.g. 2 days)" className="field w-40" />
                <button onClick={addComplaint} className="rounded-xl bg-blue-600 px-3 py-2 text-sm font-medium text-white">Add</button>
              </div>
              {enc.complaints.length === 0 ? <p className="text-sm text-slate-400">No complaints yet.</p> : (
                <ul className="space-y-1.5">{enc.complaints.map((c, i) => (
                  <li key={i} className="flex items-center justify-between rounded-lg border border-slate-200 px-3 py-2 text-sm">
                    <span className="text-slate-800">{c.text}{c.duration && <span className="text-slate-400"> · {c.duration}</span>}</span>
                    <button onClick={() => setEnc({ ...enc, complaints: enc.complaints.filter((_, idx) => idx !== i) })} className="text-xs text-slate-400 hover:text-red-600">Remove</button>
                  </li>))}</ul>)}
              <Advance onClick={() => setSection("history")} label="Continue to History" /></div>)}

            {section === "history" && (<div><SectionHead title="History" />
              <Area label="History of present illness" v={enc.hpi} on={(x) => setEnc({ ...enc, hpi: x })} />
              <Area label="Past history" v={enc.past_history} on={(x) => setEnc({ ...enc, past_history: x })} />
              <Area label="Allergies" v={enc.allergies} on={(x) => setEnc({ ...enc, allergies: x })} />
              <Area label="Current medications" v={enc.medications} on={(x) => setEnc({ ...enc, medications: x })} />
              <Advance onClick={() => setSection("general")} label="Continue to Examination" /></div>)}

            {section === "general" && (<div><SectionHead title="General Examination" />
              <Area label="General examination findings" v={enc.general_exam} on={(x) => setEnc({ ...enc, general_exam: x })} rows={5} />
              <Advance onClick={() => setSection("systemic")} label="Continue to Systemic Examination" /></div>)}

            {section === "systemic" && (<div><SectionHead title="Systemic Examination" />
              <Area label="Systemic examination findings" v={enc.systemic_exam} on={(x) => setEnc({ ...enc, systemic_exam: x })} rows={5} />
              <div className="mt-4 flex justify-end"><button onClick={goToPrescription} className="btn-primary">Continue to Prescription →</button></div></div>)}
          </section>
        </div>
      )}

      {step === 2 && (
        <div className="space-y-4">
          <div className="flex items-center gap-2 text-xs">
            {(["dx", "ix", "tx"] as const).map((s, i) => (
              <span key={s} className={`rounded-full px-2.5 py-1 font-medium ${sub === s ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-500"}`}>{["1 Diagnosis", "2 Investigations", "3 Treatment"][i]}</span>
            ))}
            {dsBusy && <span className="text-slate-400">{dsBusy}</span>}
          </div>

          {!ds && !dsBusy && <p className="glass rounded-2xl p-6 text-sm text-slate-400">Add at least one chief complaint in Step 1 to generate the differential.</p>}

          {sub === "dx" && ds && (
            <div className="glass rounded-2xl p-5">
              <SectionHead title="Differential diagnosis" note="Clinical Synthesis · ICMR-grounded" />
              {ds.must_not_miss.length > 0 && <div className="mb-3 flex flex-wrap gap-1.5">{ds.must_not_miss.map((m, i) => <span key={i} className="rounded bg-red-600 px-2 py-0.5 text-xs font-medium text-white">Don&apos;t miss: {m.diagnosis}</span>)}</div>}
              <ul className="space-y-2">
                {ds.differential_diagnosis.map((d, i) => (
                  <li key={i} className={`rounded-xl border p-3 ${primaryDx?.diagnosis === d.diagnosis ? "border-blue-400 bg-blue-50" : "border-slate-200"}`}>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs text-slate-400">{i + 1}</span>
                      <span className="text-sm font-medium text-slate-900">{d.diagnosis}</span>
                      {d.icd10 && <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-mono text-slate-500">{d.icd10}</span>}
                      {d.likelihood && <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${likeTag[d.likelihood.toLowerCase()] || likeTag.low}`}>{d.likelihood}</span>}
                      <button onClick={() => setPrimaryDx(d)} className={`ml-auto rounded-lg px-2.5 py-1 text-xs font-medium ${primaryDx?.diagnosis === d.diagnosis ? "bg-blue-600 text-white" : "border border-slate-200 text-blue-600 hover:bg-blue-50"}`}>{primaryDx?.diagnosis === d.diagnosis ? "Primary ✓" : "Set as Primary"}</button>
                    </div>
                    {d.reasoning && <p className="mt-1 text-xs text-slate-500">{d.reasoning}</p>}
                  </li>))}
              </ul>
              <div className="mt-4 flex justify-between">
                <button onClick={() => setStep(1)} className="rounded-xl border border-slate-200 px-4 py-2 text-sm text-slate-600">← Consultation</button>
                <button onClick={confirmDx} disabled={!primaryDx} className="btn-primary disabled:opacity-40">Confirm diagnosis →</button>
              </div>
            </div>
          )}

          {sub === "ix" && ds && (
            <div className="glass rounded-2xl p-5">
              <SectionHead title="Investigations" note={`for ${primaryDx?.diagnosis || "the diagnosis"}`} />
              <div className="mb-2 flex gap-2">
                <button onClick={() => setSelIx(new Set(ds.investigations.map((_, i) => i).filter((i) => ["immediate", "urgent"].includes(ds.investigations[i].urgency.toLowerCase()))))} className="rounded-lg border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600">Add urgent</button>
                <button onClick={() => setSelIx(new Set(ds.investigations.map((_, i) => i)))} className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium text-slate-600">Add all</button>
              </div>
              <ul className="space-y-1.5">
                {ds.investigations.map((iv, i) => (
                  <li key={i} className="flex items-start gap-2 rounded-lg border border-slate-100 p-2.5 text-sm">
                    <input type="checkbox" checked={selIx.has(i)} onChange={(e) => setSelIx((prev) => { const n = new Set(prev); if (e.target.checked) n.add(i); else n.delete(i); return n; })} className="mt-1 h-4 w-4 accent-blue-600" />
                    <span className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${urgTag[iv.urgency.toLowerCase()] || urgTag.routine}`}>{iv.urgency}</span>
                    <span className="text-slate-700"><span className="font-medium">{iv.investigation}</span>{iv.mnm_floor && <span className="ml-1 text-[10px] font-semibold text-red-600">must-not-miss</span>}{iv.rationale && <span className="text-slate-500"> — {iv.rationale}</span>}</span>
                  </li>))}
              </ul>
              <div className="mt-4 flex justify-between">
                <button onClick={() => setSub("dx")} className="rounded-xl border border-slate-200 px-4 py-2 text-sm text-slate-600">← Diagnosis</button>
                <button onClick={() => setSub("tx")} className="btn-primary">Continue to Treatment →</button>
              </div>
            </div>
          )}

          {sub === "tx" && ds && (
            <div className="glass rounded-2xl p-5">
              <SectionHead title="Treatment" note="add to prescription" />
              {ds.treatment.map((t, ti) => (
                <div key={ti} className="mb-3">
                  <ul className="space-y-2">
                    {t.first_line.map((d, di) => {
                      const added = rx.some((x) => x.drug === d.drug);
                      return (
                        <li key={di} className="flex items-start justify-between gap-2 rounded-lg border border-slate-200 p-2.5">
                          <div><span className="text-sm font-medium text-slate-900">{d.drug}</span>
                            <span className="text-xs text-slate-600"> {[d.dose, d.route, d.frequency, d.duration].filter(Boolean).join(" · ")}</span>
                            {d.brands.length > 0 && <div className="text-xs text-slate-400">Brands: {d.brands.join(", ")}</div>}
                            {d.dose_needs_doctor && <p className="mt-1 text-[11px] font-medium text-amber-700">⚠ {d.dose_flag || "Set the dose."}</p>}</div>
                          <button onClick={() => addTx(d)} disabled={added} className={`shrink-0 rounded-lg px-2.5 py-1 text-xs font-medium ${added ? "bg-emerald-100 text-emerald-700" : "bg-blue-600 text-white hover:bg-blue-700"}`}>{added ? "Added ✓" : "+ Add"}</button>
                        </li>);
                    })}
                  </ul>
                  {t.non_pharmacological.length > 0 && <ul className="mt-1.5 list-disc pl-5 text-xs text-slate-500">{t.non_pharmacological.map((x, i) => <li key={i}>{x}</li>)}</ul>}
                </div>))}
              <div className="mt-4 flex justify-between">
                <button onClick={() => setSub("ix")} className="rounded-xl border border-slate-200 px-4 py-2 text-sm text-slate-600">← Investigations</button>
                <button onClick={() => setStep(3)} className="btn-primary">Review &amp; Sign →</button>
              </div>
            </div>
          )}
        </div>
      )}

      {step === 3 && (
        <div className="glass rounded-2xl p-5">
          <SectionHead title="Review & Sign" />
          <Row label="Diagnosis" value={primaryDx ? `${primaryDx.diagnosis}${primaryDx.icd10 ? ` (${primaryDx.icd10})` : ""}` : "—"} />
          <Row label="Chief complaints" value={enc.complaints.map((c) => c.text).join(", ") || "—"} />
          <Row label="Investigations" value={[...selIx].map((i) => ds?.investigations[i]?.investigation).filter(Boolean).join("; ") || "—"} />
          <Row label="Prescription" value={rx.length ? rx.map((r) => `${r.drug} ${r.dose} ${r.frequency}`.trim()).join(" · ") : "—"} />
          <Row label="Vitals" value={Object.entries(cleanVitals()).map(([k, v]) => `${k.toUpperCase()} ${v}`).join("  ·  ") || "—"} />
          <label className="mt-4 flex items-start gap-2.5 rounded-xl border border-slate-200 bg-slate-50/70 p-3">
            <input type="checkbox" checked={attested} onChange={(e) => setAttested(e.target.checked)} className="mt-0.5 h-4 w-4 accent-blue-600" />
            <span className="text-xs text-slate-700"><span className="font-semibold text-slate-900">Physician attestation.</span> I have reviewed this note and its AI-assisted suggestions, corrected them as needed, and approve this record. I remain the responsible clinician.</span>
          </label>
          <div className="mt-4 flex justify-between">
            <button onClick={() => setStep(2)} className="rounded-xl border border-slate-200 px-4 py-2 text-sm text-slate-600">← Prescription</button>
            <button onClick={save} disabled={saving || !attested} className="btn-primary disabled:opacity-40">{saving ? "Saving…" : "Sign & save"}</button>
          </div>
        </div>
      )}
    </main>
  );
}

function PatientPicker({ onPick }: { onPick: (id: string) => void }) {
  const [mode, setMode] = useState<"search" | "register">("search");
  const [q, setQ] = useState(""); const [list, setList] = useState<Patient[]>([]);
  useEffect(() => { if (mode !== "search") return; const t = setTimeout(async () => { const r = await apiGet(`/patients${q.trim() ? `?q=${encodeURIComponent(q.trim())}` : ""}`); if (r.ok) setList((await r.json()).items || []); }, 250); return () => clearTimeout(t); }, [q, mode]);

  return (
    <main className="mx-auto max-w-2xl px-4 py-14">
      <h1 className="mb-1 text-2xl font-semibold tracking-tight text-slate-900">New Consultation</h1>
      <p className="mb-4 text-sm text-slate-500">Find an existing patient or register a new one.</p>
      <div className="mb-4 flex gap-2 text-sm">
        <button onClick={() => setMode("search")} className={`rounded-xl px-3 py-1.5 ${mode === "search" ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-600"}`}>Search patient</button>
        <button onClick={() => setMode("register")} className={`rounded-xl px-3 py-1.5 ${mode === "register" ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-600"}`}>+ New patient</button>
      </div>
      {mode === "search" ? (
        <>
          <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search by name, phone or UHID…" className="field mb-3 w-full" />
          <div className="glass overflow-hidden rounded-2xl">
            {list.length === 0 ? <p className="p-6 text-sm text-slate-400">No patients found.</p> : (
              <ul className="divide-y divide-slate-200/70">{list.map((p) => (
                <li key={p.id}><button onClick={() => onPick(p.id)} className="flex w-full items-center justify-between px-5 py-3.5 text-left hover:bg-slate-50/60">
                  <span><span className="font-medium text-slate-900">{p.name}</span>{p.uhid && <span className="ml-2 font-mono text-xs text-slate-400">{p.uhid}</span>}</span>
                  <span className="text-sm text-slate-500">{[p.gender, p.phone].filter(Boolean).join(" · ")}</span>
                </button></li>))}</ul>)}
          </div>
        </>
      ) : <RegistrationForm onDone={onPick} />}
    </main>
  );
}

function RegistrationForm({ onDone }: { onDone: (id: string) => void }) {
  const [f, setF] = useState({ first: "", last: "", mobile: "", dobMode: "age" as "age" | "dob", years: "", months: "", dob: "", gender: "male", address: "", pincode: "", city: "", state: "" });
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<string | null>(null);
  const up = (k: string, v: string) => setF({ ...f, [k]: v });

  async function submit() {
    if (!f.first.trim()) { setErr("First name is required."); return; }
    if (!/^[6-9]\d{9}$/.test(f.mobile)) { setErr("Enter a valid 10-digit mobile (starts 6–9)."); return; }
    const dob = f.dobMode === "dob" ? (f.dob || null) : (f.years ? `${new Date().getFullYear() - parseInt(f.years)}-01-01` : null);
    setBusy(true); setErr(null);
    const r = await apiPost("/patients", {
      name: `${f.first} ${f.last}`.trim(), phone: f.mobile, gender: f.gender, dob,
      address: f.address || null, pincode: f.pincode || null, city: f.city || null, state: f.state || null,
    });
    setBusy(false);
    if (!r.ok) { setErr(`Registration failed (${r.status}).`); return; }
    onDone((await r.json()).id);
  }

  return (
    <div className="glass rounded-2xl p-5">
      <div className="mb-3 grid grid-cols-2 gap-3">
        <label className="text-xs text-slate-500">First name *<input value={f.first} onChange={(e) => up("first", e.target.value)} className="field mt-0.5 w-full" /></label>
        <label className="text-xs text-slate-500">Last name<input value={f.last} onChange={(e) => up("last", e.target.value)} className="field mt-0.5 w-full" /></label>
      </div>
      <label className="text-xs text-slate-500">Mobile *<input value={f.mobile} onChange={(e) => up("mobile", e.target.value.replace(/\D/g, "").slice(0, 10))} placeholder="10-digit (6–9 start)" className="field mt-0.5 w-full" /></label>
      <div className="mt-3 mb-1 flex items-center justify-between">
        <span className="text-xs text-slate-500">Date of Birth / Age</span>
        <div className="flex overflow-hidden rounded-lg border border-slate-200 text-xs">
          <button onClick={() => up("dobMode", "dob")} className={`px-2 py-1 ${f.dobMode === "dob" ? "bg-slate-900 text-white" : "text-slate-500"}`}>DOB</button>
          <button onClick={() => up("dobMode", "age")} className={`px-2 py-1 ${f.dobMode === "age" ? "bg-slate-900 text-white" : "text-slate-500"}`}>Age</button>
        </div>
      </div>
      {f.dobMode === "age" ? (
        <div className="grid grid-cols-2 gap-3">
          <input value={f.years} onChange={(e) => up("years", e.target.value.replace(/\D/g, "").slice(0, 3))} placeholder="Years" className="field w-full" />
          <input value={f.months} onChange={(e) => up("months", e.target.value.replace(/\D/g, "").slice(0, 2))} placeholder="Months (optional)" className="field w-full" />
        </div>
      ) : <input type="date" value={f.dob} onChange={(e) => up("dob", e.target.value)} className="field w-full" />}
      <div className="mt-3">
        <p className="mb-1 text-xs text-slate-500">Gender</p>
        <div className="flex gap-2">{["male", "female", "other"].map((g) => (
          <button key={g} onClick={() => up("gender", g)} className={`flex-1 rounded-lg px-3 py-2 text-sm capitalize ${f.gender === g ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-600"}`}>{g}</button>))}</div>
      </div>
      <input value={f.address} onChange={(e) => up("address", e.target.value)} placeholder="Street, Area, Landmark" className="field mt-3 w-full" />
      <div className="mt-3 grid grid-cols-3 gap-3">
        <input value={f.pincode} onChange={(e) => up("pincode", e.target.value.replace(/\D/g, "").slice(0, 6))} placeholder="Pincode" className="field w-full" />
        <input value={f.city} onChange={(e) => up("city", e.target.value)} placeholder="City" className="field w-full" />
        <input value={f.state} onChange={(e) => up("state", e.target.value)} placeholder="State" className="field w-full" />
      </div>
      {err && <p className="mt-3 text-sm text-red-600">{err}</p>}
      <button onClick={submit} disabled={busy} className="btn-primary mt-4 w-full disabled:opacity-40">{busy ? "Registering…" : "Register & Start Consultation →"}</button>
    </div>
  );
}

function ScribeOverlay({ onApply, onClose }: { onApply: (ex: Extracted) => void; onClose: () => void }) {
  const [recording, setRecording] = useState(false);
  const [status, setStatus] = useState("Ready");
  const [elapsed, setElapsed] = useState(0);
  const [transcript, setTranscript] = useState("");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const ctxRef = useRef<AudioContext | null>(null);
  const procRef = useRef<ScriptProcessorNode | null>(null);
  const srcRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Float32Array[]>([]);
  const rateRef = useRef(48000);
  const sentRef = useRef(0); const rawRef = useRef(""); const procFlagRef = useRef(false);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    if (timerRef.current) clearInterval(timerRef.current);
    procRef.current?.disconnect(); srcRef.current?.disconnect();
    streamRef.current?.getTracks().forEach((t) => t.stop());
    ctxRef.current?.close().catch(() => {});
  }, []);

  async function start() {
    if (!consent) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true }); streamRef.current = stream;
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new AC(); ctxRef.current = ctx; rateRef.current = ctx.sampleRate;
      const src = ctx.createMediaStreamSource(stream); srcRef.current = src;
      const proc = ctx.createScriptProcessor(4096, 1, 1); procRef.current = proc;
      chunksRef.current = []; sentRef.current = 0; rawRef.current = "";
      proc.onaudioprocess = (e) => chunksRef.current.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      const mute = ctx.createGain(); mute.gain.value = 0; src.connect(proc); proc.connect(mute); mute.connect(ctx.destination);
      setRecording(true); setStatus("Listening…"); setElapsed(0); setErr(null);
      timerRef.current = setInterval(() => setElapsed((e) => e + 1), 1000);
      intervalRef.current = setInterval(() => { void tick(false); }, 15000);
    } catch { setErr("Microphone access denied or unavailable."); }
  }

  async function tick(final: boolean) {
    if (procFlagRef.current) return;
    const all = flatten(chunksRef.current); const newCount = all.length - sentRef.current;
    if (newCount < rateRef.current * (final ? 0.5 : 5) && !final) return;
    procFlagRef.current = true;
    try {
      if (newCount > 0) {
        const slice = new Float32Array(all.subarray(sentRef.current)); sentRef.current = all.length;
        setStatus("Transcribing…");
        const form = new FormData(); form.append("file", encodeWav(slice, rateRef.current), "scribe.wav");
        const r = await apiUpload("/scribe/transcribe", form);
        if (r.ok) { const t = ((await r.json()).transcript || "").trim(); if (t) { rawRef.current = (rawRef.current + " " + t).trim(); setTranscript(rawRef.current); } }
      }
    } finally { procFlagRef.current = false; if (!final) setStatus("Listening…"); }
  }

  async function stopAndApply() {
    if (intervalRef.current) clearInterval(intervalRef.current);
    if (timerRef.current) clearInterval(timerRef.current);
    setRecording(false); setBusy(true); setStatus("Finishing…");
    await tick(true);
    procRef.current?.disconnect(); srcRef.current?.disconnect();
    streamRef.current?.getTracks().forEach((t) => t.stop()); await ctxRef.current?.close().catch(() => {});
    if (rawRef.current.trim().length < 3) { setBusy(false); setErr("No speech was captured."); return; }
    setStatus("Extracting encounter…");
    const r = await apiPost("/scribe/extract", { transcript: rawRef.current });
    setBusy(false);
    if (!r.ok) { setErr("Couldn't process the recording."); return; }
    onApply(await r.json());
  }

  const mm = String(Math.floor(elapsed / 60)).padStart(2, "0"), ss = String(elapsed % 60).padStart(2, "0");
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 px-4" onClick={() => !recording && !busy && onClose()}>
      <div className="glass w-full max-w-lg rounded-2xl p-6" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-base font-semibold text-slate-900">◉ Scribe {recording && <span className="rounded bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700">REC {mm}:{ss}</span>}</h3>
          {!recording && !busy && <button onClick={onClose} className="text-sm text-slate-400 hover:text-slate-700">Close</button>}
        </div>
        {!recording && !busy && (
          <label className="mb-3 flex items-start gap-2.5 rounded-xl bg-amber-50/70 p-3">
            <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} className="mt-0.5 h-4 w-4 accent-amber-600" />
            <span className="text-xs text-slate-700"><span className="font-semibold text-amber-800">Recording consent.</span> The patient has been informed and consents to recording this consultation for documentation.</span>
          </label>
        )}
        <div className="mb-3 h-40 overflow-y-auto rounded-xl border border-slate-200 bg-white/60 p-3 text-sm text-slate-700">
          {transcript || <span className="text-slate-400">Speak naturally — the transcript builds here. Press Stop to auto-fill the encounter.</span>}
        </div>
        {err && <p className="mb-2 text-sm text-red-600">{err}</p>}
        <div className="flex items-center gap-3">
          {!recording
            ? <button onClick={start} disabled={!consent || busy} className="btn-primary disabled:opacity-40">● Record</button>
            : <button onClick={stopAndApply} className="rounded-xl bg-red-600 px-4 py-2.5 text-sm font-medium text-white">■ Stop &amp; fill encounter</button>}
          <span className="text-xs text-slate-500">{busy ? status : recording ? status : consent ? "Ready to record." : "Confirm consent to record."}</span>
        </div>
      </div>
    </div>
  );
}

function Steps({ step }: { step: number }) {
  const items = ["Consultation", "Prescription", "Review & Sign"];
  return (<div className="mb-4 flex flex-wrap items-center gap-2 text-sm">{items.map((label, i) => { const n = i + 1, active = step === n, past = step > n; return (
    <span key={label} className="flex items-center gap-2">
      <span className={`grid h-6 w-6 place-items-center rounded-full text-xs font-semibold ${active ? "bg-blue-600 text-white" : past ? "bg-emerald-500 text-white" : "bg-slate-200 text-slate-500"}`}>{past ? "✓" : n}</span>
      <span className={active ? "font-medium text-slate-900" : "text-slate-400"}>{label}</span>{i < 2 && <span className="mx-1 text-slate-300">›</span>}
    </span>); })}</div>);
}
function SectionHead({ title, note }: { title: string; note?: string }) { return (<div className="mb-3 flex items-center justify-between"><h2 className="text-sm font-semibold text-slate-800">{title}</h2>{note && <span className="text-xs text-slate-400">{note}</span>}</div>); }
function Field({ label, v, on, ph }: { label: string; v: string; on: (x: string) => void; ph?: string }) { return <label className="text-xs text-slate-500">{label}<input value={v} onChange={(e) => on(e.target.value)} placeholder={ph} className="field mt-0.5 w-full" /></label>; }
function Area({ label, v, on, rows = 3 }: { label: string; v: string; on: (x: string) => void; rows?: number }) { return <div className="mb-3"><label className="text-xs font-medium text-slate-500">{label}</label><textarea value={v} onChange={(e) => on(e.target.value)} rows={rows} className="field mt-0.5 w-full" /></div>; }
function Advance({ onClick, label }: { onClick: () => void; label: string }) { return <div className="mt-4 flex justify-end"><button onClick={onClick} className="rounded-xl bg-slate-100 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-200">{label} →</button></div>; }
function Row({ label, value }: { label: string; value: string }) { return <div className="border-b border-slate-100 py-2 last:border-0"><p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{label}</p><p className="whitespace-pre-wrap text-sm text-slate-700">{value}</p></div>; }
