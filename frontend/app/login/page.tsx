"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "../../lib/supabaseClient";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const { data, error } =
      mode === "signin"
        ? await supabase.auth.signInWithPassword({ email, password })
        : await supabase.auth.signUp({ email, password });
    setBusy(false);
    if (error) return setError(error.message);
    if (!data.session) {
      setError("Check your email to confirm, then sign in.");
      return;
    }
    router.push("/patients");
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <form onSubmit={submit} className="glass w-full max-w-sm space-y-5 rounded-2xl p-8">
        <div className="space-y-1">
          <div className="mb-3 inline-flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-blue-600 to-indigo-600 text-sm font-bold text-white">
            C
          </div>
          <h1 className="text-lg font-semibold tracking-tight text-slate-900">Clinical Memory AI</h1>
          <p className="text-sm text-slate-500">
            {mode === "signin" ? "Sign in to your clinic" : "Create your account"}
          </p>
        </div>

        <div className="space-y-3">
          <input type="email" required placeholder="Email" value={email}
            onChange={(e) => setEmail(e.target.value)} className="field" />
          <input type="password" required minLength={6} placeholder="Password" value={password}
            onChange={(e) => setPassword(e.target.value)} className="field" />
        </div>

        {error && <p className="text-sm text-red-600">{error}</p>}

        <button type="submit" disabled={busy} className="btn-primary w-full">
          {busy ? "…" : mode === "signin" ? "Sign in" : "Sign up"}
        </button>

        <button type="button" onClick={() => setMode(mode === "signin" ? "signup" : "signin")}
          className="w-full text-center text-sm text-blue-600 hover:text-blue-700">
          {mode === "signin" ? "Need an account? Sign up" : "Have an account? Sign in"}
        </button>
      </form>
    </main>
  );
}
