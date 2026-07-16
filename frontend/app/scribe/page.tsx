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
type RedFlag = { finding: string; concern: string; urgency: "emergency" | "urgent" | "routine"; action: string };
type Considerations = { red_flags: RedFlag[]; missing_information: string[]; suggested_investigations: { test: string; rationale: string }[]; completeness_pct: number };
type RxItem = { brand: string; generic: string | null; strength: string | null; form: string | null; dose: string; frequency: string; duration: string; instructions: string; warning?: string };
type DrugResult = { brand_name: string; generic_name: string | null; strength: string | null; dosage_form: string | null };
type Patient = { id: string; name: string };
type Summary = {
  visit_count: number; problems: string[]; medications: string[]; allergies: string[];
  recurring_symptoms: { term: string; count: number }[];
  recent_visits: { date: string; assessment: string }[];
  since_last: { new_symptoms?: string[]; resolved_symptoms?: string[]; new_medications?: string[]; stopped_medications?: string[] };
  context_text: string;
};

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

const sevColor: Record<string, string> = { high: "bg-red-100 text-red-700", moderate: "bg-amber-100 text-amber-700", low: "bg-slate-100 text-slate-600" };

export default function ScribePage() {
  const router = useRouter();
  const [recording, setRecording] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [dialogue, setDialogue] = useState<Turn[]>([]);
  const [soap, setSoap] = useState<Soap | null>(null);
  const [entities, setEntities] = useState<Entities | null>(null);
  const [followUps, setFollowUps] = useState<FollowUp[]>([]);
  const [considerations, setConsiderations] = useState<Considerations | null>(null);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showSave, setShowSave] = useState(false);
  const [saved, setSaved] = useState<{ visit_id: string; patient_id: string } | null>(null);
  const [segments, setSegments] = useState(0);
  const [isFinal, setIsFinal] = useState(false);
  const [rx, setRx] = useState<RxItem[]>([]);
  // longitudinal
  const [patientId, setPatientId] = useState<string | null>(null);
  const [patientName, setPatientName] = useState("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [showHistory, setShowHistory] = useState(true);

  const ctxRef = useRef<AudioContext | null>(null);
  const procRef = useRef<ScriptProcessorNode | null>(null);
  const srcRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Float32Array[]>([]);
  const rateRef = useRef(48000);

  async function attach(pid: string) {
    const [p, s] = await Promise.all([apiGet(`/patients/${pid}`), apiGet(`/patients/${pid}/summary`)]);
    if (p.ok) { setPatientId(pid); setPatientName((await p.json()).name); }
    if (s.ok) setSummary(await s.json());
  }

  useEffect(() => {
    (async () => {
      const { data } = await supabase.auth.getSession();
      if (!data.session) { router.push("/login"); return; }
      const pid = new URLSearchParams(window.location.search).get("patient");
      if (pid) await attach(pid);
    })();
  }, [router]);

  async function startRecording() {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true }); streamRef.current = stream;
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new AC(); ctxRef.current = ctx; rateRef.current = ctx.sampleRate;
      const s2 = ctx.createMediaStreamSource(stream); srcRef.current = s2;
      const proc = ctx.createScriptProcessor(4096, 1, 1); procRef.current = proc; chunksRef.current = [];
      proc.onaudioprocess = (e) => chunksRef.current.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      const mute = ctx.createGain(); mute.gain.value = 0; s2.connect(proc); proc.connect(mute); mute.connect(ctx.destination);
      setRecording(true);
    } catch { setError("Microphone access denied or unavailable."); }
  }

  async function stopRecording() {
    setRecording(false); procRef.current?.disconnect(); srcRef.current?.disconnect();
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
    const seg = (await r.json()).transcript || "";
    if (seg.trim()) { setTranscript((prev) => (prev.trim() ? prev.trim() + "\n" + seg : seg)); setSegments((n) => n + 1); }
  }

  async function analyze(mode: "interim" | "final") {
    setBusy(mode === "final" ? "Finalising note…" : "Analysing conversation…"); setError(null); setSaved(null);
    const r = await apiPost("/scribe/soap", { transcript, mode, patient_context: summary?.context_text }); setBusy(null);
    if (!r.ok) { setError(`Analysis failed (${r.status}). ${await r.text()}`); return; }
    const d = await r.json();
    setDialogue(d.dialogue || []); setSoap(d.soap); setEntities(d.entities);
    setFollowUps(d.follow_up_questions || []); setConsiderations(d.clinical_considerations || null);
    setIsFinal(mode === "final");
  }

  if (saved) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-10">
        <div className="glass rounded-2xl p-8 text-center">
          <p className="text-lg font-semibold text-slate-900">Visit saved ✓</p>
          <p className="mt-1 text-sm text-slate-500">The reviewed note is now in the patient&apos;s memory log.</p>
          <div className="mt-4 flex justify-center gap-3">
            <Link href={`/patients/${saved.patient_id}`} className="btn-primary">View patient record</Link>
            <button onClick={() => { setSaved(null); setTranscript(""); setSoap(null); setDialogue([]); setFollowUps([]); setEntities(null); setSegments(0); setIsFinal(false); setRx([]); setConsiderations(null); setConsent(false); }}
              className="rounded-xl border border-slate-200 px-4 py-2.5 text-sm text-slate-600">New consultation</button>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header className="mb-4 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">New Consultation</h1>
          <p className="text-sm text-slate-500">Records the doctor–patient conversation → dialogue, SOAP, follow-ups. Physician reviews everything.</p>
        </div>
        <Link href="/patients" className="text-sm text-slate-500 transition hover:text-slate-900">← Patients</Link>
      </header>

      {/* Patient attach + history */}
      <div className="glass mb-4 flex items-center justify-between rounded-2xl px-4 py-3">
        {patientId ? (
          <>
            <div>
              <span className="text-sm font-medium text-slate-900">{patientName}</span>
              {summary && summary.visit_count > 0 && <span className="ml-2 text-xs text-emerald-600">history-aware · {summary.visit_count} prior visit{summary.visit_count > 1 ? "s" : ""}</span>}
            </div>
            <div className="flex items-center gap-3 text-sm">
              {summary && summary.visit_count > 0 && <button onClick={() => setShowHistory(!showHistory)} className="text-blue-600">{showHistory ? "Hide history" : "Show history"}</button>}
              <button onClick={() => { setPatientId(null); setSummary(null); setPatientName(""); }} className="text-slate-400 hover:text-slate-700">Detach</button>
            </div>
          </>
        ) : (
          <>
            <span className="text-sm text-slate-500">No patient attached — attach one to make the note history-aware.</span>
            <AttachPatient onAttach={attach} />
          </>
        )}
      </div>
      {patientId && showHistory && summary && summary.visit_count > 0 && <HistoryPanel s={summary} />}

      <div className="glass mb-6 rounded-2xl p-4">
        <label className="mb-3 flex items-start gap-2.5 rounded-xl bg-amber-50/70 p-3">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} disabled={recording || segments > 0}
            className="mt-0.5 h-4 w-4 accent-amber-600" />
          <span className="text-xs text-slate-700">
            <span className="font-semibold text-amber-800">Recording consent.</span>{" "}
            I confirm the patient has been informed and has given consent to record this
            consultation for documentation. This is stored with the visit.
          </span>
        </label>
        <div className="flex items-center gap-4">
          {!recording
            ? <button onClick={startRecording} disabled={!!busy || !consent} className="btn-primary disabled:opacity-40">● Record</button>
            : <button onClick={stopRecording} className="rounded-xl bg-red-600 px-4 py-2.5 text-sm font-medium text-white">■ Stop</button>}
          <span className="text-xs text-slate-500">
            {recording ? "Recording…" : busy ? busy
              : !consent && segments === 0 ? "Confirm consent to enable recording."
              : segments > 0 ? `${segments} segment${segments > 1 ? "s" : ""} recorded — record more or analyse`
              : "Keep each segment short (~30s) for now."}
          </span>
        </div>
      </div>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      {(transcript || busy === "Transcribing…") && (
        <section className="mb-6">
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Transcript <span className="font-normal text-slate-400">(editable)</span></h2>
          <textarea value={transcript} onChange={(e) => setTranscript(e.target.value)} rows={4} className="field w-full" placeholder="Transcript…" />
          <div className="mt-3 flex flex-wrap gap-2">
            <button onClick={() => analyze("interim")} disabled={!!busy || transcript.trim().length < 3} className="btn-primary">{soap ? "Re-analyse" : "Analyse consultation"}</button>
            {soap && <button onClick={() => analyze("final")} disabled={!!busy} className="rounded-xl bg-emerald-600 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-emerald-500">Finalise consultation</button>}
          </div>
          <p className="mt-2 text-xs text-slate-400">Tip: press ● Record again to add the next part of the visit, then re-analyse. Finalise when the consultation is complete.</p>
        </section>
      )}

      {dialogue.length > 0 && (
        <section className="mb-6">
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Conversation</h2>
          <div className="glass space-y-2 rounded-2xl p-4">
            {dialogue.map((t, i) => (
              <div key={i}>
                <span className={`mr-2 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${t.speaker === "doctor" ? "bg-blue-100 text-blue-700" : "bg-emerald-100 text-emerald-700"}`}>{t.speaker}</span>
                <span className="text-sm text-slate-700">{t.text}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {considerations && <ConsiderationsPanel c={considerations} />}

      {soap && (
        <section className="mb-6 space-y-4">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-700">
            {isFinal ? "Final note" : "SOAP note"}
            {isFinal && <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700">FINAL</span>}
            <span className="font-normal text-slate-400">(AI draft — review &amp; edit)</span>
          </h2>
          {(["subjective", "objective", "assessment", "plan"] as const).map((k) => (
            <div key={k}>
              <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500">{k}</label>
              <textarea value={soap[k]} onChange={(e) => setSoap({ ...soap, [k]: e.target.value })} rows={k === "assessment" ? 5 : k === "objective" ? 2 : 3} className="field w-full" />
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
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-blue-500" style={{ width: `${f.likelihood_pct}%` }} /></div>
                  <span className="text-[11px] text-slate-400">{f.likelihood_pct}% relevance</span>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {soap && <RxSection items={rx} setItems={setRx} summary={summary} />}

      {soap && <div className="flex justify-end"><button onClick={() => setShowSave(true)} className="btn-primary">Save visit</button></div>}

      {showSave && soap && (
        <SaveModal onClose={() => setShowSave(false)} onSaved={(res) => { setShowSave(false); setSaved(res); }}
          defaultPatient={patientId ? { id: patientId, name: patientName } : undefined}
          redFlagCount={considerations?.red_flags.length ?? 0}
          payload={{ transcript, dialogue, soap, entities: entities ?? undefined, follow_up_questions: followUps,
            prescription: rx.map(({ warning, ...r }) => r), clinical_considerations: considerations ?? undefined,
            consent_given: consent, consent_method: consent ? "verbal" : undefined }} />
      )}
    </main>
  );
}

function Line({ label, items, tone }: { label: string; items: string[]; tone?: string }) {
  return (
    <div className="mb-1"><span className="text-xs font-medium text-slate-500">{label}: </span>
      {items.map((x, i) => <span key={i} className={`mr-1 inline-block rounded-full px-2 py-0.5 text-xs ${tone === "red" ? "bg-red-100 text-red-700" : "bg-slate-100 text-slate-600"}`}>{x}</span>)}
    </div>
  );
}

const urgencyStyle: Record<string, { box: string; tag: string; label: string }> = {
  emergency: { box: "border-red-300 bg-red-50", tag: "bg-red-600 text-white", label: "EMERGENCY" },
  urgent: { box: "border-amber-300 bg-amber-50", tag: "bg-amber-500 text-white", label: "URGENT" },
  routine: { box: "border-slate-200 bg-slate-50", tag: "bg-slate-400 text-white", label: "NOTE" },
};

function ConsiderationsPanel({ c }: { c: Considerations }) {
  const hasAny = c.red_flags.length > 0 || c.missing_information.length > 0 || c.suggested_investigations.length > 0;
  if (!hasAny && !c.completeness_pct) return null;
  return (
    <section className="mb-6">
      <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-700">
        Clinical considerations
        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-500">Physician review only — not a diagnosis</span>
      </h2>

      {c.red_flags.length > 0 && (
        <div className="mb-3 space-y-2">
          {c.red_flags.map((f, i) => {
            const st = urgencyStyle[f.urgency] || urgencyStyle.routine;
            return (
              <div key={i} className={`rounded-xl border ${st.box} p-3`}>
                <div className="flex items-start justify-between gap-3">
                  <p className="text-sm font-semibold text-slate-900">⚠ {f.finding}</p>
                  <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold ${st.tag}`}>{st.label}</span>
                </div>
                {f.concern && <p className="mt-1 text-xs text-slate-600">Possible concern: {f.concern}</p>}
                {f.action && <p className="mt-1 text-xs font-medium text-slate-700">Consider: {f.action}</p>}
              </div>
            );
          })}
        </div>
      )}

      <div className="glass grid gap-4 rounded-2xl p-4 sm:grid-cols-2">
        {c.suggested_investigations.length > 0 && (
          <div>
            <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">Investigations to consider</p>
            <ul className="space-y-1">
              {c.suggested_investigations.map((t, i) => (
                <li key={i} className="text-sm text-slate-700">
                  <span className="font-medium">{t.test}</span>
                  {t.rationale && <span className="text-slate-500"> — {t.rationale}</span>}
                </li>
              ))}
            </ul>
          </div>
        )}
        {c.missing_information.length > 0 && (
          <div>
            <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">Missing information</p>
            <ul className="list-disc pl-4 text-sm text-slate-600">
              {c.missing_information.map((m, i) => <li key={i}>{m}</li>)}
            </ul>
          </div>
        )}
        {c.completeness_pct > 0 && (
          <div className="sm:col-span-2">
            <div className="mb-1 flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Consultation completeness</span>
              <span className="text-xs text-slate-500">{c.completeness_pct}%</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
              <div className={`h-full rounded-full ${c.completeness_pct >= 75 ? "bg-emerald-500" : c.completeness_pct >= 50 ? "bg-amber-500" : "bg-red-500"}`} style={{ width: `${c.completeness_pct}%` }} />
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function HistoryPanel({ s }: { s: Summary }) {
  const sl = s.since_last || {};
  const changed = (sl.new_symptoms?.length || sl.resolved_symptoms?.length || sl.new_medications?.length || sl.stopped_medications?.length);
  return (
    <div className="glass mb-6 rounded-2xl p-4">
      <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Patient history (used by the note)</p>
      {s.problems.length > 0 && <Line label="Problems" items={s.problems} />}
      {s.medications.length > 0 && <Line label="Medications" items={s.medications} />}
      {s.allergies.length > 0 && <Line label="Allergies" items={s.allergies} tone="red" />}
      {s.recurring_symptoms.length > 0 && <Line label="Recurring" items={s.recurring_symptoms.map((r) => `${r.term} ×${r.count}`)} />}
      {changed ? (
        <div className="mt-2 border-t border-slate-200/70 pt-2 text-xs text-slate-600">
          <p className="mb-1 font-semibold text-slate-600">Since last visit</p>
          {sl.new_symptoms?.length ? <p>New symptoms: {sl.new_symptoms.join(", ")}</p> : null}
          {sl.resolved_symptoms?.length ? <p>Resolved: {sl.resolved_symptoms.join(", ")}</p> : null}
          {sl.new_medications?.length ? <p>Started: {sl.new_medications.join(", ")}</p> : null}
          {sl.stopped_medications?.length ? <p>Stopped: {sl.stopped_medications.join(", ")}</p> : null}
        </div>
      ) : null}
      {s.recent_visits.length > 0 && (
        <div className="mt-2 border-t border-slate-200/70 pt-2">
          <p className="mb-1 text-xs font-semibold text-slate-600">Recent visits</p>
          {s.recent_visits.map((v, i) => <p key={i} className="text-xs text-slate-500">{new Date(v.date).toLocaleDateString()} — {v.assessment || "—"}</p>)}
        </div>
      )}
    </div>
  );
}

function AttachPatient({ onAttach }: { onAttach: (id: string) => void }) {
  const [q, setQ] = useState(""); const [list, setList] = useState<Patient[]>([]); const [open, setOpen] = useState(false);
  useEffect(() => { if (open && list.length === 0) apiGet("/patients").then(async (r) => { if (r.ok) setList((await r.json()).items || []); }); }, [open, list.length]);
  const f = list.filter((p) => p.name.toLowerCase().includes(q.toLowerCase()));
  return (
    <div className="relative">
      <button onClick={() => setOpen(!open)} className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-medium text-white">Attach patient</button>
      {open && (
        <div className="absolute right-0 z-10 mt-1 w-64 rounded-xl border border-slate-200 bg-white p-2 shadow-lg">
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search…" className="field mb-1 w-full" />
          <div className="max-h-48 overflow-y-auto">
            {f.length === 0 ? <p className="p-2 text-xs text-slate-400">No patients.</p> :
              f.map((p) => <button key={p.id} onClick={() => { onAttach(p.id); setOpen(false); }} className="block w-full rounded px-2 py-1.5 text-left text-sm text-slate-800 hover:bg-slate-50">{p.name}</button>)}
          </div>
        </div>
      )}
    </div>
  );
}

function RxSection({ items, setItems, summary }: { items: RxItem[]; setItems: (v: RxItem[]) => void; summary: Summary | null }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<DrugResult[]>([]);

  useEffect(() => {
    if (q.trim().length < 2) { setResults([]); return; }
    const t = setTimeout(async () => {
      const r = await apiGet(`/drugs?q=${encodeURIComponent(q.trim())}`);
      if (r.ok) setResults((await r.json()).items || []);
    }, 300);
    return () => clearTimeout(t);
  }, [q]);

  function checkSafety(generic: string | null): string | undefined {
    if (!generic || !summary) return undefined;
    const g = generic.toLowerCase();
    const a = (summary.allergies || []).find((x) => g.includes(x.toLowerCase()) || x.toLowerCase().includes(g));
    if (a) return `Possible allergy — patient allergic to ${a}`;
    const m = (summary.medications || []).find((x) => g.includes(x.toLowerCase()) || x.toLowerCase().includes(g));
    if (m) return `Already noted on ${m} (duplicate?)`;
    return undefined;
  }

  function add(d: DrugResult) {
    setItems([...items, { brand: d.brand_name, generic: d.generic_name, strength: d.strength, form: d.dosage_form, dose: "", frequency: "", duration: "", instructions: "", warning: checkSafety(d.generic_name) }]);
    setQ(""); setResults([]);
  }
  const update = (i: number, patch: Partial<RxItem>) => setItems(items.map((it, idx) => (idx === i ? { ...it, ...patch } : it)));
  const remove = (i: number) => setItems(items.filter((_, idx) => idx !== i));

  return (
    <section className="mb-6">
      <h2 className="mb-2 text-sm font-semibold text-slate-700">Prescription</h2>
      <div className="glass rounded-2xl p-4">
        <div className="relative mb-3">
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search drug (brand or generic)…" className="field w-full" />
          {results.length > 0 && (
            <div className="absolute z-10 mt-1 max-h-56 w-full overflow-y-auto rounded-xl border border-slate-200 bg-white shadow-lg">
              {results.map((d, i) => (
                <button key={i} onClick={() => add(d)} className="block w-full px-3 py-2 text-left text-sm hover:bg-slate-50">
                  <span className="font-medium text-slate-900">{d.brand_name}</span>
                  <span className="text-xs text-slate-500"> — {[d.generic_name, d.strength, d.dosage_form].filter(Boolean).join(" · ")}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        {items.length === 0 ? <p className="text-sm text-slate-400">No medications added.</p> : (
          <ul className="space-y-3">
            {items.map((it, i) => (
              <li key={i} className="rounded-xl border border-slate-200 p-3">
                <div className="mb-2 flex items-start justify-between">
                  <div>
                    <span className="text-sm font-medium text-slate-900">{it.brand}</span>
                    <span className="text-xs text-slate-500"> {[it.generic, it.strength, it.form].filter(Boolean).join(" · ")}</span>
                  </div>
                  <button onClick={() => remove(i)} className="text-xs text-slate-400 hover:text-red-600">Remove</button>
                </div>
                {it.warning && <p className="mb-2 rounded bg-red-50 px-2 py-1 text-xs font-medium text-red-700">⚠ {it.warning}</p>}
                <div className="flex flex-wrap gap-2">
                  <input placeholder="Dose (e.g. 500 mg)" value={it.dose} onChange={(e) => update(i, { dose: e.target.value })} className="field w-32" />
                  <input placeholder="Frequency (e.g. BD)" value={it.frequency} onChange={(e) => update(i, { frequency: e.target.value })} className="field w-32" />
                  <input placeholder="Duration (e.g. 5 days)" value={it.duration} onChange={(e) => update(i, { duration: e.target.value })} className="field w-36" />
                  <input placeholder="Instructions" value={it.instructions} onChange={(e) => update(i, { instructions: e.target.value })} className="field min-w-[120px] flex-1" />
                </div>
              </li>
            ))}
          </ul>
        )}
        {summary && summary.visit_count > 0 && <p className="mt-2 text-[11px] text-slate-400">Safety checks run against this patient&apos;s recorded allergies &amp; medications.</p>}
      </div>
    </section>
  );
}

function SaveModal({ onClose, onSaved, payload, defaultPatient, redFlagCount }: {
  onClose: () => void; onSaved: (r: { visit_id: string; patient_id: string }) => void;
  payload: Record<string, unknown>; defaultPatient?: { id: string; name: string }; redFlagCount: number;
}) {
  const [tab, setTab] = useState<"existing" | "new">("existing");
  const [patients, setPatients] = useState<Patient[]>([]);
  const [q, setQ] = useState("");
  const [np, setNp] = useState({ name: "", age: "", gender: "", height_cm: "", weight_kg: "", phone: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [attested, setAttested] = useState(false);

  useEffect(() => { apiGet("/patients").then(async (r) => { if (r.ok) setPatients((await r.json()).items || []); }); }, []);
  const filtered = useMemo(() => patients.filter((p) => p.name.toLowerCase().includes(q.toLowerCase())), [patients, q]);

  async function save(body: Record<string, unknown>) {
    if (!attested) { setErr("Please confirm you have reviewed and approve this note."); return; }
    setBusy(true); setErr(null);
    const r = await apiPost("/scribe/save", { ...payload, ...body, attested: true }); setBusy(false);
    if (!r.ok) { setErr(`Save failed (${r.status}).`); return; }
    onSaved(await r.json());
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 px-4" onClick={onClose}>
      <div className="glass w-full max-w-md rounded-2xl p-6" onClick={(e) => e.stopPropagation()}>
        <h3 className="mb-3 text-base font-semibold text-slate-900">Save this visit</h3>

        {redFlagCount > 0 && (
          <p className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-xs font-medium text-red-700">
            ⚠ {redFlagCount} clinical red flag{redFlagCount > 1 ? "s" : ""} were raised for review. Please confirm you have considered them.
          </p>
        )}

        <label className="mb-4 flex items-start gap-2.5 rounded-xl border border-slate-200 bg-slate-50/70 p-3">
          <input type="checkbox" checked={attested} onChange={(e) => setAttested(e.target.checked)} className="mt-0.5 h-4 w-4 accent-blue-600" />
          <span className="text-xs text-slate-700">
            <span className="font-semibold text-slate-900">Physician attestation.</span>{" "}
            I have reviewed this AI-assisted note and its considerations, corrected them as needed,
            and approve this record as clinically accurate. I remain the responsible clinician.
          </span>
        </label>

        {defaultPatient && (
          <button disabled={busy || !attested} onClick={() => save({ patient_id: defaultPatient.id })}
            className="btn-primary mb-3 w-full disabled:opacity-40">Save to {defaultPatient.name}</button>
        )}
        <div className="mb-4 flex gap-2">
          <button onClick={() => setTab("existing")} className={`flex-1 rounded-lg px-3 py-2 text-sm ${tab === "existing" ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600"}`}>Another existing</button>
          <button onClick={() => setTab("new")} className={`flex-1 rounded-lg px-3 py-2 text-sm ${tab === "new" ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600"}`}>New patient</button>
        </div>
        {tab === "existing" ? (
          <div>
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search patients…" className="field mb-2 w-full" />
            <div className="max-h-56 overflow-y-auto rounded-lg border border-slate-200">
              {filtered.length === 0 ? <p className="p-3 text-sm text-slate-400">No patients.</p> :
                filtered.map((p) => <button key={p.id} disabled={busy || !attested} onClick={() => save({ patient_id: p.id })} className="block w-full px-3 py-2.5 text-left text-sm text-slate-800 hover:bg-slate-50 disabled:opacity-40">{p.name}</button>)}
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
            <button disabled={busy || !np.name.trim() || !attested} onClick={() => save({
              new_patient: { name: np.name, gender: np.gender || null, phone: np.phone || null,
                age: np.age ? parseInt(np.age) : null, height_cm: np.height_cm ? parseFloat(np.height_cm) : null, weight_kg: np.weight_kg ? parseFloat(np.weight_kg) : null },
            })} className="btn-primary w-full disabled:opacity-40">{busy ? "Saving…" : "Create patient & save visit"}</button>
          </div>
        )}
        {err && <p className="mt-2 text-sm text-red-600">{err}</p>}
        <button onClick={onClose} className="mt-3 w-full text-center text-sm text-slate-400">Cancel</button>
      </div>
    </div>
  );
}
