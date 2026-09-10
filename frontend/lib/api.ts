import { supabase } from "./supabaseClient";

const BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

/**
 * The backend is the authority. Every call carries the Supabase session token,
 * and the server derives the caller's clinic and role from it — the client
 * never asserts either. Client-side checks in this app exist to shape the UI,
 * not to enforce anything.
 */
async function headers(): Promise<Record<string, string>> {
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

// Multipart upload (audio) — do NOT set Content-Type; the browser adds the boundary.
export async function apiUpload(path: string, form: FormData): Promise<Response> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  const h: Record<string, string> = {};
  if (token) h["Authorization"] = `Bearer ${token}`;
  return fetch(`${BASE}${path}`, { method: "POST", headers: h, body: form });
}

/**
 * Turn a failed response into a message a clinician can act on.
 *
 * The backend distinguishes its failures deliberately — 409 means someone else
 * edited this record, 403 means your role may not do this, 429 means slow down —
 * and collapsing them all into "Save failed (409)" throws that away at exactly
 * the moment the user needs to know which one it was.
 */
export async function errorMessage(res: Response): Promise<string> {
  if (res.status === 401) return "Your session has expired. Please sign in again.";
  if (res.status === 403) return "You do not have permission to do that.";
  if (res.status === 409) {
    return "This record was changed by someone else, or is already signed. Reload and try again.";
  }
  if (res.status === 413) return "That file is too large.";
  if (res.status === 429) {
    const retry = res.headers.get("Retry-After");
    return retry
      ? `Too many requests — please wait ${retry}s and try again.`
      : "Too many requests — please slow down and try again.";
  }
  if (res.status === 503) return "The service is temporarily unavailable. Please retry.";

  try {
    const body = await res.json();
    if (typeof body?.detail === "string" && body.detail.trim()) return body.detail;
  } catch {
    // A non-JSON error body is not something to show the user verbatim.
  }
  return `Something went wrong (${res.status}). Nothing was saved.`;
}
