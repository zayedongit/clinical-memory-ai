"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { supabase } from "../../lib/supabaseClient";
import { apiPost, apiUpload } from "../../lib/api";

type Soap = { subjective: string; objective: string; assessment: string; plan: string };
type Entities = { symptoms: string[]; medications: string[]; allergies: string[]; diagnoses: string[]; follow_up: string[] };

// Encode mono Float32 PCM as a 16-bit WAV blob (a format Sarvam accepts).
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
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true); o += 2;
  }
  return new Blob([view], { type: "audio/wav" });
}

export default function ScribePage() {
  const router = useRouter();
  const [recording, setRecording] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [soap, setSoap] = useState<Soap | null>(null);
  const [entities, setEntities] = useState<Entities | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const ctxRef = useRef<AudioContext | null>(null);
  const procRef = useRef<ScriptProcessorNode | null>(null);
  const srcRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Float32Array[]>([]);
  const rateRef = useRef<number>(48000);

  useEffect(() => {
    (async () => {
      const { data } = await supabase.auth.getSession();
      if (!data.session) router.push("/login");
    })();
  }, [router]);

  async function startRecording() {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new AC();
      ctxRef.current = ctx;
      rateRef.current = ctx.sampleRate;
      const src = ctx.createMediaStreamSource(stream);
      srcRef.current = src;
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      procRef.current = proc;
      chunksRef.current = [];
      proc.onaudioprocess = (e) => {
        chunksRef.current.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      };
      const mute = ctx.createGain();
      mute.gain.value = 0;
      src.connect(proc);
      proc.connect(mute);
      mute.connect(ctx.destination);
      setRecording(true);
    } catch {
      setError("Microphone access denied or unavailable.");
    }
  }

  async function stopRecording() {
    setRecording(false);
    procRef.current?.disconnect();
    srcRef.current?.disconnect();
    streamRef.current?.getTracks().forEach((t) => t.stop());
    await ctxRef.current?.close();

    const chunks = chunksRef.current;
    const len = chunks.reduce((a, c) => a + c.length, 0);
    if (len === 0) { setError("No audio captured."); return; }
    const merged = new Float32Array(len);
    let off = 0;
    for (const c of chunks) { merged.set(c, off); off += c.length; }
    await transcribe(encodeWav(merged, rateRef.current));
  }

  async function transcribe(blob: Blob) {
    setBusy("Transcribing…");
    setError(null);
    const form = new FormData();
    form.append("file", blob, "consultation.wav");
    const r = await apiUpload("/scribe/transcribe", form);
    setBusy(null);
    if (!r.ok) { setError(`Transcription failed (${r.status}). ${await r.text()}`); return; }
    setTranscript((await r.json()).transcript || "");
  }

  async function generateSoap() {
    setBusy("Generating SOAP…");
    setError(null);
    const r = await apiPost("/scribe/soap", { transcript });
    setBusy(null);
    if (!r.ok) { setError(`SOAP generation failed (${r.status}). ${await r.text()}`); return; }
    const data = await r.json();
    setSoap(data.soap); setEntities(data.entities);
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">New Consultation</h1>
          <p className="text-sm text-slate-500">Record → transcript → SOAP draft. Physician reviews everything.</p>
        </div>
        <Link href="/patients" className="text-sm text-slate-500 transition hover:text-slate-900">← Patients</Link>
      </header>

      <div className="glass mb-6 flex items-center gap-4 rounded-2xl p-4">
        {!recording ? (
          <button onClick={startRecording} disabled={!!busy} className="btn-primary">● Record</button>
        ) : (
          <button onClick={stopRecording} className="rounded-xl bg-red-600 px-4 py-2.5 text-sm font-medium text-white">■ Stop</button>
        )}
        <span className="text-xs text-slate-500">
          {recording ? "Recording…" : busy ? busy : "Keep it short (~30s) for now."}
        </span>
      </div>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      {(transcript || busy === "Transcribing…") && (
        <section className="mb-6">
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Transcript <span className="font-normal text-slate-400">(editable)</span></h2>
          <textarea value={transcript} onChange={(e) => setTranscript(e.target.value)} rows={5}
            className="field w-full" placeholder="Transcript will appear here…" />
          <button onClick={generateSoap} disabled={!!busy || transcript.trim().length < 3} className="btn-primary mt-3">Generate SOAP note</button>
        </section>
      )}

      {soap && (
        <section className="space-y-4">
          <h2 className="text-sm font-semibold text-slate-700">SOAP note <span className="font-normal text-slate-400">(AI draft — review &amp; edit)</span></h2>
          {(["subjective", "objective", "assessment", "plan"] as const).map((k) => (
            <div key={k}>
              <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500">{k}</label>
              <textarea value={soap[k]} onChange={(e) => setSoap({ ...soap, [k]: e.target.value })}
                rows={k === "plan" || k === "subjective" ? 3 : 2} className="field w-full" />
            </div>
          ))}
          {entities && (
            <div className="glass rounded-2xl p-4">
              <p className="mb-2 text-xs font-semibold text-slate-700">Extracted entities</p>
              {(Object.keys(entities) as (keyof Entities)[]).map((k) =>
                entities[k].length > 0 ? (
                  <div key={k} className="mb-2">
                    <span className="text-xs font-medium text-slate-500">{k.replace("_", " ")}: </span>
                    {entities[k].map((v, i) => (
                      <span key={i} className="mr-1 inline-block rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600">{v}</span>
                    ))}
                  </div>
                ) : null,
              )}
            </div>
          )}
          <p className="text-xs text-slate-400">AI-generated draft. Nothing is saved to the patient record until a physician approves it.</p>
        </section>
      )}
    </main>
  );
}
