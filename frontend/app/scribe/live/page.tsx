"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { supabase } from "../../../lib/supabaseClient";
import { apiGet, apiPost, apiUpload } from "../../../lib/api";

type RedFlag = { finding: string; concern: string; urgency: string; action: string; source?: string };
type Question = { question: string; severity: string };
type Ddx = { diagnosis: string; likelihood: string; icd10: string };
type Ds = { available: boolean; differential_diagnosis: Ddx[]; must_not_miss: { diagnosis: string }[] };

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

function flatten(chunks: Float32Array[]): Float32Array {
  const len = chunks.reduce((a, c) => a + c.length, 0);
  const out = new Float32Array(len); let off = 0;
  for (const c of chunks) { out.set(c, off); off += c.length; }
  return out;
}

const urgTag: Record<string, string> = { emergency: "bg-red-600 text-white", urgent: "bg-amber-500 text-white", routine: "bg-slate-400 text-white" };
const dot: Record<string, string> = { emergency: "bg-red-500", urgent: "bg-amber-500", routine: "bg-slate-400" };
const sevDot: Record<string, string> = { high: "bg-red-500", moderate: "bg-amber-500", low: "bg-slate-300" };
const like: Record<string, string> = { high: "bg-red-100 text-red-700", moderate: "bg-amber-100 text-amber-700", medium: "bg-amber-100 text-amber-700", low: "bg-slate-100 text-slate-600" };
const sectLabel = "mb-2 text-[11px] font-bold uppercase tracking-wide text-slate-500";

export default function LiveConsult() {
  const router = useRouter();
  const [live, setLive] = useState(false);
  const [status, setStatus] = useState("Ready");
  const [transcriptEn, setTranscriptEn] = useState("");
  const [symptoms, setSymptoms] = useState<string[]>([]);
  const [redFlags, setRedFlags] = useState<RedFlag[]>([]);
  const [questions, setQuestions] = useState<Question[]>([]);
  const [ds, setDs] = useState<Ds | null>(null);
  const [patientName, setPatientName] = useState("");
  const [consent, setConsent] = useState(false);

  // audio + loop refs
  const ctxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const procRef = useRef<ScriptProcessorNode | null>(null);
  const srcRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Float32Array[]>([]);
  const rateRef = useRef(48000);
  const sentRef = useRef(0);
  const rawRef = useRef("");
  const procFlagRef = useRef(false);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const rafRef = useRef(0);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // patient context
  const metaRef = useRef<{ age?: string; gender?: string; weight?: string }>({});
  const contextRef = useRef<string>("");
  const patientIdRef = useRef<string | null>(null);
  const synthAtRef = useRef(0);
  const synthCountRef = useRef(0);
  const [ended, setEnded] = useState(false);

  useEffect(() => {
    (async () => {
      const { data } = await supabase.auth.getSession();
      if (!data.session) { router.push("/login"); return; }
      const pid = new URLSearchParams(window.location.search).get("patient");
      patientIdRef.current = pid;
      if (pid) {
        const [p, s] = await Promise.all([apiGet(`/patients/${pid}`), apiGet(`/patients/${pid}/summary`)]);
        if (p.ok) { const pj = await p.json(); setPatientName(pj.name);
          const age = pj.dob ? String(new Date().getFullYear() - new Date(pj.dob).getFullYear()) : undefined;
          metaRef.current = { age, gender: pj.gender || undefined, weight: pj.weight_kg ? String(pj.weight_kg) : undefined }; }
        if (s.ok) contextRef.current = (await s.json()).context_text || "";
      }
    })();
    return () => stopAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current, analyser = analyserRef.current;
    if (!canvas || !analyser) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
    const g = canvas.getContext("2d")!; g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
    const bins = analyser.frequencyBinCount; const buf = new Uint8Array(bins); analyser.getByteFrequencyData(buf);
    const bars = 44; const mid = h / 2; const bw = w / bars;
    for (let i = 0; i < bars; i++) {
      const idx = Math.floor((i / bars) * bins * 0.7);
      const amp = (buf[idx] / 255) ** 1.4;
      const bh = Math.max(3, amp * (h * 0.9));
      const x = i * bw + bw * 0.2; const bwid = bw * 0.6;
      const grad = g.createLinearGradient(0, mid - bh / 2, 0, mid + bh / 2);
      grad.addColorStop(0, "#6366f1"); grad.addColorStop(1, "#3b82f6");
      g.fillStyle = grad;
      const r = Math.min(bwid / 2, bh / 2);
      const y = mid - bh / 2;
      g.beginPath(); g.roundRect(x, y, bwid, bh, r); g.fill();
    }
    rafRef.current = requestAnimationFrame(draw);
  }, []);

  async function start() {
    if (!consent) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true }); streamRef.current = stream;
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new AC(); ctxRef.current = ctx; rateRef.current = ctx.sampleRate;
      const src = ctx.createMediaStreamSource(stream); srcRef.current = src;
      const analyser = ctx.createAnalyser(); analyser.fftSize = 256; analyserRef.current = analyser; src.connect(analyser);
      const proc = ctx.createScriptProcessor(4096, 1, 1); procRef.current = proc;
      chunksRef.current = []; sentRef.current = 0; rawRef.current = "";
      proc.onaudioprocess = (e) => chunksRef.current.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      const mute = ctx.createGain(); mute.gain.value = 0; src.connect(proc); proc.connect(mute); mute.connect(ctx.destination);
      setLive(true); setEnded(false); setStatus("Listening…"); setDs(null); setRedFlags([]); setQuestions([]); setSymptoms([]); setTranscriptEn("");
      rafRef.current = requestAnimationFrame(draw);
      intervalRef.current = setInterval(() => { void tick(false); }, 10000);
    } catch { setStatus("Microphone access denied."); }
  }

  async function tick(final: boolean) {
    if (procFlagRef.current) return;
    const all = flatten(chunksRef.current);
    const newCount = all.length - sentRef.current;
    const minNew = rateRef.current * (final ? 0.5 : 4);
    if (newCount < minNew && !final) return;
    procFlagRef.current = true;
    try {
      if (newCount > 0) {
        const slice = new Float32Array(all.subarray(sentRef.current)); sentRef.current = all.length;
        setStatus("Transcribing…");
        const form = new FormData(); form.append("file", encodeWav(slice, rateRef.current), "live.wav");
        const r = await apiUpload("/scribe/transcribe", form);
        if (r.ok) { const t = ((await r.json()).transcript || "").trim(); if (t) rawRef.current = (rawRef.current + " " + t).trim(); }
      }
      await analyzeLive();
    } finally { procFlagRef.current = false; if (live && !final) setStatus("Listening…"); }
  }

  async function analyzeLive() {
    if (rawRef.current.length < 3) return;
    setStatus("Reading…");
    const r = await apiPost("/scribe/live", { transcript: rawRef.current, patient_context: contextRef.current });
    if (r.ok) {
      const d = await r.json();
      setTranscriptEn(d.translation || "");
      setSymptoms(d.symptoms || []); setRedFlags(d.red_flags || []); setQuestions(d.questions || []);
      void maybeSynth(d.symptoms || []);
    }
  }

  async function maybeSynth(syms: string[]) {
    if (syms.length === 0) return;
    const now = Date.now();
    if (now - synthAtRef.current < 25000 && syms.length <= synthCountRef.current) return;
    synthAtRef.current = now; synthCountRef.current = syms.length;
    const r = await apiPost("/synthesis/decision-support", {
      chief_complaints: syms, age: metaRef.current.age, gender: metaRef.current.gender, patient_weight: metaRef.current.weight,
    });
    if (r.ok) { const d = await r.json(); if (d.available) setDs(d); }
  }

  function stopAll() {
    if (intervalRef.current) { clearInterval(intervalRef.current); intervalRef.current = null; }
    if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0; }
    procRef.current?.disconnect(); srcRef.current?.disconnect(); analyserRef.current?.disconnect();
    streamRef.current?.getTracks().forEach((t) => t.stop());
    ctxRef.current?.close().catch(() => {}); ctxRef.current = null;
  }

  async function stop() {
    if (intervalRef.current) { clearInterval(intervalRef.current); intervalRef.current = null; }
    setLive(false); setStatus("Finishing…");
    await tick(true);
    stopAll();
    setEnded(true);
    setStatus("Session ended");
  }

  // Carry the captured consultation into the standard scribe to finalise the
  // SOAP note, prescribe, attest and save.
  function handoff() {
    const t = rawRef.current.trim();
    if (!t) return;
    try { sessionStorage.setItem("cma_live_handoff", JSON.stringify({ transcript: t })); } catch { /* ignore */ }
    const pid = patientIdRef.current;
    router.push(pid ? `/scribe?patient=${pid}&from=live` : "/scribe?from=live");
  }

  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      <header className="mb-4 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">Live Consultation</h1>
          <p className="text-sm text-slate-500">Real-time transcription, translation &amp; decision support · physician review only</p>
        </div>
        <div className="flex items-center gap-4 text-sm">
          {patientName && <span className="text-slate-600">Patient: <span className="font-medium text-slate-900">{patientName}</span></span>}
          <Link href="/scribe" className="text-slate-500 hover:text-slate-900">Standard scribe →</Link>
        </div>
      </header>

      <div className="grid gap-4 md:grid-cols-2">
        {/* LEFT — mic + live transcript */}
        <section className="glass flex flex-col rounded-2xl p-5">
          {!live && !consent && (
            <label className="mb-3 flex items-start gap-2.5 rounded-xl bg-amber-50/70 p-3">
              <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} className="mt-0.5 h-4 w-4 accent-amber-600" />
              <span className="text-xs text-slate-700"><span className="font-semibold text-amber-800">Recording consent.</span> The patient has been informed and consents to recording this consultation.</span>
            </label>
          )}

          <div className="relative flex h-40 items-center justify-center overflow-hidden rounded-2xl bg-slate-900">
            <canvas ref={canvasRef} className="h-full w-full" />
            {!live && <span className="absolute text-xs font-medium text-slate-400">{status === "Session ended" ? "Session ended" : "Press Go live to start"}</span>}
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-3">
            {!live
              ? <button onClick={start} disabled={!consent} className="btn-primary disabled:opacity-40">● Go live</button>
              : <button onClick={stop} className="rounded-xl bg-red-600 px-4 py-2.5 text-sm font-medium text-white">■ Finish</button>}
            {ended && rawRef.current.trim().length > 0 && (
              <button onClick={handoff} className="rounded-xl bg-emerald-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-emerald-500">Review, prescribe &amp; save →</button>
            )}
            <span className="flex items-center gap-2 text-xs text-slate-500">
              {live && <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-red-500" />}
              {status}
            </span>
          </div>

          <div className="mt-4 flex-1">
            <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Live transcript (English)</p>
            <div className="h-56 overflow-y-auto rounded-xl border border-slate-200 bg-white/60 p-3 text-sm leading-relaxed text-slate-700">
              {transcriptEn || <span className="text-slate-400">The translated conversation will appear here as you speak…</span>}
            </div>
          </div>
        </section>

        {/* RIGHT — clean, prioritised decision support */}
        <section className="space-y-3">
          {/* 1. Alerts — only real ones, compact */}
          {redFlags.length > 0 && (
            <div className="rounded-2xl border border-red-200 bg-red-50/70 p-4">
              <p className="mb-2 text-[11px] font-bold uppercase tracking-wide text-red-700">Alerts</p>
              <ul className="space-y-2">
                {redFlags.slice(0, 3).map((f, i) => (
                  <li key={i}>
                    <div className="flex items-center gap-2">
                      <span className={`h-2 w-2 shrink-0 rounded-full ${dot[f.urgency] || dot.routine}`} />
                      <span className="text-sm font-semibold text-slate-900">{f.finding}</span>
                      <span className={`ml-auto shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${urgTag[f.urgency] || urgTag.routine}`}>{f.urgency}</span>
                    </div>
                    {f.action && <p className="ml-4 mt-0.5 line-clamp-2 text-xs text-slate-600">{f.action}</p>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* 2. Working differential (Clinical Synthesis) — the grounded reasoning */}
          <div className="glass rounded-2xl p-4">
            <p className={`${sectLabel} flex items-center gap-2`}>Working differential
              <span className="rounded bg-indigo-100 px-1 py-0.5 text-[9px] font-bold text-indigo-700">SYNTHESIS</span>
            </p>
            {!ds || ds.differential_diagnosis.length === 0 ? (
              <p className="text-xs text-slate-400">Builds as symptoms are gathered…</p>
            ) : (
              <>
                {ds.must_not_miss.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-1">
                    {ds.must_not_miss.slice(0, 3).map((m, i) => <span key={i} className="rounded bg-red-600 px-1.5 py-0.5 text-[10px] font-medium text-white">Don&apos;t miss: {m.diagnosis}</span>)}
                  </div>
                )}
                <ol className="space-y-1.5">
                  {ds.differential_diagnosis.slice(0, 4).map((d, i) => (
                    <li key={i} className="flex items-center gap-2 text-sm text-slate-700">
                      <span className="w-3 shrink-0 text-xs text-slate-400">{i + 1}</span>
                      <span className="font-medium">{d.diagnosis}</span>
                      {d.likelihood && <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${like[d.likelihood.toLowerCase()] || like.low}`}>{d.likelihood}</span>}
                    </li>
                  ))}
                </ol>
              </>
            )}
          </div>

          {/* 3. Symptoms — compact chips */}
          <div className="glass rounded-2xl p-4">
            <p className={sectLabel}>Symptoms</p>
            {symptoms.length === 0 ? <p className="text-xs text-slate-400">Listening…</p> : (
              <div className="flex flex-wrap items-center gap-1.5">
                {symptoms.slice(0, 8).map((s, i) => <span key={i} className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs text-slate-700">{s}</span>)}
                {symptoms.length > 8 && <span className="text-xs text-slate-400">+{symptoms.length - 8}</span>}
              </div>
            )}
          </div>

          {/* 4. Ask the patient — top few, one line each */}
          <div className="glass rounded-2xl p-4">
            <p className={sectLabel}>Ask the patient</p>
            {questions.length === 0 ? <p className="text-xs text-slate-400">Suggestions will appear…</p> : (
              <ul className="space-y-1.5">
                {questions.slice(0, 3).map((q, i) => (
                  <li key={i} className="flex items-start gap-2 text-sm text-slate-700">
                    <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${sevDot[q.severity] || sevDot.low}`} />
                    <span>{q.question}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
      </div>

      <p className="mt-4 text-center text-xs text-slate-400">Physician-review-only assistance — not an autonomous diagnosis. Use the standard scribe to finalise, prescribe &amp; save the visit.</p>
    </main>
  );
}
