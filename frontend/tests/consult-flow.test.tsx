/**
 * The consultation flow, driven through the real component.
 *
 * These cover the parts of the UI that carry clinical weight: you cannot sign
 * without attesting, safety warnings cannot be dismissed silently, a stale
 * version produces a message the user can act on, and a resumed draft comes
 * back with the version needed for the optimistic lock.
 *
 * The backend enforces all of this independently — these tests are about the
 * physician not being led into a state the backend will then reject.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
// A stable object: Next's useRouter returns one, and returning a fresh object
// per render makes every effect that depends on `router` re-run forever.
const router = { push, replace: vi.fn(), back: vi.fn(), refresh: vi.fn(), prefetch: vi.fn() };

vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => window.location.pathname,
  redirect: vi.fn(),
}));

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

vi.mock("../lib/supabaseClient", () => ({
  supabase: {
    auth: {
      getSession: vi.fn(async () => ({ data: { session: { access_token: "t" } } })),
    },
  },
}));

const PATIENT = {
  id: "p1", name: "Asha Rao", uhid: "CH-2026-000001", gender: "female",
  phone: "9000000001", dob: "1978-01-01", height_cm: 160, weight_kg: 62,
};

type RouteMap = Record<string, () => Response>;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Route table for the fake backend; tests override individual entries. */
function installFetch(overrides: RouteMap = {}) {
  const saves: unknown[] = [];
  const routes: RouteMap = {
    "GET /patients/p1": () => json(PATIENT),
    "GET /patients/p1/memory": () => json({ visit_count: 0, problems: [], allergies: [],
      allergy_status: "not_recorded", current_medications: [], trends: {},
      flagged_metrics: [], recurring_symptoms: [], unresolved: [], since_last: {},
      medication_changes: { current: [], started: [], stopped: [], continued: [], visits_compared: 0 },
      method: {}, disclaimer: "" }),
    "GET /patients/p1/last-visit": () => json({ found: false, prescription: [] }),
    "POST /scribe/risk": () => json({ kind: "documentation_prompt", scored: true, escalate: false,
      probability: 0.04, band: "very_low", hard_criteria_met: [], reasons: [], disclaimer: "d" }),
    "POST /synthesis/decision-support": () => json({ available: false, differential_diagnosis: [],
      must_not_miss: [], investigations: [], treatment: [] }),
    "POST /scribe/save": () => json({ visit_id: "v1", patient_id: "p1", status: "approved", version: 2 }),
    ...overrides,
  };

  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url).replace("http://api.test", "").split("?")[0];
    const key = `${init?.method ?? "GET"} ${path}`;
    if (key === "POST /scribe/save" && init?.body) saves.push(JSON.parse(String(init.body)));
    const handler = routes[key];
    if (!handler) return json({ detail: `unmocked ${key}` }, 404);
    return handler();
  }));

  return { saves };
}

async function loadPage() {
  const mod = await import("../app/consult/page");
  return mod.default;
}

/** Enter a chief complaint in the manual wizard. */
async function enterComplaint(user: ReturnType<typeof userEvent.setup>, text: string) {
  await screen.findByText(/Clinical Consultation/i);
  // Live mode is the default; switch to manual entry to drive the wizard.
  await user.click(await screen.findByRole("button", { name: /Type manually instead/i }));
  await user.click(await screen.findByRole("button", { name: /Continue to Chief Complaints/i }));
  await user.type(screen.getByPlaceholderText(/^Complaint/i), text);
  await user.click(screen.getByRole("button", { name: /^Add$/ }));
}

/** Walk from a fresh consult to the Review & Sign step. */
async function reachReviewStep(user: ReturnType<typeof userEvent.setup>) {
  await enterComplaint(user, "cough");

  await user.click(screen.getByRole("button", { name: /Continue to History/i }));
  await user.click(screen.getByRole("button", { name: /Continue to Examination/i }));
  await user.click(screen.getByRole("button", { name: /Continue to Systemic Examination/i }));
  await user.click(screen.getByRole("button", { name: /Continue to Prescription/i }));

  // Decision support is unavailable in the fake backend, so the consultation
  // takes the "continue without suggestions" path. That path must exist:
  // losing decision support cannot be allowed to block signing a note.
  await user.click(await screen.findByRole("button", { name: /Continue to Review & Sign/i }));
  await screen.findByText(/Physician attestation/i);
}

beforeEach(() => {
  push.mockClear();
  window.history.replaceState({}, "", "/consult?patient=p1");
});

describe("attestation gate", () => {
  it("disables Sign & save until the physician attests", async () => {
    installFetch();
    const Consult = await loadPage();
    const user = userEvent.setup();
    render(<Consult />);
    await reachReviewStep(user);

    const signButton = await screen.findByRole("button", { name: /Sign & save/i });
    expect(signButton).toBeDisabled();

    await user.click(screen.getByRole("checkbox"));
    expect(signButton).toBeEnabled();
  });

  it("sends attested: true and the completed status when signed", async () => {
    const { saves } = installFetch();
    const Consult = await loadPage();
    const user = userEvent.setup();
    render(<Consult />);
    await reachReviewStep(user);

    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: /Sign & save/i }));

    await waitFor(() => expect(saves).toHaveLength(1));
    expect(saves[0]).toMatchObject({ attested: true, status: "completed", patient_id: "p1" });
  });

  it("shows the signed confirmation only after a successful save", async () => {
    installFetch();
    const Consult = await loadPage();
    const user = userEvent.setup();
    render(<Consult />);
    await reachReviewStep(user);

    expect(screen.queryByText(/signed & saved/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: /Sign & save/i }));
    expect(await screen.findByText(/signed & saved/i)).toBeInTheDocument();
  });
});

describe("save failures", () => {
  it("explains a concurrent-edit conflict instead of showing a status code", async () => {
    installFetch({
      "POST /scribe/save": () => json({ detail: "Visit was modified by someone else." }, 409),
    });
    const Consult = await loadPage();
    const user = userEvent.setup();
    render(<Consult />);
    await reachReviewStep(user);

    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: /Sign & save/i }));

    const message = await screen.findByText(/changed by someone else|already signed/i);
    expect(message).toBeInTheDocument();
    expect(screen.queryByText(/signed & saved/i)).not.toBeInTheDocument();
  });

  it("explains a role failure", async () => {
    installFetch({
      "POST /scribe/save": () => json({ detail: "Role 'staff' may not sign a clinical note." }, 403),
    });
    const Consult = await loadPage();
    const user = userEvent.setup();
    render(<Consult />);
    await reachReviewStep(user);

    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: /Sign & save/i }));
    expect(await screen.findByText(/permission/i)).toBeInTheDocument();
  });
});

describe("resuming a draft", () => {
  it("carries the visit version back on save, so the backend can detect a clobber", async () => {
    window.history.replaceState({}, "", "/consult?visit=v9");
    const { saves } = installFetch({
      "GET /visits/v9": () => json({
        id: "v9", patient_id: "p1", status: "in_progress", version: 7,
        note: { wizard: { step: 3, enc: { complaints: [{ text: "cough", duration: "3 days" }],
          hpi: "", past_history: "", allergies: "", medications: "",
          general_exam: "", systemic_exam: "" } } },
      }),
    });
    const Consult = await loadPage();
    const user = userEvent.setup();
    render(<Consult />);

    await screen.findByText(/Physician attestation/i);
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: /Sign & save/i }));

    await waitFor(() => expect(saves).toHaveLength(1));
    expect(saves[0]).toMatchObject({ visit_id: "v9", expected_version: 7 });
  });
});

describe("the escalation prompt", () => {
  it("is shown with its reasons when the backend escalates", async () => {
    installFetch({
      "POST /scribe/risk": () => json({
        kind: "documentation_prompt", scored: true, escalate: true, probability: 0.71,
        band: "high", threshold: 0.11, model_flagged: true,
        hard_criteria_met: ["Chest pain with breathlessness"],
        reasons: [{ label: "Chest pain documented", contribution_pct: 40 }],
        model_performance: { precision: 0.66, recall: 0.82, specificity: 0.9, roc_auc: 0.91 },
        disclaimer: "Physician-review-only prompt. Not a diagnosis.",
      }),
    });
    const Consult = await loadPage();
    const user = userEvent.setup();
    render(<Consult />);

    await enterComplaint(user, "chest pain");

    const panel = await screen.findByLabelText("Escalation prompt", {}, { timeout: 3000 });
    expect(within(panel).getByText(/Chest pain with breathlessness/)).toBeInTheDocument();
    // The reason, its weight, and how good the model is must all be visible:
    // a bare score invites more trust than it has earned.
    expect(within(panel).getByText(/Chest pain documented/)).toBeInTheDocument();
    expect(within(panel).getByText(/40%/)).toBeInTheDocument();
    expect(within(panel).getByText(/Not a diagnosis/i)).toBeInTheDocument();
    expect(within(panel).getByText(/recall 0\.82/)).toBeInTheDocument();
  });

  it("stays hidden when the backend does not escalate", async () => {
    installFetch();
    const Consult = await loadPage();
    const user = userEvent.setup();
    render(<Consult />);

    await enterComplaint(user, "ankle sprain");

    await new Promise((r) => setTimeout(r, 900));
    expect(screen.queryByLabelText("Escalation prompt")).not.toBeInTheDocument();
  });
});

describe("authentication", () => {
  it("redirects to login without a session", async () => {
    const { supabase } = await import("../lib/supabaseClient");
    vi.mocked(supabase.auth.getSession).mockResolvedValueOnce({ data: { session: null } } as never);
    installFetch();
    const Consult = await loadPage();
    render(<Consult />);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/login"));
  });
});

describe("longitudinal memory panel", () => {
  it("renders trends from the analytics series shape the backend returns", async () => {
    // /patients/{id}/memory returns Record<string, SeriesAnalysis>, not a bare
    // point list. Reading `pts.length` on it silently rendered nothing *and*
    // suppressed the "Not enough data yet" fallback, because `undefined < 2`
    // is false — so the panel was blank with no explanation.
    installFetch({
      "GET /patients/p1/memory": () => json({
        visit_count: 5, problems: [], allergies: [], allergy_status: "documented_none",
        current_medications: [], flagged_metrics: ["bp"], recurring_symptoms: [],
        unresolved: [], since_last: {},
        medication_changes: { current: [], started: [], stopped: [], continued: [], visits_compared: 2 },
        trends: {
          bp: {
            metric: "bp", n: 5, latest: 156, direction: "rising", significant: true,
            p_value: 0.0275, step_change: null,
            points: [
              { date: "2026-01-01", value: 128 }, { date: "2026-02-01", value: 134 },
              { date: "2026-03-01", value: 142 }, { date: "2026-04-01", value: 148 },
              { date: "2026-05-01", value: 156 },
            ],
          },
        },
        method: {}, disclaimer: "",
      }),
    });
    const Consult = await loadPage();
    render(<Consult />);

    const panel = await screen.findByText(/Patient memory/i);
    expect(panel).toBeInTheDocument();
    expect(await screen.findByText("156")).toBeInTheDocument();
    expect(screen.getByText(/rising p=0.0275/)).toBeInTheDocument();
    expect(screen.queryByText(/Not enough data yet/)).not.toBeInTheDocument();
  });

  it("shows the fallback when there are not enough readings", async () => {
    installFetch({
      "GET /patients/p1/memory": () => json({
        visit_count: 1, problems: [], allergies: [], allergy_status: "not_recorded",
        current_medications: [], flagged_metrics: [], recurring_symptoms: [],
        unresolved: [], since_last: {},
        medication_changes: { current: [], started: [], stopped: [], continued: [], visits_compared: 1 },
        trends: {
          hr: { metric: "hr", n: 1, latest: 78, direction: "insufficient_data",
                significant: false, p_value: 1, step_change: null,
                points: [{ date: "2026-01-01", value: 78 }] },
        },
        method: {}, disclaimer: "",
      }),
    });
    const Consult = await loadPage();
    render(<Consult />);
    expect(await screen.findByText(/Not enough data yet/)).toBeInTheDocument();
  });
});
