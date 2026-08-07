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
type Extracted = { chief_complaints: Complaint[]; hpi: string; past_history: string; allergies: string; medications: string; general_exam: string; systemic_exam: string; vitals: Record<string, string>; evidence?: Record<string, string> };

type Patient = { id: string; name: string; uhid: string | null; gender: string | null; phone: string | null; dob: string | null; height_cm: number | null; weight_kg: number | null };
type Vitals = { weight: string; height: string; bp_sys: string; bp_dia: string; hr: string; spo2: string; temp: string; rr: string };
type Complaint = { text: string; duration: string; evidence?: string };
type Encounter = { complaints: Complaint[]; hpi: string; past_history: string; allergies: string; medications: string; general_exam: string; systemic_exam: string };
type Ddx = { diagnosis: string; likelihood: string; reasoning: string; icd10: string };
type Investigation = { investigation: string; urgency: string; rationale: string; mnm_floor: boolean };
type TxDrug = { drug: string; dose: string; route: string; frequency: string; duration: string; brands: string[]; dose_needs_doctor: boolean; dose_flag: string };
type Treatment = { diagnosis: string; first_line: TxDrug[]; non_pharmacological: string[] };
type DS = { available: boolean; differential_diagnosis: Ddx[]; must_not_miss: { diagnosis: string }[]; investigations: Investigation[]; treatment: Treatment[] };
type RxItem = { drug: string; brand: string; dose: string; frequency: string; duration: string; route: string };
type StoredRx = { brand?: string; generic?: string; dose?: string; strength?: string; frequency?: string; duration?: string; instructions?: string };
type RedFlag = { finding: string; concern: string; urgency: string; action: string };
type LiveQ = { question: string; severity: string };
type Consider = { translation: string; symptoms: string[]; red_flags: RedFlag[]; questions: LiveQ[] };
type TrendPt = { date: string; value: string };
type Memory = { visit_count: number; problems: { label: string; count: number; first_seen: string; last_seen: string }[]; allergies: string[]; current_medications: string[]; trends: Record<string, TrendPt[]>; since_last: { new_problems?: string[]; new_medications?: string[]; stopped_medications?: string[] } };

type Section = "vitals" | "complaints" | "history" | "general" | "systemic";
const SECTIONS: { key: Section; label: string }[] = [
  { key: "vitals", label: "Vitals" }, { key: "complaints", label: "Chief Complaints" },
  { key: "history", label: "History" }, { key: "general", label: "General Examination" },
  { key: "systemic", label: "Systemic Examination" },
];
const urgTag: Record<string, string> = { immediate: "bg-red-600 text-white", urgent: "bg-amber-500 text-white", routine: "bg-slate-400 text-white" };
const likeTag: Record<string, string> = { high: "bg-red-100 text-red-700", moderate: "bg-amber-100 text-amber-700", medium: "bg-amber-100 text-amber-700", low: "bg-slate-100 text-slate-600" };

function ageFrom(dob: string | null): string { if (!dob) return "—"; const y = new Date(dob).getFullYear(); return y ? `${new Date().getFullYear() - y}y` : "—"; }

// Shows the verbatim words from the transcript that produced an AI-filled field,
// so the doctor can verify the auto-fill at a glance (backend guarantees the quote
// genuinely appears in the transcript).
function EvidenceChip({ q }: { q?: string }) {
  if (!q) return null;
  return (
    <p className="mt-1 flex items-start gap-1 text-[11px] text-slate-400" title="Verbatim from the transcript">
      <span className="italic">&ldquo;{q}&rdquo;</span>
      <span className="ml-1 shrink-0 rounded bg-slate-100 px-1 text-[9px] font-semibold uppercase tracking-wide text-slate-400">heard</span>
    </p>
  );
}

function _num(v: string): number { const s = String(v); return parseFloat(s.includes("/") ? s.split("/")[0] : s); }

function Sparkline({ pts }: { pts: TrendPt[] }) {
  const vals = pts.map((p) => _num(p.value)).filter((n) => !isNaN(n));
  if (vals.length < 2) return null;
  const w = 56, h = 16, min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1, step = w / (vals.length - 1);
  const d = vals.map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - min) / span) * h).toFixed(1)}`).join(" ");
  return <svg width={w} height={h} className="text-blue-500"><polyline points={d} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" /></svg>;
}

const METRIC_LABEL: Record<string, string> = { bp: "BP", hr: "HR", spo2: "SpO₂", temp: "Temp", weight: "Weight", rr: "RR", height: "Ht" };

// Longitudinal memory shown automatically at the top of the consult — the doctor
// sees the patient's story (allergies, problems, meds, trends, what changed) with
// zero clicks. Collapsible, no input required.
function MemoryPanel({ m, open, onToggle }: { m: Memory; open: boolean; onToggle: () => void }) {
  const sl = m.since_last || {};
  const changes = [
    ...(sl.new_problems || []).map((x) => ({ t: `New: ${x}`, c: "bg-amber-100 text-amber-800" })),
    ...(sl.new_medications || []).map((x) => ({ t: `Started: ${x}`, c: "bg-blue-100 text-blue-700" })),
    ...(sl.stopped_medications || []).map((x) => ({ t: `Stopped: ${x}`, c: "bg-slate-100 text-slate-600" })),
  ];
  return (
    <div className="mb-5 rounded-2xl border border-slate-200 bg-white/70 p-4">
      <div className="flex items-center justify-between">
        <p className="text-sm font-semibold text-slate-900">Patient memory <span className="font-normal text-slate-400">· {m.visit_count} prior visit{m.visit_count === 1 ? "" : "s"}</span></p>
        <button onClick={onToggle} className="text-xs text-slate-400 hover:text-slate-700">{open ? "Hide" : "Show"}</button>
      </div>
      {m.allergies.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-xs font-semibold text-red-600">Allergies:</span>
          {m.allergies.map((a, i) => <span key={i} className="rounded bg-red-600 px-2 py-0.5 text-xs font-medium text-white">{a}</span>)}
        </div>
      )}
      {changes.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-xs font-medium text-slate-500">Since last visit:</span>
          {changes.map((c, i) => <span key={i} className={`rounded px-2 py-0.5 text-xs ${c.c}`}>{c.t}</span>)}
        </div>
      )}
      {open && (
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          <div>
            <p className="text-[10px] font-bold uppercase tracking-wide text-slate-400">Active problems</p>
            {m.problems.length ? <ul className="mt-1 space-y-0.5 text-sm text-slate-700">{m.problems.slice(0, 6).map((p, i) => <li key={i}>{p.label}{p.count > 1 && <span className="text-slate-400"> ×{p.count}</span>}</li>)}</ul> : <p className="mt-1 text-sm text-slate-400">—</p>}
          </div>
          <div>
            <p className="text-[10px] font-bold uppercase tracking-wide text-slate-400">Current medications</p>
            {m.current_medications.length ? <ul className="mt-1 space-y-0.5 text-sm text-slate-700">{m.current_medications.slice(0, 6).map((x, i) => <li key={i}>{x}</li>)}</ul> : <p className="mt-1 text-sm text-slate-400">—</p>}
          </div>
          <div>
            <p className="text-[10px] font-bold uppercase tracking-wide text-slate-400">Trends</p>
            <div className="mt-1 space-y-1">
              {Object.entries(m.trends).filter(([, pts]) => pts.length >= 2).slice(0, 4).map(([k, pts]) => (
                <div key={k} className="flex items-center gap-2 text-xs">
                  <span className="w-12 shrink-0 text-slate-500">{METRIC_LABEL[k] || k}</span>
                  <Sparkline pts={pts} />
                  <span className="text-slate-800">{pts[pts.length - 1].value}</span>
                </div>
              ))}
              {Object.values(m.trends).every((p) => p.length < 2) && <p className="text-sm text-slate-400">Not enough data yet</p>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

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
  const [evidence, setEvidence] = useState<Record<string, string>>({});
  const [dsFailed, setDsFailed] = useState<"unavailable" | "error" | null>(null);
  const [gate, setGate] = useState<{ kind: string; label: string }[] | null>(null);
  const [gateReason, setGateReason] = useState("");
  const [memory, setMemory] = useState<Memory | null>(null);
  const [memoryOpen, setMemoryOpen] = useState(true);
  const [liveMode, setLiveMode] = useState(true);           // fresh consults open in live mode (the main flow)
  const [liveConsider, setLiveConsider] = useState<Consider>({ translation: "", symptoms: [], red_flags: [], questions: [] });
  const [lastRx, setLastRx] = useState<{ date: string | null; items: StoredRx[] } | null>(null);

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
    setEvidence(ex.evidence || {});
    setScribeOpen(false); setSection("complaints");
  }

  // Live-mode merge: non-destructive, runs repeatedly as the transcript grows.
  // Latest non-empty AI value fills each field; complaints accumulate by unique text.
  function liveMerge(ex: Extracted) {
    setEnc((e) => {
      const seen = new Set(e.complaints.map((c) => c.text.toLowerCase()));
      const merged = [...e.complaints];
      for (const c of (ex.chief_complaints || [])) if (c.text && !seen.has(c.text.toLowerCase())) merged.push(c);
      return {
        complaints: merged,
        hpi: ex.hpi || e.hpi, past_history: ex.past_history || e.past_history,
        allergies: ex.allergies || e.allergies, medications: ex.medications || e.medications,
        general_exam: ex.general_exam || e.general_exam, systemic_exam: ex.systemic_exam || e.systemic_exam,
      };
    });
    setVitals((v) => {
      const n = { ...v }; const mv = ex.vitals || {};
      if (mv.bp) { const [s, d] = mv.bp.split("/"); if (s) n.bp_sys = s.trim(); if (d) n.bp_dia = d.trim(); }
      for (const k of ["hr", "temp", "spo2", "rr", "weight", "height"] as const) if (mv[k]) (n as Record<string, string>)[k] = mv[k];
      return n;
    });
    setEvidence((prev) => ({ ...prev, ...(ex.evidence || {}) }));
  }

  // Short, history-aware context handed to the live lanes so suggestions reflect
  // the patient's own record — not just today's words.
  const patientContext = useMemo(() => {
    if (!memory) return "";
    return [
      memory.allergies.length ? `Allergies: ${memory.allergies.join(", ")}` : "",
      memory.problems.length ? `Known problems: ${memory.problems.map((p) => p.label).join(", ")}` : "",
      memory.current_medications.length ? `Current meds: ${memory.current_medications.join(", ")}` : "",
    ].filter(Boolean).join(". ");
  }, [memory]);

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
    loadMemory(pid);
    loadLastVisit(pid);
  }

  async function loadMemory(pid: string) {
    const r = await apiGet(`/patients/${pid}/memory`);
    if (!r.ok) return;
    const m: Memory = await r.json();
    if (m.visit_count > 0 || m.allergies.length || m.problems.length) setMemory(m);
  }

  async function loadLastVisit(pid: string) {
    const r = await apiGet(`/patients/${pid}/last-visit`);
    if (!r.ok) return;
    const d = await r.json();
    if (d.found && d.prescription?.length) setLastRx({ date: d.visit_date, items: d.prescription });
  }

  const bmi = useMemo(() => { const w = parseFloat(vitals.weight), h = parseFloat(vitals.height); return w && h ? (w / ((h / 100) ** 2)).toFixed(1) : ""; }, [vitals.weight, vitals.height]);
  const done: Record<Section, boolean> = {
    vitals: !!(vitals.bp_sys || vitals.hr || vitals.temp || vitals.spo2 || vitals.weight),
    complaints: enc.complaints.length > 0, history: !!(enc.hpi || enc.past_history || enc.allergies || enc.medications),
    general: !!enc.general_exam, systemic: !!enc.systemic_exam,
  };

  function addComplaint() { if (!cText.trim()) return; setEnc({ ...enc, complaints: [...enc.complaints, { text: cText.trim(), duration: cDur.trim() }] }); setCText(""); setCDur(""); }
  function cleanVitals(): Record<string, string> { const o: Record<string, string> = {}; if (vitals.bp_sys || vitals.bp_dia) o.bp = `${vitals.bp_sys}/${vitals.bp_dia}`; if (vitals.hr) o.hr = vitals.hr; if (vitals.temp) o.temp = vitals.temp; if (vitals.spo2) o.spo2 = vitals.spo2; if (vitals.rr) o.rr = vitals.rr; return o; }

  // Flag vitals outside a safe range (critical values the doctor should not miss).
  function vitalFlags(): string[] {
    const out: string[] = [];
    const chk = (name: string, val: string, lo: number, hi: number, unit = "") => {
      const n = parseFloat(val); if (isNaN(n)) return;
      if (n < lo || n > hi) out.push(`${name} ${val}${unit} is outside the safe range (${lo}–${hi}${unit})`);
    };
    chk("BP systolic", vitals.bp_sys, 90, 180);
    chk("BP diastolic", vitals.bp_dia, 50, 110);
    chk("Heart rate", vitals.hr, 45, 130, " bpm");
    chk("SpO₂", vitals.spo2, 92, 100, "%");
    chk("Temperature", vitals.temp, 95, 103, "°F");
    chk("Respiratory rate", vitals.rr, 8, 30, "/min");
    return out;
  }

  // Completeness gate: safety issues the physician must acknowledge before signing.
  function computeWarnings(): { kind: string; label: string }[] {
    const w: { kind: string; label: string }[] = [];
    if (ds?.must_not_miss?.length) {
      const floorOrdered = [...selIx].some((i) => ds.investigations[i]?.mnm_floor);
      if (!floorOrdered)
        w.push({ kind: "red_flag", label: `Must-not-miss raised — confirm considered: ${ds.must_not_miss.map((m) => m.diagnosis).join(", ")}` });
    }
    (ds?.investigations || []).forEach((iv, i) => {
      const urgent = ["immediate", "urgent"].includes(iv.urgency.toLowerCase()) || iv.mnm_floor;
      if (urgent && !selIx.has(i)) w.push({ kind: "missing_investigation", label: `${iv.urgency} investigation not ordered: ${iv.investigation}` });
    });
    const allergy = enc.allergies.toLowerCase().trim();
    if (allergy) {
      const tokens = allergy.split(/[,;/]+|\band\b/).map((t) => t.trim()).filter((t) => t.length > 2);
      rx.forEach((r) => {
        const hay = `${r.drug} ${r.brand}`.toLowerCase();
        tokens.forEach((t) => { if (hay.includes(t)) w.push({ kind: "allergy_conflict", label: `Possible allergy conflict: ${r.brand || r.drug} vs noted allergy "${t}"` }); });
      });
    }
    vitalFlags().forEach((label) => w.push({ kind: "vitals", label }));
    return w;
  }

  async function runDecisionSupport() {
    if (enc.complaints.length === 0) return;
    setDsBusy("Analysing with Clinical Synthesis…"); setDsFailed(null);
    const r = await apiPost("/synthesis/decision-support", {
      chief_complaints: enc.complaints.map((c) => c.text), age: ageFrom(patient?.dob ?? null).replace("y", ""),
      gender: patient?.gender || undefined, patient_weight: vitals.weight || undefined, vitals: cleanVitals(),
    });
    setDsBusy(null);
    if (r.ok) { const d = await r.json(); setDs(d); setDsFailed(d.available ? null : "unavailable"); }
    else { setDs(null); setDsFailed("error"); }
  }

  async function goToPrescription() {
    setStep(2); setSub("dx"); setErr(null);
    if (ds || enc.complaints.length === 0) return;
    await runDecisionSupport();
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
  function removeRx(i: number) { setRx((prev) => prev.filter((_, idx) => idx !== i)); }
  function repeatLastRx() {
    if (!lastRx) return;
    setRx((prev) => {
      const seen = new Set(prev.map((x) => x.drug.toLowerCase()));
      const add: RxItem[] = [];
      for (const it of lastRx.items) {
        const drug = (it.generic || it.brand || "").trim();
        if (!drug || seen.has(drug.toLowerCase())) continue;
        const route = (it.instructions || "").replace(/^Route:\s*/i, "").trim();
        add.push({ drug, brand: it.brand || "", dose: it.dose || it.strength || "", frequency: it.frequency || "", duration: it.duration || "", route });
        seen.add(drug.toLowerCase());
      }
      return [...prev, ...add];
    });
  }

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
      clinical_considerations: { red_flags: liveConsider.red_flags },
      follow_up_questions: liveConsider.questions,
      wizard: { step, section, enc, vitals, ds, sub, primaryDx, selIx: [...selIx], rx, liveConsider },
    };
  }

  async function loadDraft(vid: string) {
    const r = await apiGet(`/visits/${vid}`);
    if (!r.ok) return;
    const v = await r.json();
    setDraftVisitId(vid);
    setLiveMode(false);                       // resuming saved work → manual wizard, not live
    if (v.patient_id) await attach(v.patient_id);
    const w = v.note?.wizard;
    if (w && Object.keys(w).length) {
      if (w.enc) setEnc(w.enc);
      if (w.vitals) setVitals(w.vitals);
      if (w.ds) setDs(w.ds);
      if (w.primaryDx) setPrimaryDx(w.primaryDx);
      if (Array.isArray(w.selIx)) setSelIx(new Set(w.selIx));
      if (Array.isArray(w.rx)) setRx(w.rx);
      if (w.liveConsider) setLiveConsider(w.liveConsider);
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
    const warnings = computeWarnings();
    if (warnings.length) { setGate(warnings); setGateReason(""); return; }   // open completeness gate
    await doSave([], "");
  }

  async function doSave(warnings: { kind: string; label: string }[], reason: string) {
    if (!patient) return;
    setSaving(true); setErr(null);
    const sign_off = {
      warnings,
      overrides: reason ? warnings.map((w) => ({ ...w, reason })) : [],
      passed: warnings.length === 0,
    };
    const r = await apiPost("/scribe/save", {
      patient_id: patient.id, visit_id: draftVisitId ?? undefined, status: "completed", attested: true,
      consent_given: true, consent_method: "verbal", sign_off, ...composeNote(),
    });
    setSaving(false); setGate(null);
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
          {step === 1 && !liveMode && <button onClick={() => setLiveMode(true)} className="flex items-center gap-1.5 rounded-xl bg-emerald-600 px-3 py-1.5 font-medium text-white hover:bg-emerald-500">● Live consultation</button>}
          {!liveMode && <button onClick={() => setScribeOpen(true)} className="rounded-xl border border-slate-200 px-3 py-1.5 text-slate-600 hover:bg-slate-50">◉ Scribe</button>}
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

      {step === 1 && memory && <MemoryPanel m={memory} open={memoryOpen} onToggle={() => setMemoryOpen((o) => !o)} />}

      {step === 1 && liveMode && (
        <LiveConsult
          patientContext={patientContext}
          enc={enc} vitals={vitals} bmi={bmi} consider={liveConsider}
          onExtract={liveMerge} onConsider={setLiveConsider}
          onManual={() => setLiveMode(false)}
          onFinish={() => setLiveMode(false)}
        />
      )}

      {step === 1 && !liveMode && (
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
              {vitalFlags().length > 0 && (
                <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3">
                  <p className="text-xs font-semibold text-amber-800">⚠ Vitals outside safe range — please double-check</p>
                  <ul className="mt-1 list-disc pl-5 text-xs text-amber-700">{vitalFlags().map((f, i) => <li key={i}>{f}</li>)}</ul>
                </div>
              )}
              <Advance onClick={() => setSection("complaints")} label="Continue to Chief Complaints" /></div>)}

            {section === "complaints" && (<div><SectionHead title="Chief Complaints" />
              <div className="mb-3 flex flex-wrap gap-2">
                <input value={cText} onChange={(e) => setCText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addComplaint()} placeholder="Complaint (e.g. sharp right ankle pain)" className="field min-w-[220px] flex-1" />
                <input value={cDur} onChange={(e) => setCDur(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addComplaint()} placeholder="Duration (e.g. 2 days)" className="field w-40" />
                <button onClick={addComplaint} className="rounded-xl bg-blue-600 px-3 py-2 text-sm font-medium text-white">Add</button>
              </div>
              {enc.complaints.length === 0 ? <p className="text-sm text-slate-400">No complaints yet.</p> : (
                <ul className="space-y-1.5">{enc.complaints.map((c, i) => (
                  <li key={i} className="rounded-lg border border-slate-200 px-3 py-2 text-sm">
                    <div className="flex items-center justify-between">
                      <span className="text-slate-800">{c.text}{c.duration && <span className="text-slate-400"> · {c.duration}</span>}</span>
                      <button onClick={() => setEnc({ ...enc, complaints: enc.complaints.filter((_, idx) => idx !== i) })} className="text-xs text-slate-400 hover:text-red-600">Remove</button>
                    </div>
                    <EvidenceChip q={c.evidence} />
                  </li>))}</ul>)}
              <Advance onClick={() => setSection("history")} label="Continue to History" /></div>)}

            {section === "history" && (<div><SectionHead title="History" />
              <Area label="History of present illness" v={enc.hpi} on={(x) => setEnc({ ...enc, hpi: x })} />
              <EvidenceChip q={evidence.hpi} />
              <Area label="Past history" v={enc.past_history} on={(x) => setEnc({ ...enc, past_history: x })} />
              <EvidenceChip q={evidence.past_history} />
              <Area label="Allergies" v={enc.allergies} on={(x) => setEnc({ ...enc, allergies: x })} />
              <EvidenceChip q={evidence.allergies} />
              <Area label="Current medications" v={enc.medications} on={(x) => setEnc({ ...enc, medications: x })} />
              <EvidenceChip q={evidence.medications} />
              <Advance onClick={() => setSection("general")} label="Continue to Examination" /></div>)}

            {section === "general" && (<div><SectionHead title="General Examination" />
              <Area label="General examination findings" v={enc.general_exam} on={(x) => setEnc({ ...enc, general_exam: x })} rows={5} />
              <EvidenceChip q={evidence.general_exam} />
              <Advance onClick={() => setSection("systemic")} label="Continue to Systemic Examination" /></div>)}

            {section === "systemic" && (<div><SectionHead title="Systemic Examination" />
              <Area label="Systemic examination findings" v={enc.systemic_exam} on={(x) => setEnc({ ...enc, systemic_exam: x })} rows={5} />
              <EvidenceChip q={evidence.systemic_exam} />
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

          {lastRx && rx.length === 0 && (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-blue-200 bg-blue-50 px-4 py-2.5 text-sm">
              <span className="text-blue-800">Returning patient{lastRx.date ? ` · last visit ${new Date(lastRx.date).toLocaleDateString()}` : ""} — repeat the previous prescription?</span>
              <button onClick={repeatLastRx} className="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700">↺ Same as last visit</button>
            </div>
          )}

          {dsFailed && !dsBusy && (
            <div className="rounded-2xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-800">
              <p className="font-semibold">Decision support is {dsFailed === "unavailable" ? "unavailable right now" : "unreachable"}.</p>
              <p className="mt-1 text-xs text-amber-700">
                No differential, investigations, or treatment suggestions were generated. This does <strong>not</strong> mean there is nothing to consider — use your own clinical judgement, or{" "}
                <button onClick={runDecisionSupport} className="underline">retry</button>. You can still complete and sign the note.
              </p>
            </div>
          )}

          {!ds && !dsBusy && !dsFailed && <p className="glass rounded-2xl p-6 text-sm text-slate-400">Add at least one chief complaint in Step 1 to generate the differential.</p>}

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

          {rx.length > 0 && (
            <div className="glass rounded-2xl p-4">
              <SectionHead title={`Current prescription (${rx.length})`} note="carried forward or added — edit freely" />
              <ul className="space-y-1.5">
                {rx.map((r, i) => (
                  <li key={i} className="flex items-center justify-between rounded-lg border border-slate-200 px-3 py-2 text-sm">
                    <span className="text-slate-800"><span className="font-medium">{r.brand || r.drug}</span>{r.drug && r.brand && <span className="text-slate-400"> ({r.drug})</span>}<span className="text-slate-500"> {[r.dose, r.frequency, r.duration, r.route].filter(Boolean).join(" · ")}</span></span>
                    <button onClick={() => removeRx(i)} className="text-xs text-slate-400 hover:text-red-600">Remove</button>
                  </li>
                ))}
              </ul>
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

      {gate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-lg rounded-2xl bg-white p-5 shadow-xl">
            <p className="text-base font-semibold text-slate-900">Before you sign — {gate.length} item{gate.length > 1 ? "s" : ""} to confirm</p>
            <p className="mt-1 text-xs text-slate-500">These are safety checks, not blockers. Resolve them, or record why you&apos;re signing anyway — your reason is saved to the record.</p>
            <ul className="mt-3 space-y-1.5">
              {gate.map((w, i) => (
                <li key={i} className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  <span className="mt-0.5 shrink-0 rounded bg-amber-200 px-1 text-[9px] font-bold uppercase text-amber-900">{w.kind.replace(/_/g, " ")}</span>
                  <span>{w.label}</span>
                </li>
              ))}
            </ul>
            <textarea value={gateReason} onChange={(e) => setGateReason(e.target.value)} rows={2} placeholder="Reason for signing despite the above (required to override)…" className="field mt-3 w-full" />
            <div className="mt-4 flex justify-end gap-2">
              <button onClick={() => setGate(null)} className="rounded-xl border border-slate-200 px-4 py-2 text-sm text-slate-600">Go back &amp; fix</button>
              <button onClick={() => doSave(gate, gateReason)} disabled={saving || !gateReason.trim()} className="rounded-xl bg-amber-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-40">{saving ? "Saving…" : "Override &amp; sign"}</button>
            </div>
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

const URG_RF: Record<string, string> = { emergency: "border-red-300 bg-red-50 text-red-800", urgent: "border-amber-300 bg-amber-50 text-amber-800", routine: "border-slate-200 bg-slate-50 text-slate-700" };
const SEV_Q: Record<string, string> = { high: "bg-red-100 text-red-700", moderate: "bg-amber-100 text-amber-700", low: "bg-slate-100 text-slate-600" };

// Bottom voice waveform — bars driven by live mic level.
function Waveform({ level, active }: { level: number; active: boolean }) {
  const bars = 28;
  return (
    <div className="flex h-10 items-center justify-center gap-[3px]">
      {Array.from({ length: bars }).map((_, i) => {
        const phase = Math.sin((i / bars) * Math.PI);          // taller in the middle
        const h = active ? Math.max(3, phase * level * 38 + Math.random() * 6) : 3;
        return <span key={i} className={`w-[3px] rounded-full ${active ? "bg-emerald-500" : "bg-slate-300"}`} style={{ height: `${h}px`, transition: "height 120ms ease" }} />;
      })}
    </div>
  );
}

// The primary consultation experience: doctor talks, the note fills itself on the
// left, and live assistance (follow-ups + red flags) refreshes on the right.
function LiveConsult({ patientContext, enc, vitals, bmi, consider, onExtract, onConsider, onManual, onFinish }: {
  patientContext: string; enc: Encounter; vitals: Vitals; bmi: string; consider: Consider;
  onExtract: (ex: Extracted) => void; onConsider: (c: Consider) => void; onManual: () => void; onFinish: () => void;
}) {
  const [recording, setRecording] = useState(false);
  const [consent, setConsent] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [status, setStatus] = useState("Ready when you are");
  const [transcript, setTranscript] = useState("");
  const [level, setLevel] = useState(0);
  const [err, setErr] = useState<string | null>(null);

  const ctxRef = useRef<AudioContext | null>(null);
  const procRef = useRef<ScriptProcessorNode | null>(null);
  const srcRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Float32Array[]>([]);
  const rateRef = useRef(48000);
  const sentRef = useRef(0); const rawRef = useRef(""); const procFlagRef = useRef(false);
  const analyzedAtRef = useRef(0); const analyzingRef = useRef(false);
  const transcribeRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const levelRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const cleanup = () => {
    [transcribeRef, timerRef, levelRef].forEach((r) => { if (r.current) clearInterval(r.current); });
    procRef.current?.disconnect(); srcRef.current?.disconnect(); analyserRef.current?.disconnect();
    streamRef.current?.getTracks().forEach((t) => t.stop());
    ctxRef.current?.close().catch(() => {});
  };
  useEffect(() => cleanup, []);

  async function start() {
    if (!consent) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true }); streamRef.current = stream;
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new AC(); ctxRef.current = ctx; rateRef.current = ctx.sampleRate;
      const src = ctx.createMediaStreamSource(stream); srcRef.current = src;
      const proc = ctx.createScriptProcessor(4096, 1, 1); procRef.current = proc;
      const analyser = ctx.createAnalyser(); analyser.fftSize = 256; analyserRef.current = analyser; src.connect(analyser);
      chunksRef.current = []; sentRef.current = 0; rawRef.current = ""; analyzedAtRef.current = 0;
      proc.onaudioprocess = (e) => chunksRef.current.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      const mute = ctx.createGain(); mute.gain.value = 0; src.connect(proc); proc.connect(mute); mute.connect(ctx.destination);
      setRecording(true); setStatus("Listening…"); setElapsed(0); setErr(null);
      timerRef.current = setInterval(() => setElapsed((e) => e + 1), 1000);
      transcribeRef.current = setInterval(() => { void tick(false); }, 12000);
      const buf = new Uint8Array(analyser.frequencyBinCount);
      levelRef.current = setInterval(() => {
        analyser.getByteFrequencyData(buf);
        setLevel(Math.min(1, (buf.reduce((a, b) => a + b, 0) / buf.length) / 90));
      }, 120);
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
        const form = new FormData(); form.append("file", encodeWav(slice, rateRef.current), "scribe.wav");
        const r = await apiUpload("/scribe/transcribe", form);
        if (r.ok) { const t = ((await r.json()).transcript || "").trim(); if (t) { rawRef.current = (rawRef.current + " " + t).trim(); setTranscript(rawRef.current); } }
      }
    } finally { procFlagRef.current = false; }
    // Refresh the note + assistant once enough new speech has accumulated.
    if (final || rawRef.current.length - analyzedAtRef.current >= 40) void analyze();
  }

  async function analyze() {
    if (analyzingRef.current || rawRef.current.trim().length < 3) return;
    analyzingRef.current = true; analyzedAtRef.current = rawRef.current.length; setStatus("Updating note…");
    try {
      const [ex, lv] = await Promise.all([
        apiPost("/scribe/extract", { transcript: rawRef.current, patient_context: patientContext }),
        apiPost("/scribe/live", { transcript: rawRef.current, patient_context: patientContext }),
      ]);
      if (ex.ok) onExtract(await ex.json());
      if (lv.ok) onConsider(await lv.json());
    } finally { analyzingRef.current = false; if (recording) setStatus("Listening…"); }
  }

  async function finish() {
    setStatus("Finishing…");
    [transcribeRef, timerRef, levelRef].forEach((r) => { if (r.current) clearInterval(r.current); });
    setRecording(false);
    await tick(true);
    await analyze();
    cleanup();
    onFinish();
  }

  const mm = String(Math.floor(elapsed / 60)).padStart(2, "0"), ss = String(elapsed % 60).padStart(2, "0");
  const vSummary = [vitals.bp_sys && `BP ${vitals.bp_sys}/${vitals.bp_dia}`, vitals.hr && `HR ${vitals.hr}`, vitals.spo2 && `SpO₂ ${vitals.spo2}`, vitals.temp && `Temp ${vitals.temp}`, bmi && `BMI ${bmi}`].filter(Boolean).join("  ·  ");

  return (
    <div className="space-y-4">
      {!recording && elapsed === 0 ? (
        <div className="glass rounded-2xl p-6 text-center">
          <p className="text-lg font-semibold text-slate-900">Start the live consultation</p>
          <p className="mt-1 text-sm text-slate-500">Just talk to your patient. The note fills itself on the left; suggestions appear on the right.</p>
          <label className="mx-auto mt-4 flex max-w-md items-start gap-2.5 rounded-xl bg-amber-50/70 p-3 text-left">
            <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} className="mt-0.5 h-4 w-4 accent-amber-600" />
            <span className="text-xs text-slate-700"><span className="font-semibold text-amber-800">Recording consent.</span> The patient has been informed and consents to recording this consultation for documentation.</span>
          </label>
          {err && <p className="mt-2 text-sm text-red-600">{err}</p>}
          <div className="mt-4 flex items-center justify-center gap-3">
            <button onClick={start} disabled={!consent} className="btn-primary disabled:opacity-40">● Start live consultation</button>
            <button onClick={onManual} className="rounded-xl border border-slate-200 px-4 py-2.5 text-sm text-slate-600">Type manually instead</button>
          </div>
        </div>
      ) : (
        <>
          <div className="grid gap-4 lg:grid-cols-[1fr_360px]">
            {/* LEFT — the note, filling itself */}
            <section className="glass rounded-2xl p-5">
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-sm font-semibold text-slate-800">Consultation note <span className="font-normal text-slate-400">· filling live</span></h2>
                <span className="text-xs text-slate-400">{status}</span>
              </div>
              <div className="space-y-3 text-sm">
                <LiveField label="Vitals" value={vSummary} />
                <LiveField label="Chief complaints" value={enc.complaints.map((c) => `${c.text}${c.duration ? ` (${c.duration})` : ""}`).join("; ")} />
                <LiveField label="History of present illness" value={enc.hpi} />
                <LiveField label="Past history" value={enc.past_history} />
                <LiveField label="Allergies" value={enc.allergies} danger />
                <LiveField label="Current medications" value={enc.medications} />
                <LiveField label="Examination" value={[enc.general_exam, enc.systemic_exam].filter(Boolean).join(" · ")} />
              </div>
            </section>

            {/* RIGHT — live assistant */}
            <aside className="glass h-fit rounded-2xl p-4">
              <h2 className="text-sm font-semibold text-slate-800">Live assistance <span className="font-normal text-slate-400">· physician-review-only</span></h2>
              {consider.red_flags.length > 0 && (
                <div className="mt-3">
                  <p className="text-[10px] font-bold uppercase tracking-wide text-red-500">Red flags to consider</p>
                  <ul className="mt-1 space-y-1.5">
                    {consider.red_flags.map((rf, i) => (
                      <li key={i} className={`rounded-lg border p-2 text-xs ${URG_RF[rf.urgency?.toLowerCase()] || URG_RF.routine}`}>
                        <span className="font-semibold">{rf.finding}</span>{rf.concern && <span> — {rf.concern}</span>}{rf.action && <div className="mt-0.5 font-medium">→ {rf.action}</div>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <div className="mt-3">
                <p className="text-[10px] font-bold uppercase tracking-wide text-slate-400">Suggested questions</p>
                {consider.questions.length ? (
                  <ul className="mt-1 space-y-1">
                    {consider.questions.map((q, i) => (
                      <li key={i} className="flex items-start gap-1.5 text-xs text-slate-700">
                        <span className={`mt-0.5 shrink-0 rounded px-1 text-[9px] font-bold uppercase ${SEV_Q[q.severity] || SEV_Q.low}`}>{q.severity}</span>{q.question}
                      </li>
                    ))}
                  </ul>
                ) : <p className="mt-1 text-xs text-slate-400">Listening for context…</p>}
              </div>
              {consider.symptoms.length > 0 && (
                <div className="mt-3">
                  <p className="text-[10px] font-bold uppercase tracking-wide text-slate-400">Heard so far</p>
                  <div className="mt-1 flex flex-wrap gap-1">{consider.symptoms.map((s, i) => <span key={i} className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">{s}</span>)}</div>
                </div>
              )}
            </aside>
          </div>

          {/* BOTTOM — waveform + controls */}
          <div className="glass sticky bottom-3 z-10 flex flex-wrap items-center justify-between gap-3 rounded-2xl px-5 py-3">
            <div className="flex items-center gap-3">
              <span className={`flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs font-medium ${recording ? "bg-red-100 text-red-700" : "bg-slate-100 text-slate-500"}`}>{recording ? "● REC" : "❚❚ Paused"} {mm}:{ss}</span>
              <button onClick={onManual} className="text-xs text-slate-400 hover:text-slate-700">Manual entry</button>
            </div>
            <Waveform level={level} active={recording} />
            <div className="flex items-center gap-2">
              {recording
                ? <button onClick={finish} className="rounded-xl bg-red-600 px-4 py-2 text-sm font-medium text-white">■ Finish &amp; review</button>
                : <>
                    <button onClick={start} disabled={!consent} className="btn-primary disabled:opacity-40">● Resume</button>
                    <button onClick={onFinish} className="rounded-xl border border-slate-200 px-4 py-2 text-sm text-slate-600">Review →</button>
                  </>}
            </div>
          </div>
          {err && <p className="text-sm text-red-600">{err}</p>}
        </>
      )}
    </div>
  );
}

function LiveField({ label, value, danger }: { label: string; value: string; danger?: boolean }) {
  return (
    <div className="border-b border-slate-100 pb-2 last:border-0">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{label}</p>
      {value ? <p className={`whitespace-pre-wrap text-sm ${danger ? "font-medium text-red-700" : "text-slate-700"}`}>{value}</p>
             : <p className="text-sm italic text-slate-300">—</p>}
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
