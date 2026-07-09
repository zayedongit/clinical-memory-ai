"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "../../lib/supabaseClient";
import { apiGet, apiPost } from "../../lib/api";

type Me = { user_id: string; clinic_id: string; role: string; clinic_name: string | null };
type Patient = { id: string; name: string; gender: string | null; phone: string | null; created_at: string | null };

export default function PatientsPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [me, setMe] = useState<Me | null>(null);
  const [needsOnboarding, setNeedsOnboarding] = useState(false);
  const [patients, setPatients] = useState<Patient[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [clinicName, setClinicName] = useState("");
  const [userName, setUserName] = useState("");

  const [pName, setPName] = useState("");
  const [pGender, setPGender] = useState("");
  const [pPhone, setPPhone] = useState("");

  const loadPatients = useCallback(async () => {
    const res = await apiGet("/patients");
    if (res.ok) setPatients((await res.json()).items ?? []);
  }, []);

  const loadMe = useCallback(async () => {
    const res = await apiGet("/me");
    if (res.status === 403) {
      setNeedsOnboarding(true);
      setLoading(false);
      return;
    }
    if (res.ok) {
      setMe(await res.json());
      setNeedsOnboarding(false);
      await loadPatients();
    } else {
      setError(`Could not load account (${res.status}).`);
    }
    setLoading(false);
  }, [loadPatients]);

  useEffect(() => {
    (async () => {
      const { data } = await supabase.auth.getSession();
      if (!data.session) return router.push("/login");
      await loadMe();
    })();
  }, [router, loadMe]);

  async function onboard(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const res = await apiPost("/clinics/bootstrap", { clinic_name: clinicName, user_name: userName });
    if (res.ok) {
      setLoading(true);
      await loadMe();
    } else setError(`Onboarding failed (${res.status}).`);
  }

  async function addPatient(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const res = await apiPost("/patients", { name: pName, gender: pGender || null, phone: pPhone || null });
    if (res.ok) {
      setPName(""); setPGender(""); setPPhone("");
      await loadPatients();
    } else setError(`Create failed (${res.status}).`);
  }

  async function signOut() {
    await supabase.auth.signOut();
    router.push("/login");
  }

  if (loading) return <main className="p-10 text-sm text-slate-500">Loading…</main>;

  if (needsOnboarding) {
    return (
      <main className="flex min-h-screen items-center justify-center px-4">
        <form onSubmit={onboard} className="glass w-full max-w-sm space-y-5 rounded-2xl p-8">
          <div className="space-y-1">
            <h1 className="text-lg font-semibold tracking-tight text-slate-900">Set up your clinic</h1>
            <p className="text-sm text-slate-500">One-time setup for your account.</p>
          </div>
          <div className="space-y-3">
            <input required placeholder="Clinic name" value={clinicName}
              onChange={(e) => setClinicName(e.target.value)} className="field" />
            <input required placeholder="Your name" value={userName}
              onChange={(e) => setUserName(e.target.value)} className="field" />
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <button className="btn-primary w-full">Create clinic</button>
        </form>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">Patients</h1>
          <p className="text-sm text-slate-500">{me?.clinic_name}</p>
        </div>
        <button onClick={signOut} className="text-sm text-slate-500 transition hover:text-slate-900">Sign out</button>
      </header>

      <form onSubmit={addPatient} className="glass mb-5 flex flex-wrap items-center gap-2 rounded-2xl p-3">
        <input required placeholder="Full name" value={pName} onChange={(e) => setPName(e.target.value)}
          className="field flex-1 min-w-[160px]" />
        <select value={pGender} onChange={(e) => setPGender(e.target.value)}
          className="field w-32">
          <option value="">Gender</option>
          <option value="male">Male</option>
          <option value="female">Female</option>
          <option value="other">Other</option>
        </select>
        <input placeholder="Phone" value={pPhone} onChange={(e) => setPPhone(e.target.value)}
          className="field w-36" />
        <button className="btn-primary shrink-0">Add patient</button>
      </form>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      <div className="glass overflow-hidden rounded-2xl">
        {patients.length === 0 ? (
          <p className="p-6 text-sm text-slate-400">No patients yet. Add your first above.</p>
        ) : (
          <ul className="divide-y divide-slate-200/70">
            {patients.map((p) => (
              <li key={p.id} className="flex items-center justify-between px-5 py-3.5">
                <span className="font-medium text-slate-900">{p.name}</span>
                <span className="text-sm text-slate-500">
                  {[p.gender, p.phone].filter(Boolean).join(" · ")}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </main>
  );
}
