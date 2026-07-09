"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { supabase } from "../../lib/supabaseClient";
import { apiGet } from "../../lib/api";

type MatchedVia = { label: string; term_type: string };
type Condition = {
  condition_id: string; name: string; specialty: string | null; cant_miss: boolean;
  prevalence_tier: string | null; findings_matched: number; matched_via: MatchedVia[];
  applies: boolean; applies_note: string | null;
};
type Result = { query: string; findings: string[]; candidate_conditions: Condition[]; red_flags: Condition[] };
type Detail = {
  id: string; name: string; specialty: string | null; icd: string[];
  symptoms: string[]; signs: string[]; red_flags: string[];
  investigations: string[]; followup: string[]; source: string | null;
};

export default function LookupPage() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [age, setAge] = useState("");
  const [sex, setSex] = useState("");
  const [res, setRes] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, Detail>>({});

  useEffect(() => {
    (async () => {
      const { data } = await supabase.auth.getSession();
      if (!data.session) router.push("/login");
    })();
  }, [router]);

  const run = useCallback(async (query: string, a: string, s: string) => {
    if (query.trim().length < 2) { setRes(null); return; }
    setBusy(true); setError(null);
    const p = new URLSearchParams({ q: query.trim() });
    if (a) p.set("age", a);
    if (s) p.set("sex", s);
    const r = await apiGet(`/match?${p.toString()}`);
    setBusy(false);
    if (!r.ok) { setError(`Lookup failed (${r.status}).`); return; }
    setRes(await r.json());
    setOpenId(null);
  }, []);

  // Debounced search-as-you-type
  useEffect(() => {
    const t = setTimeout(() => run(q, age, sex), 350);
    return () => clearTimeout(t);
  }, [q, age, sex, run]);

  async function toggle(id: string) {
    if (openId === id) { setOpenId(null); return; }
    setOpenId(id);
    if (!details[id]) {
      const r = await apiGet(`/conditions/${encodeURIComponent(id)}`);
      if (r.ok) {
        const detail = (await r.json()) as Detail;
        setDetails((d) => ({ ...d, [id]: detail }));
      }
    }
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">Clinical Lookup</h1>
          <p className="text-sm text-slate-500">Type one or more findings — informational, physician review only.</p>
        </div>
        <Link href="/patients" className="text-sm text-slate-500 transition hover:text-slate-900">← Patients</Link>
      </header>

      <div className="glass mb-6 space-y-2 rounded-2xl p-3">
        <input value={q} onChange={(e) => setQ(e.target.value)} autoFocus
          placeholder="e.g. chest pain + sweating + breathless"
          className="field w-full" />
        <div className="flex gap-2">
          <input value={age} onChange={(e) => setAge(e.target.value)} inputMode="numeric"
            placeholder="Age (optional)" className="field w-40" />
          <select value={sex} onChange={(e) => setSex(e.target.value)} className="field w-40">
            <option value="">Sex (optional)</option>
            <option value="male">Male</option>
            <option value="female">Female</option>
          </select>
          {busy && <span className="self-center text-xs text-slate-400">searching…</span>}
        </div>
      </div>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      {res && res.findings.length > 1 && (
        <p className="mb-3 text-xs text-slate-500">Findings: {res.findings.join(" · ")}</p>
      )}

      {res && (
        <div className="space-y-6">
          {res.red_flags.length > 0 && (
            <section>
              <h2 className="mb-2 text-sm font-semibold text-red-700">⚑ Red flags to consider</h2>
              <div className="glass overflow-hidden rounded-2xl">
                <ul className="divide-y divide-red-100">
                  {res.red_flags.map((c) => (
                    <li key={c.condition_id} className="flex items-center justify-between px-5 py-3 text-sm">
                      <span className="font-medium text-red-800">{c.name}</span>
                      <span className="text-xs text-red-500">{c.specialty}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </section>
          )}

          <section>
            <h2 className="mb-2 text-sm font-semibold text-slate-700">Candidate conditions</h2>
            <div className="glass overflow-hidden rounded-2xl">
              {res.candidate_conditions.length === 0 ? (
                <p className="p-6 text-sm text-slate-400">No matches. Try a different phrase.</p>
              ) : (
                <ul className="divide-y divide-slate-200/70">
                  {res.candidate_conditions.map((c) => (
                    <li key={c.condition_id} className={c.applies ? "" : "opacity-60"}>
                      <button onClick={() => toggle(c.condition_id)}
                        className="flex w-full items-start justify-between gap-3 px-5 py-3.5 text-left transition hover:bg-slate-50/60">
                        <span className="min-w-0">
                          <span className="flex flex-wrap items-center gap-2">
                            <span className="font-medium text-slate-900">{c.name}</span>
                            {c.cant_miss && <span className="rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-semibold text-red-700">RED FLAG</span>}
                            {c.findings_matched > 1 && <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-semibold text-blue-700">{c.findings_matched} findings</span>}
                            {!c.applies && <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700">may not apply</span>}
                          </span>
                          <span className="mt-0.5 block text-xs text-slate-400">
                            matched: {c.matched_via.map((m) => m.label).join(", ")}
                            {c.applies_note ? ` · ${c.applies_note}` : ""}
                          </span>
                        </span>
                        <span className="shrink-0 text-xs text-slate-400">{c.specialty}</span>
                      </button>

                      {openId === c.condition_id && (
                        <div className="border-t border-slate-200/70 bg-slate-50/50 px-5 py-4 text-sm">
                          {!details[c.condition_id] ? (
                            <p className="text-slate-400">Loading…</p>
                          ) : (
                            <ConditionDetail d={details[c.condition_id]} />
                          )}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>

          <p className="text-xs text-slate-400">Informational only. Not a diagnosis. The physician remains the decision-maker.</p>
        </div>
      )}
    </main>
  );
}

function Block({ title, items, tone = "slate" }: { title: string; items: string[]; tone?: "slate" | "red" }) {
  if (!items || items.length === 0) return null;
  const color = tone === "red" ? "text-red-700" : "text-slate-700";
  return (
    <div className="mb-3">
      <p className={`mb-1 text-xs font-semibold ${color}`}>{title}</p>
      <div className="flex flex-wrap gap-1.5">
        {items.map((x, i) => (
          <span key={i} className={`rounded-full px-2.5 py-1 text-xs ${tone === "red" ? "bg-red-100 text-red-700" : "bg-white text-slate-600 border border-slate-200"}`}>{x}</span>
        ))}
      </div>
    </div>
  );
}

function ConditionDetail({ d }: { d: Detail }) {
  return (
    <div>
      <Block title="Red flags" items={d.red_flags} tone="red" />
      <Block title="Key symptoms" items={d.symptoms} />
      <Block title="Signs" items={d.signs} />
      <Block title="Mandatory investigations" items={d.investigations} />
      {d.followup.length > 0 && (
        <div className="mb-2">
          <p className="mb-1 text-xs font-semibold text-slate-700">Follow-up</p>
          <ul className="list-disc pl-5 text-xs text-slate-600">
            {d.followup.map((f, i) => <li key={i}>{f}</li>)}
          </ul>
        </div>
      )}
      {d.source && <p className="mt-2 text-[11px] text-slate-400">Source: {d.source}</p>}
    </div>
  );
}
