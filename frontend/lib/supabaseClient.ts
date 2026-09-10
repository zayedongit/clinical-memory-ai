import { createClient, type SupabaseClient } from "@supabase/supabase-js";

/**
 * Browser Supabase client, used **only** for authentication (sign-in, sign-up,
 * session). All patient data goes through the FastAPI backend, so that every
 * read and write passes the checks the backend enforces and lands in the audit
 * trail. The browser never queries a clinical table directly.
 *
 * The client is created lazily, on first use, rather than at module load.
 * `createClient` throws when the URL is missing, and these pages are client
 * components that Next still evaluates during prerender — so constructing it at
 * import time made `next build` fail on any checkout without a populated
 * `.env.local`, including CI. Building a project should not require production
 * credentials.
 */
let client: SupabaseClient | null = null;

function missingConfig(): string[] {
  return [
    ["NEXT_PUBLIC_SUPABASE_URL", process.env.NEXT_PUBLIC_SUPABASE_URL],
    ["NEXT_PUBLIC_SUPABASE_ANON_KEY", process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY],
  ]
    .filter(([, value]) => !value)
    .map(([name]) => name as string);
}

export function getSupabase(): SupabaseClient {
  if (client) return client;

  const missing = missingConfig();
  if (missing.length > 0) {
    throw new Error(
      `Supabase is not configured: ${missing.join(", ")} missing. ` +
        `Copy frontend/.env.local.example to .env.local and fill it in.`,
    );
  }

  client = createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL as string,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY as string,
    // Sessions live in the browser; nothing here runs on a server that would
    // need to share them.
    { auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true } },
  );
  return client;
}

/**
 * Convenience proxy so call sites can keep writing `supabase.auth.getSession()`
 * while construction stays deferred to the first actual property access.
 */
export const supabase: SupabaseClient = new Proxy({} as SupabaseClient, {
  get(_target, property, receiver) {
    return Reflect.get(getSupabase(), property, receiver);
  },
}) as SupabaseClient;
