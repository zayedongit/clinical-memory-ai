"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { supabase } from "../../lib/supabaseClient";
import { apiGet } from "../../lib/api";

type Row = { visit_id: string; patient_id: string; patient_name: string | null; uhid: string | null; date: string | null; status: string; mine: boolean; assessment: string };
type Stats = { total: number; completed: number; in_progress: number; today: number };

const COMPLETED = new Set(["approved", "completed"]);
function fmt(d: string | null): string {
  return d ? new Date(d).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "—";
}

export default function Consultations() {
  const router = useRouter();
  const [rows, setRows] = useState<Row[]>([]);
  const [stats, setStats] = useState<Stats>({ total: 0, completed: 0, in_progress: 0, today: 0 });
  const [scope, setScope] = useState<"all" | "mine">("all");
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (sc: string, query: string) => {
    const res = await apiGet(`/consultations?scope=${sc}${query.trim() ? `&q=${encodeURIComponent(query.trim())}` : ""}`);
    if (res.ok) { const d = await res.json(); setRows(d.items || []); setStats(d.stats || { total: 0, completed: 0, in_progress: 0, today: 0 }); }
    setLoading(false);
  }, []);

  useEffect(() => {
    (async () => {
      const { data } = await supabase.auth.getSession();
      if (!data.session) return router.push("/login");
      await load(scope, "");
    })();
  }, [router, load, scope]);

  useEffect(() => {
    const t = setTimeout(() => { void load(scope, q); }, 250);
    return () => clearTimeout(t);
  }, [q, scope, load]);

  if (loading) return <main className="p-10 text-sm text-slate-500">Loading…</main>;

  const card = (label: string, value: number, tone: string, sub?: string) => (
    <div className="glass rounded-2xl p-4">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`mt-1 text-2xl font-semibold ${tone}`}>{value}</p>
      {sub && <p className="text-xs text-slate-400">{sub}</p>}
    </div>
  );

  return (
    <main className="mx-auto max-w-4xl px-4 py-10">
      <header className="mb-6 flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">Consultation History</h1>
          <p className="text-sm text-slate-500">Clinical consultations across your clinic</p>
        </div>
        <Link href="/consult" className="btn-primary">+ New Consultation</Link>
      </header>

      <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {card("Total Consultations", stats.total, "text-slate-900")}
        {card("Completed", stats.completed, "text-emerald-600", `${stats.today} today`)}
        {card("In Progress", stats.in_progress, "text-amber-600")}
        {card("Completed Today", stats.today, "text-emerald-600")}
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search patient, UHID, diagnosis…" className="field min-w-[220px] flex-1" />
        <div className="flex overflow-hidden rounded-xl border border-slate-200">
          <button onClick={() => setScope("all")} className={`px-3 py-2 text-sm ${scope === "all" ? "bg-slate-900 text-white" : "bg-white text-slate-600"}`}>All Clinic</button>
          <button onClick={() => setScope("mine")} className={`px-3 py-2 text-sm ${scope === "mine" ? "bg-slate-900 text-white" : "bg-white text-slate-600"}`}>My Consultations</button>
        </div>
      </div>

      <div className="glass overflow-hidden rounded-2xl">
        {rows.length === 0 ? (
          <p className="p-6 text-sm text-slate-400">No consultations{q ? " match your search" : " yet"}.</p>
        ) : (
          <ul className="divide-y divide-slate-200/70">
            {rows.map((r) => {
              const inProgress = r.status === "in_progress";
              return (
                <li key={r.visit_id} className="flex items-center justify-between gap-3 px-5 py-3.5">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="truncate font-medium text-slate-900">{r.patient_name || "Patient"}</span>
                      {r.uhid && <span className="font-mono text-xs text-slate-400">{r.uhid}</span>}
                    </div>
                    <p className="truncate text-xs text-slate-500">{r.assessment || "No diagnosis recorded"}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-3">
                    <span className={`rounded px-2 py-0.5 text-[10px] font-semibold uppercase ${inProgress ? "bg-amber-100 text-amber-700" : COMPLETED.has(r.status) ? "bg-emerald-100 text-emerald-700" : "bg-slate-100 text-slate-500"}`}>{inProgress ? "In Progress" : "Completed"}</span>
                    <span className="hidden text-xs text-slate-400 sm:inline">{fmt(r.date)}</span>
                    <Link href={`/patients/${r.patient_id}`} className="text-xs font-medium text-slate-500 hover:text-slate-900">Preview</Link>
                    {inProgress && <Link href={`/consult?visit=${r.visit_id}`} className="rounded-lg bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-500">▷ Resume</Link>}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </main>
  );
}
