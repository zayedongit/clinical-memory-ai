/**
 * Prescription safety checks against the patient's own record.
 *
 * **Scope, stated plainly.** These are two narrow checks — does this drug match
 * a documented allergy, and is the patient already on something with the same
 * active ingredient. They are *not* a drug-interaction engine, a renal or
 * hepatic dosing check, or a dose-ceiling check. Presenting them as more than
 * that would be worse than not having them, because a clinician who believes
 * the system checks interactions will stop checking them.
 *
 * **Why the matching is not a substring test.** The original check asked
 * whether either string contained the other:
 *
 *     generic.includes(allergen) || allergen.includes(generic)
 *
 * which fires on any short common substring. "Iron" matches "environmental
 * allergy"; a documented allergy recorded as the sentence "no known drug
 * allergies" matches almost every drug containing the letters "no". False
 * allergy warnings are not harmless — they are the fastest way to teach a
 * prescriber to click through the warning that matters.
 *
 * Matching here is therefore token-based with a minimum token length and a
 * whole-token comparison, plus a small table of ingredient families so that
 * "amoxicillin" is recognised as a penicillin.
 */

/** Ingredient families, so a class allergy catches its members. */
const DRUG_FAMILIES: Record<string, string[]> = {
  penicillin: [
    "penicillin", "amoxicillin", "amoxycillin", "ampicillin", "cloxacillin",
    "flucloxacillin", "piperacillin", "augmentin", "co-amoxiclav",
  ],
  cephalosporin: ["cefixime", "cefuroxime", "ceftriaxone", "cephalexin", "cefpodoxime", "cefaclor"],
  sulfa: ["sulfa", "sulphonamide", "sulfonamide", "cotrimoxazole", "sulfamethoxazole", "trimethoprim"],
  nsaid: ["nsaid", "ibuprofen", "diclofenac", "naproxen", "aceclofenac", "ketorolac", "indomethacin"],
  quinolone: ["ciprofloxacin", "levofloxacin", "ofloxacin", "norfloxacin", "moxifloxacin"],
  macrolide: ["erythromycin", "azithromycin", "clarithromycin"],
  statin: ["atorvastatin", "rosuvastatin", "simvastatin", "pravastatin"],
  ace_inhibitor: ["ramipril", "enalapril", "lisinopril", "perindopril", "captopril"],
};

/** Tokens too generic to base a warning on. */
const STOPWORDS = new Set([
  "drug", "drugs", "allergy", "allergies", "allergic", "tablet", "tablets", "cap",
  "capsule", "syrup", "injection", "mg", "ml", "oral", "and", "the", "none", "known",
  "no", "nil", "daily", "twice", "sos", "bd", "od", "tds", "qid",
]);

/** Shortest token that may trigger a warning on its own. */
const MIN_TOKEN = 4;

export function tokenise(text: string): string[] {
  return (text ?? "")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((t) => t.length >= MIN_TOKEN && !STOPWORDS.has(t));
}

function familiesOf(tokens: string[]): Set<string> {
  const families = new Set<string>();
  for (const [family, members] of Object.entries(DRUG_FAMILIES)) {
    if (tokens.some((t) => members.some((m) => m === t || t.startsWith(m) || m.startsWith(t)))) {
      families.add(family);
    }
  }
  return families;
}

export type SafetyWarning = {
  kind: "allergy_conflict" | "duplicate_therapy";
  message: string;
  /** Exact ingredient match, or a shared drug family. */
  basis: "exact" | "family";
};

/**
 * Check one prescription item against documented allergies and current meds.
 *
 * `allergies` must contain real allergens only. A "no known drug allergies"
 * statement is recorded by the backend as a documented negative and never
 * appears in this list — which is the fix for the check that used to warn on
 * every drug because the sentence itself was stored as an allergen.
 */
export function checkPrescription(
  drug: { generic?: string | null; brand?: string | null },
  allergies: string[],
  currentMedications: string[],
): SafetyWarning[] {
  const name = `${drug.generic ?? ""} ${drug.brand ?? ""}`.trim();
  const drugTokens = tokenise(name);
  if (drugTokens.length === 0) return [];
  const drugFamilies = familiesOf(drugTokens);
  const warnings: SafetyWarning[] = [];

  for (const allergen of allergies) {
    const allergenTokens = tokenise(allergen);
    if (allergenTokens.length === 0) continue;

    if (allergenTokens.some((a) => drugTokens.includes(a))) {
      warnings.push({
        kind: "allergy_conflict",
        message: `Documented allergy to ${allergen}`,
        basis: "exact",
      });
      continue;
    }
    const shared = [...familiesOf(allergenTokens)].filter((f) => drugFamilies.has(f));
    if (shared.length > 0) {
      warnings.push({
        kind: "allergy_conflict",
        message: `Documented allergy to ${allergen} — same ${shared[0].replace(/_/g, " ")} family`,
        basis: "family",
      });
    }
  }

  for (const current of currentMedications) {
    const currentTokens = tokenise(current);
    if (currentTokens.length === 0) continue;
    if (currentTokens.some((c) => drugTokens.includes(c))) {
      warnings.push({
        kind: "duplicate_therapy",
        message: `Already prescribed ${current} — same ingredient`,
        basis: "exact",
      });
    }
  }

  return warnings;
}

/** The single-line form the prescription rows show. */
export function warningLine(
  drug: { generic?: string | null; brand?: string | null },
  allergies: string[],
  currentMedications: string[],
): string | undefined {
  const warnings = checkPrescription(drug, allergies, currentMedications);
  if (warnings.length === 0) return undefined;
  // Allergy first: it is the one that can hurt someone today.
  const allergy = warnings.find((w) => w.kind === "allergy_conflict");
  return (allergy ?? warnings[0]).message;
}
