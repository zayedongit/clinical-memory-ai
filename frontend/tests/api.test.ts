/**
 * The API client's error translation.
 *
 * The backend distinguishes its failures deliberately — 409 means someone else
 * signed this visit, 403 means your role may not do this — and the UI used to
 * collapse all of them into "Save failed (409)". That is exactly the moment a
 * clinician needs to know which one it was: one means reload, one means ask a
 * colleague, one means wait.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/supabaseClient", () => ({
  supabase: {
    auth: { getSession: vi.fn(async () => ({ data: { session: { access_token: "test-token" } } })) },
  },
}));

const { apiGet, apiPost, apiUpload, errorMessage } = await import("../lib/api");
const { supabase } = await import("../lib/supabaseClient");

describe("error translation", () => {
  it("tells the user to sign in again on 401", async () => {
    const message = await errorMessage(new Response("", { status: 401 }));
    expect(message).toMatch(/sign in again/i);
  });

  it("explains a permission failure on 403", async () => {
    expect(await errorMessage(new Response("", { status: 403 }))).toMatch(/permission/i);
  });

  it("explains a concurrent edit on 409 rather than showing a status code", async () => {
    const message = await errorMessage(new Response("", { status: 409 }));
    expect(message).toMatch(/changed by someone else|already signed/i);
    expect(message).not.toMatch(/409/);
  });

  it("surfaces the retry delay on 429", async () => {
    const res = new Response("", { status: 429, headers: { "Retry-After": "30" } });
    expect(await errorMessage(res)).toContain("30s");
  });

  it("uses the backend's own message when it supplies one", async () => {
    const res = new Response(JSON.stringify({ detail: "Physician attestation is required." }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
    expect(await errorMessage(res)).toBe("Physician attestation is required.");
  });

  it("never shows a raw non-JSON error body", async () => {
    const res = new Response('relation "patients" does not exist at character 41', { status: 500 });
    const message = await errorMessage(res);
    expect(message).not.toContain("relation");
    expect(message).toMatch(/nothing was saved/i);
  });

  it("falls back cleanly when the body is empty JSON", async () => {
    const res = new Response("{}", { status: 500, headers: { "Content-Type": "application/json" } });
    expect(await errorMessage(res)).toMatch(/something went wrong/i);
  });
});

describe("request construction", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
  });

  it("attaches the session token to every request", async () => {
    await apiGet("/patients");
    const [, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer test-token");
  });

  it("omits the Authorization header when there is no session", async () => {
    vi.mocked(supabase.auth.getSession).mockResolvedValueOnce({ data: { session: null } } as never);
    await apiGet("/patients");
    const [, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("does not set Content-Type on a multipart upload", async () => {
    // The browser has to add the multipart boundary itself; setting the header
    // manually produces a body the server cannot parse.
    const form = new FormData();
    form.append("file", new Blob(["x"]), "a.wav");
    await apiUpload("/scribe/transcribe", form);
    const [, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect((init.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
  });

  it("sends JSON bodies", async () => {
    await apiPost("/scribe/save", { attested: true });
    const [, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.body).toBe('{"attested":true}');
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });
});
