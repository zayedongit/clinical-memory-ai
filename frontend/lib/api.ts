import { supabase } from "./supabaseClient";

const BASE = process.env.NEXT_PUBLIC_API_BASE_URL!;

async function headers(): Promise<HeadersInit> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (token) h["Authorization"] = `Bearer ${token}`;
  return h;
}

export async function apiGet(path: string): Promise<Response> {
  return fetch(`${BASE}${path}`, { headers: await headers() });
}

export async function apiPost(path: string, body: unknown): Promise<Response> {
  return fetch(`${BASE}${path}`, {
    method: "POST",
    headers: await headers(),
    body: JSON.stringify(body),
  });
}

export async function apiPatch(path: string, body: unknown): Promise<Response> {
  return fetch(`${BASE}${path}`, {
    method: "PATCH",
    headers: await headers(),
    body: JSON.stringify(body),
  });
}

export async function apiDelete(path: string): Promise<Response> {
  return fetch(`${BASE}${path}`, { method: "DELETE", headers: await headers() });
}

// multipart upload (audio) — do NOT set Content-Type; the browser adds the boundary.
export async function apiUpload(path: string, form: FormData): Promise<Response> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  const h: Record<string, string> = {};
  if (token) h["Authorization"] = `Bearer ${token}`;
  return fetch(`${BASE}${path}`, { method: "POST", headers: h, body: form });
}
