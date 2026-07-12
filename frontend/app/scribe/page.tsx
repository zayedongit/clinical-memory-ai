"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { supabase } from "../../lib/supabaseClient";
import { apiGet, apiPost, apiUpload } from "../../lib/api";

type Soap = { subjective: string; objective: string; assessment: string; plan: string };
type Entities = { symptoms: string[]; medications: string[]; allergies: string[]; diagnoses: string[]; follow_up: string[] };
type Turn = { speaker: "doctor" | "patient"; text: string };
type FollowUp = { question: string; concern: string; likelihood_pct: number; severity: string };
type Patient = { id: string; name: string };

function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buf);
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  str(36, "data"); view.setUint32(40, samples.length * 2, true);
  let o = 44;
  for (let i = 0; i < samples.length; i++) { const v = Math.max(-1, Math.min(1, samples[i])); view.setInt16(o, v < 0 ? v * 0x8000 : v * 0x7fff, true); o += 2; }
  return new Blob([view], { type: "audio/wav" });
}

const sevColor: Record<string, string> = {
  high: "bg-red-100 text-red-700", moderate: "bg-amber-100 text-amber-700", low: "bg-slate-100 text-slate-600",
};

export default function ScribePage() {
  const router = useRouter();
  const [recording, setRecording] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [dialogue, setDialogue] = useState<Turn[]>([]);
  const [soap, setSoap] = useState<Soap | null>(null);
  const [entities, setEntities] = useState<Entities | null>(null);
  const [followUps, setFollowUps] = useState<FollowUp[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showSave, setShowSave] = useState(false);
  const [saved, setSaved] = useState<{ visit_id: string; patient_id: string } | null>(null);

  const ctxRef = useRef<AudioContext | null>(null);
  const procRef = useRef<ScriptProcessorNode | null>(null);
  const srcRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Float32Array[]>([]);
  const rateRef = useRef(48000);

  useEffect(() => { (async () => { const { data } = await supabase.auth.getSession(); if (!data.session) router.push("/login"); })(); }, [router]);

  async function startRecording() {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new AC(); ctxRef.current = ctx; rateRef.current = ctx.sampleRate;
      const src = ctx.createMediaStreamSource(stream); srcRef.current = src;
      const proc = ctx.createScriptProcessor(4096, 1, 1); procRef.current = proc; chunksRef.current = [];
      proc.onaudioprocess = (e) => chunksRef.current.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      const mute = ctx.createGain(); mute.gain.value = 0;
      src.connect(proc); proc.connect(mute); mute.connect(ctx.destination);
      setRecording(true);
    } catch { setError("Microphone access denied or unavailable."); }
  }

  async function stopRecording() {
    setRecording(false);
    procRef.current?.disconnect(); srcRef.current?.disconnect();
    streamRef.current?.getTracks().forEach((t) => t.stop()); await ctxRef.current?.close();
    const chunks = chunksRef.current; const len = chunks.reduce((a, c) => a + c.length, 0);
    if (len === 0) { setError("No audio captured."); return; }
    const merged = new Float32Array(len); let off = 0; for (const c of chunks) { merged.set(c, off); off += c.length; }
    await transcribe(encodeWav(merged, rateRef.current));
  }

  async function transcribe(blob: Blob) {
    setBusy("Transcribing…"); setError(null);
    const form = new FormData(); form.append("file", blob, "consultation.wav");
    const r = await apiUpload("/scribe/transcribe", form); setBusy(null);
    if (!r.ok) { setError(`Transcription failed (${r.status}). ${await r.text()}`); return; }
    setTranscript((await r.json()).transcript || "");
  }

  async function analyze() {
    setBusy("Analysing conversation…"); setError(null); setSaved(null);
    const r = await apiPost("/scribe/soap", { transcript }); setBusy(null);
    if (!r.ok) { setError(`Analysis failed (${r.status}). ${await r.text()}`); return; }
    const d = await r.json();
    setDialogue(d.dialogue || []); setSoap(d.soap); setEntities(d.entities); setFollowUps(d.follow_up_questions || []);
  }

  if (saved) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-10">
        <div className="glass rounded-2xl p-8 text-center">
          <p className="text-lg font-semibold text-slate-900">Visit saved ✓</p>
          <p className="mt-1 text-sm text-slate-500">The reviewed note is now in the patient&apos;s memory log.</p>
          <div className="mt-4 flex justify-center gap-3">
            <Link href={`/patients/${saved.patient_id}`} className="btn-primary">View patient record</Link>
            <button onClick={() => { setSaved(null); setTranscript(""); setSoap(null); setDialogue([]); setFollowUps([]); setEntities(null); }}
              className="rounded-xl border border-slate-200 px-4 py-2.5 text-sm text-slate-600">New consultation</button>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">New Consultation</h1>
          <p className="text-sm text-slate-500">Records the doctor–patient conversation → dialogue, SOAP, follow-ups. Physician reviews everything.</p>
        </div>
        <Link href="/patients" className="text-sm text-slate-500 transition hover:text-slate-900">← Patients</Link>
      </header>

      <div className="glass mb-6 flex items-center gap-4 rounded-2xl p-4">
        {!recording
          ? <button onClick={startRecording} disabled={!!busy} className="btn-primary">● Record</button>
          : <button onClick={stopRecording} className="rounded-xl bg-red-600 px-4 py-2.5 text-sm font-medium text-white">■ Stop</button>}
        <span className="text-xs text-slate-500">{recording ? "Recording…" : busy ? busy : "Keep it short (~30s) for now."}</span>
      </div>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      {(transcript || busy === "Transcribing…") && (
        <section className="mb-6">
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Transcript <span className="font-normal text-slate-400">(editable)</span></h2>
          <textarea value={transcript} onChange={(e) => setTranscript(e.target.value)} rows={4} className="field w-full" placeholder="Transcript…" />
          <button onClick={analyze} disabled={!!busy || transcript.trim().length < 3} className="btn-primary mt-3">Analyse consultation</button>
        </section>
      )}

      {dialogue.length > 0 && (
        <section className="mb-6">
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Conversation</h2>
          <div className="glass space-y-2 rounded-2xl p-4">
            {dialogue.map((t, i) => (
              <div key={i} className={t.speaker === "doctor" ? "text-left" : "text-left"}>
                <span className={`mr-2 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${t.speaker === "doctor" ? "bg-blue-100 text-blue-700" : "bg-emerald-100 text-emerald-700"}`}>{t.speaker}</span>
                <span className="text-sm text-slate-700">{t.text}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {soap && (
        <section className="mb-6 space-y-4">
          <h2 className="text-sm font-semibold text-slate-700">SOAP note <span className="font-normal text-slate-400">(AI draft — review &amp; edit)</span></h2>
          {(["subjective", "objective", "assessment", "plan"] as const).map((k) => (
            <div key={k}>
              <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500">{k}</label>
              <textarea value={soap[k]} onChange={(e) => setSoap({ ...soap, [k]: e.target.value })} rows={k === "plan" || k === "subjective" ? 3 : 2} className="field w-full" />
            </div>
          ))}
          {entities && (
            <div className="glass rounded-2xl p-4">
              <p className="mb-2 text-xs font-semibold text-slate-700">Extracted entities</p>
              {(Object.keys(entities) as (keyof Entities)[]).map((k) => entities[k].length > 0 ? (
                <div key={k} className="mb-1.5"><span className="text-xs font-medium text-slate-500">{k.replace("_", " ")}: </span>
                  {entities[k].map((v, i) => <span key={i} className="mr-1 inline-block rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600">{v}</span>)}
                </div>) : null)}
            </div>
          )}
        </section>
      )}

      {followUps.length > 0 && (
        <section className="mb-6">
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Suggested follow-up questions <span className="font-normal text-slate-400">(you decide whether to ask)</span></h2>
          <div className="space-y-2">
            {followUps.map((f, i) => (
              <div key={i} className="glass rounded-2xl p-4">
                <div className="flex items-start justify-between gap-3">
                  <p className="text-sm font-medium text-slate-900">{f.question}</p>
                  <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${sevColor[f.severity] || sevColor.low}`}>{f.severity}</span>
                </div>
                {f.concern && <p className="mt-1 text-xs text-slate-500">{f.concern}</p>}
                <div className="mt-2 flex items-center gap-2">
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
                    <div className="h-full rounded-full bg-blue-500" style={{ width: `${f.likelihood_pct}%` }} />
                  </div>
                  <span className="text-[11px] text-slate-400">{f.likelihood_pct}% relevance</span>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {soap && (
        <div className="flex justify-end">
          <button onClick={() => setShowSave(true)} className="btn-primary">Save visit</button>
        </div>
      )}

      {showSave && soap && (
        <SaveModal
          onClose={() => setShowSave(false)}
          onSaved={(res) => { setShowSave(false); setSaved(res); }}
          payload={{ transcript, dialogue, soap, entities: entities ?? undefined, follow_up_questions: followUps }}
        />
      )}
    </main>
  );
}

function SaveModal({ onClose, onSaved, payload }: {
  onClose: () => void;
  onSaved: (r: { visit_id: string; patient_id: string }) => void;
  payload: Record<string, unknown>;
}) {
  const [tab, setTab] = useState<"existing" | "new">("existing");
  const [patients, setPatients] = useState<Patient[]>([]);
  const [q, setQ] = useState("");
  const [np, setNp] = useState({ name: "", age: "", gender: "", height_cm: "", weight_kg: "", phone: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { (async () => { const r = await apiGet("/patients"); if (r.ok) setPatients((await r.json()).items || []); })(); }, []);
  const filtered = useMemo(() => patients.filter((p) => p.name.toLowerCase().includes(q.toLowerCase())), [patients, q]);

  async function save(body: Record<string, unknown>) {
    setBusy(true); setErr(null);
    const r = await apiPost("/scribe/save", { ...payload, ...body });
    setBusy(false);
    if (!r.ok) { setErr(`Save failed (${r.status}).`); return; }
    onSaved(await r.json());
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 px-4" onClick={onClose}>
      <div className="glass w-full max-w-md rounded-2xl p-6" onClick={(e) => e.stopPropagation()}>
        <h3 className="mb-3 text-base font-semibold text-slate-900">Save this visit to…</h3>
        <div className="mb-4 flex gap-2">
          <button onClick={() => setTab("existing")} className={`flex-1 rounded-lg px-3 py-2 text-sm ${tab === "existing" ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600"}`}>Existing patient</button>
          <button onClick={() => setTab("new")} className={`flex-1 rounded-lg px-3 py-2 text-sm ${tab === "new" ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600"}`}>New patient</button>
        </div>

        {tab === "existing" ? (
          <div>
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search patients…" className="field mb-2 w-full" />
            <div className="max-h-56 overflow-y-auto rounded-lg border border-slate-200">
              {filtered.length === 0 ? <p className="p-3 text-sm text-slate-400">No patients.</p> :
                filtered.map((p) => (
                  <button key={p.id} disabled={busy} onClick={() => save({ patient_id: p.id })}
                    className="block w-full px-3 py-2.5 text-left text-sm text-slate-800 hover:bg-slate-50">{p.name}</button>
                ))}
            </div>
          </div>
        ) : (
          <div className="space-y-2">
            <input placeholder="Full name" value={np.name} onChange={(e) => setNp({ ...np, name: e.target.value })} className="field w-full" />
            <div className="flex gap-2">
              <input placeholder="Age" inputMode="numeric" value={np.age} onChange={(e) => setNp({ ...np, age: e.target.value })} className="field w-1/2" />
              <select value={np.gender} onChange={(e) => setNp({ ...np, gender: e.target.value })} className="field w-1/2">
                <option value="">Gender</option><option value="male">Male</option><option value="female">Female</option><option value="other">Other</option>
              </select>
            </div>
            <div className="flex gap-2">
              <input placeholder="Height (cm)" inputMode="numeric" value={np.height_cm} onChange={(e) => setNp({ ...np, height_cm: e.target.value })} className="field w-1/2" />
              <input placeholder="Weight (kg)" inputMode="numeric" value={np.weight_kg} onChange={(e) => setNp({ ...np, weight_kg: e.target.value })} className="field w-1/2" />
            </div>
            <input placeholder="Phone" value={np.phone} onChange={(e) => setNp({ ...np, phone: e.target.value })} className="field w-full" />
            <button disabled={busy || !np.name.trim()} onClick={() => save({
              new_patient: {
                name: np.name, gender: np.gender || null, phone: np.phone || null,
                age: np.age ? parseInt(np.age) : null,
                height_cm: np.height_cm ? parseFloat(np.height_cm) : null,
                weight_kg: np.weight_kg ? parseFloat(np.weight_kg) : null,
              },
            })} className="btn-primary w-full">{busy ? "Saving…" : "Create patient & save visit"}</button>
          </div>
        )}
        {err && <p className="mt-2 text-sm text-red-600">{err}</p>}
        <button onClick={onClose} className="mt-3 w-full text-center text-sm text-slate-400">Cancel</button>
      </div>
    </div>
  );
}
