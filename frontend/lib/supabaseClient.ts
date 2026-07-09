import { createClient } from "@supabase/supabase-js";

// Browser Supabase client, used only for authentication (login/signup/session).
// All patient data goes through the FastAPI backend, never directly from here.
export const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
);
