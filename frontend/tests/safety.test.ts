/**
 * Prescription safety checks.
 *
 * These matter more than their size suggests. The check that shipped before
 * asked whether either string contained the other, which produced allergy
 * warnings on unrelated drugs — and a prescriber who learns to click through
 * allergy warnings will click through the real one too. Every case here is
 * either a conflict that must fire or a false positive that must not.
 */
import { describe, expect, it } from "vitest";
import { checkPrescription, tokenise, warningLine } from "../lib/safety";

describe("allergy conflicts", () => {
  it("flags an exact ingredient match", () => {
    const warnings = checkPrescription({ generic: "amoxicillin" }, ["amoxicillin"], []);
    expect(warnings).toHaveLength(1);
    expect(warnings[0].kind).toBe("allergy_conflict");
    expect(warnings[0].basis).toBe("exact");
  });

  it("flags a member of an allergic drug family", () => {
    // "Allergic to penicillin" must catch amoxicillin. This is the case a
    // string comparison misses entirely and a clinician would not.
    const warnings = checkPrescription({ generic: "Amoxicillin", brand: "Mox" }, ["penicillin"], []);
    expect(warnings[0].kind).toBe("allergy_conflict");
    expect(warnings[0].basis).toBe("family");
    expect(warnings[0].message).toContain("penicillin");
  });

  it("flags an NSAID for an ibuprofen allergy", () => {
    const warnings = checkPrescription({ generic: "diclofenac" }, ["ibuprofen"], []);
    expect(warnings.some((w) => w.kind === "allergy_conflict")).toBe(true);
  });

  it("matches a brand name when the generic is missing", () => {
    const warnings = checkPrescription({ brand: "Augmentin" }, ["penicillin"], []);
    expect(warnings.some((w) => w.kind === "allergy_conflict")).toBe(true);
  });

  it("is case and whitespace insensitive", () => {
    expect(checkPrescription({ generic: "  PENICILLIN " }, ["Penicillin"], [])).toHaveLength(1);
  });
});

describe("allergy false positives", () => {
  it("does not flag an unrelated drug", () => {
    expect(checkPrescription({ generic: "paracetamol" }, ["penicillin"], [])).toEqual([]);
  });

  it("does not flag on a short shared substring", () => {
    // The previous two-way substring test fired here.
    expect(checkPrescription({ generic: "iron" }, ["environmental allergens"], [])).toEqual([]);
    expect(checkPrescription({ generic: "cetirizine" }, ["citrus"], [])).toEqual([]);
  });

  it("ignores an allergy field that is really a negative statement", () => {
    // The backend records "no known drug allergies" as a documented negative
    // and never puts it in this list, but the check must be safe even if a
    // caller passes the sentence through.
    for (const negative of ["no known drug allergies", "none", "NKDA", "nil known"]) {
      expect(checkPrescription({ generic: "amoxicillin" }, [negative], [])).toEqual([]);
    }
  });

  it("ignores dose and form words", () => {
    expect(checkPrescription({ generic: "amoxicillin", brand: "500mg tablet" }, ["tablet"], []))
      .toEqual([]);
  });

  it("returns nothing for an unnamed drug", () => {
    expect(checkPrescription({ generic: "", brand: null }, ["penicillin"], [])).toEqual([]);
  });
});

describe("duplicate therapy", () => {
  it("flags the same ingredient already prescribed", () => {
    const warnings = checkPrescription({ generic: "metformin" }, [], ["metformin 500mg"]);
    expect(warnings).toHaveLength(1);
    expect(warnings[0].kind).toBe("duplicate_therapy");
  });

  it("does not flag a different drug in the same list", () => {
    expect(checkPrescription({ generic: "amlodipine" }, [], ["metformin 500mg"])).toEqual([]);
  });
});

describe("presentation", () => {
  it("shows the allergy warning first when both fire", () => {
    // Allergy is the one that can hurt someone today.
    const line = warningLine({ generic: "amoxicillin" }, ["penicillin"], ["amoxicillin 500mg"]);
    expect(line).toContain("allergy");
  });

  it("returns undefined when nothing fires", () => {
    expect(warningLine({ generic: "paracetamol" }, ["penicillin"], ["metformin"])).toBeUndefined();
  });
});

describe("tokenisation", () => {
  it("drops short and generic tokens", () => {
    expect(tokenise("Tab. Amoxicillin 500 mg BD")).toEqual(["amoxicillin"]);
  });

  it("handles empty input", () => {
    expect(tokenise("")).toEqual([]);
    expect(tokenise("   ")).toEqual([]);
  });
});
