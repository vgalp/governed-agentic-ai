# 017. Medication review for clinicians, from FDA labels

Status: Accepted · Date: 2026-10-19

## Context

Clinicians want help with one of the slowest parts of a visit: checking a patient's medication list
for interactions. An AI model asked "do these interact?" may invent an interaction, miss one, or turn
the answer into advice ("reduce the dose"). The clinic assistant blocks medication decisions for every
role (rules.yaml), and that must not change.

The question is whether the governance layer can let a clinical tool like this be switched on safely:
facts from an official source, only for the right role, nothing decided for the clinician, and every
lookup audited.

## Decision

1. **A read-only tool, `medication_review`**, for clinicians only (roles.yaml). For the patient named
   in the question it reads the current medication list from the records database, and for every pair
   looks for where one drug's FDA label mentions the other, by ingredient, brand name or FDA drug class,
   in the contraindications and drug-interaction sections (`mcp_servers/medication_review`).
2. **The facts are the FDA label, word for word.** Each finding quotes the label sentence, with the
   label's source ID, version date and DailyMed link. No AI model writes or rewords the answer: the
   tool's text is the answer (`answer: direct` in tools.yaml), and the chat shows "no model".
3. **It decides nothing.** The answer says it is for the clinician's review and not a recommendation.
   A pair the labels do not mention is reported as "not mentioned", with "that does not mean they are
   safe together". A drug with no label on file is listed as not checked. The words for a drug class
   (medication_review.yaml `class_terms`) are names for the same class only.
4. **Decisions stay blocked.** A question that asks for a decision ("should we stop…", "can we
   combine…", "how much…") is blocked by the existing medication rule before any tool runs. The word
   "interact" was removed from the decision words, because asking whether medications interact is a
   review, not a decision. A narrow classifier exemption lets a review request through only when it has
   no decision words; a crisis flag can never be overridden.
5. **Only the right role, only when asked.** The tool is called only for questions that ask for a
   medication review (`when: interaction_review` in tools.yaml), and then alone. A nurse, front desk or
   compliance user who asks gets a fixed reply naming who can (`tool_denied`); nothing is looked up. The
   gateway also checks the role may see medication records, and audits every call.
6. **No network at question time.** The label sections are downloaded once with
   `scripts/fetch_drug_labels.py`: medication names (never anything about a patient) go to RxNorm
   (National Library of Medicine) for ingredients, and ingredients go to openFDA for the most recent
   prescription label with a drug-interaction section. The file records the download date; the answer
   shows it.
7. **Two general features, not special code.** `when` (a tool for some questions only) and
   `answer: direct` (a tool whose source text is the answer) are tools.yaml settings any profile can use.
   The loader requires `when` for a direct answer and a `tool_denied` reply for any triggered tool.

## FDA clinical decision support

Software that supports a health care professional's decision is not a regulated medical device when,
among other criteria, it does not analyse images or signals and lets the professional independently
review the basis for what it shows (FD&C Act section 520(o)(1)(E)). This tool is designed to stay on that
side: it shows FDA labeling text with its source to a clinician, recommends nothing, and leaves the
decision to the clinician. This is a design intention, not a regulatory determination; FDA's current
guidance on clinical decision support must be checked before any use beyond a demonstration.

## Consequences

- A clinician gets the interactions the FDA labels mention, with the exact words and a link to the full
  label, in seconds, and the decision visibly stays with them.
- **Limits:** labels do not list every interaction, and this is text matching, not pharmacology: a
  label that names a class in other words, or an interaction described without naming the drug or
  class, is missed. Matching a class word can also find a sentence that says the combination needs no
  change. That is why every finding is quoted in full context and nothing is called safe. Severity is
  not graded. It is not a substitute for a pharmacist's review or a commercial interaction database.
- The demonstration uses synthetic patients only: the Synthea patients and three hand-written ones
  (`profiles/clinic-assistant/data/demo_patients.yaml`). Not for patient care.
- When the Epic FHIR records source exists (ADR 013), the medication list can come from an EHR sandbox
  through the same tool, with no change to the review.

## Tests

`tests/test_medication_review.py`: label mentions found by name and by class and quoted; every cited
ID returned (grounding passes); unmentioned pairs never called safe; a role without medication records
gets nothing; no label file is said plainly; the tool is called only for review questions and only by
clinicians; decision questions are still blocked; the loader refuses a direct answer without `when`, an
unknown pattern, and a missing `tool_denied` reply; the demo patients are in the database.
