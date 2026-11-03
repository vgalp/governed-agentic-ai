"""How accurate is the chart brief? (ADR 023) Measured, not estimated.

Generates N synthetic charts (names, problem lists, medications, allergies, vitals, invented with a
fixed seed; no real people), plants documentation issues in some of them, builds each chart's
brief through the real tool code against a real SQLite database, and scores:

  faithfulness  every cited line of every brief is checked against the row it cites: the text
                must be the record's own text and the ID must exist for that patient. A brief
                that cites a record of another patient, or a record that does not exist, or
                states a value the row does not hold, is an error. Expected: 0, because no model
                writes a line; this run is the proof, not the hope.
  checks        for each planted issue (a medication with no reason and no documenting problem;
                a problem with no expected medication; a vital outside the thresholds) whether the
                brief flagged it, and whether it flagged anything that was not planted.
                Reported as precision and recall per check.

    python eval/chart_brief_accuracy.py --charts 200 --seed 7
    python eval/chart_brief_accuracy.py --charts 200 --write eval/results/chart_brief_accuracy.md
"""

import argparse
import random
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "profiles/clinic-assistant/data"))

import build_db  # noqa: E402

from mcp_servers.chart_brief import server as cb  # noqa: E402
from profiles.loader import load_profile  # noqa: E402

FIRST = ["Ada", "Bram", "Chen", "Dalia", "Emeka", "Farah", "Goran", "Hana", "Ivo", "Jun", "Kofi", "Lior",
         "Mira", "Nils", "Oona", "Pavel", "Quinn", "Rosa", "Sami", "Tova", "Uli", "Vera", "Wen", "Yara", "Zed"]
LAST = ["Abara", "Bellweather", "Castellan", "Dorrit", "Evensong", "Fairweather", "Granholm", "Hollis",
        "Ingram", "Jessop", "Kalloway", "Lindqvist", "Marchetti", "Novak", "Okonkwo", "Prentiss", "Quist",
        "Rasmussen", "Stroud", "Tanaka", "Umber", "Vasquez", "Wexler", "Yilmaz", "Zane"]

# Problems and the medications that document/treat them, consistent with chart_brief.yaml's lists.
PAIRS = [("Atrial fibrillation", "Warfarin Sodium 5 MG Oral Tablet", "warfarin"),
         ("Essential hypertension", "Lisinopril 10 MG Oral Tablet", "lisinopril"),
         ("Hypothyroidism", "Levothyroxine Sodium 0.1 MG Oral Tablet", "levothyroxine"),
         ("Type 2 diabetes mellitus", "Metformin Hydrochloride 500 MG Oral Tablet", "metformin"),
         ("Hyperlipidemia", "Atorvastatin 20 MG Oral Tablet", "atorvastatin"),
         ("Gastroesophageal reflux disease", "Omeprazole 20 MG Delayed Release Oral Capsule", "omeprazole")]
OTHER_PROBLEMS = ["Osteoarthritis of knee", "Seasonal allergic rhinitis", "Chronic low back pain"]
ALLERGIES = [("Penicillin", "Rash", "moderate"), ("Sulfonamide antibacterial", "Hives", "mild"), ("Latex", "Itching", "mild")]
VITALS = [("Systolic blood pressure", 100, 138, "mmHg", 150, 175), ("Diastolic blood pressure", 62, 88, "mmHg", 92, 110),
          ("Heart rate", 55, 98, "/min", 104, 130), ("Oxygen saturation", 93, 99, "%", 85, 91),
          ("Body temperature", 36.0, 37.6, "Cel", 38.2, 39.5)]


NAMES = [f"{f} {ln}" for f in FIRST for ln in LAST]           # 625 distinct names


def make_chart(i: int, rng: random.Random) -> dict:
    """One synthetic chart with its planted issues listed."""
    name = NAMES[i]
    pid = f"00000000-0000-4000-8000-{i:012x}"
    chosen = rng.sample(PAIRS, rng.randint(1, 4))
    problems, meds, planted = [], [], []
    for prob, med, key in chosen:
        problems.append(prob)
        kind = rng.random()
        if kind < 0.15:                                    # planted: problem with no expected medication
            planted.append(("problem_treatment", prob))    # (every PAIRS problem is in the office's list)
        elif kind < 0.30:                                  # planted: medication with no reason and no problem
            meds.append((med, None))
            problems.remove(prob)
            planted.append(("medication_reason", med))
        elif kind < 0.45:                                  # reason field empty, but the problem documents it: no flag
            meds.append((med, None))
        else:
            meds.append((med, prob))
    problems += rng.sample(OTHER_PROBLEMS, rng.randint(0, 2))
    allergies = rng.sample(ALLERGIES, rng.randint(0, 2))
    vitals = []
    for kind, lo, hi, units, out_lo, out_hi in VITALS:
        if rng.random() < 0.2:
            value = round(rng.uniform(out_lo, out_hi), 1)
            planted.append(("vitals", kind))
        else:
            value = round(rng.uniform(lo, hi), 1)
        vitals.append((kind, value, units))
    # an older out-of-range reading that must NOT be flagged (only the latest counts)
    old = [(k, round(hi_out, 1), u) for k, lo, hi, u, lo_out, hi_out in VITALS[:1]]
    return {"id": pid, "name": name, "birth_date": f"19{rng.randint(40, 95)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
            "sex": rng.choice("MF"), "city": "Testville", "problems": problems, "meds": meds, "allergies": allergies,
            "vitals": vitals, "old_vitals": old, "planted": planted}


def load(db_path: Path, charts: list[dict]) -> None:
    db = sqlite3.connect(db_path)
    db.executescript(build_db.SCHEMA)
    db.executescript(build_db.CHART_SCHEMA)
    for c in charts:
        db.execute("INSERT INTO patients VALUES (?, ?, ?, ?, ?)", (c["id"], c["name"], c["birth_date"], c["sex"], c["city"]))
        db.executemany("INSERT INTO conditions (patient_id, description, onset, stop) VALUES (?, ?, '2024-01-01', NULL)",
                       [(c["id"], p) for p in c["problems"]])
        db.executemany("INSERT INTO medications (patient_id, encounter_id, start, stop, description, reason) "
                       "VALUES (?, NULL, '2024-02-01', NULL, ?, ?)", [(c["id"], m, r) for m, r in c["meds"]])
        db.executemany("INSERT INTO allergies (patient_id, description, reaction, severity) VALUES (?, ?, ?, ?)",
                       [(c["id"], *a) for a in c["allergies"]])
        db.executemany("INSERT INTO vitals (patient_id, date, description, value, units) VALUES (?, '2026-10-30', ?, ?, ?)",
                       [(c["id"], *v) for v in c["vitals"]])
        db.executemany("INSERT INTO vitals (patient_id, date, description, value, units) VALUES (?, '2026-01-15', ?, ?, ?)",
                       [(c["id"], *v) for v in c["old_vitals"]])
    db.commit()
    db.close()


CITE = re.compile(r"\[([a-z]+-\d+)\]")


def faithful(db: sqlite3.Connection, chart: dict, answer: str, results: list[dict]) -> list[str]:
    """Errors: a cited ID not among the results, or not this patient's, or a line whose value is not the row's."""
    errors = []
    ids = {r["id"] for r in results}
    tables = {"cond": ("conditions", "description"), "med": ("medications", "description"),
              "alg": ("allergies", "description"), "vit": ("vitals", "description"), "pt": ("patients", "name")}
    for line in answer.splitlines():
        for rid in CITE.findall(line):
            if rid not in ids:
                errors.append(f"cites {rid}, not returned")
                continue
            prefix, _, num = rid.partition("-")
            if prefix not in tables:
                continue
            table, col = tables[prefix]
            row = db.execute(f"SELECT patient_id, {col} FROM {table} WHERE id = ?", (num,)).fetchone() if prefix != "pt" \
                else db.execute("SELECT id, name FROM patients WHERE id LIKE ?", ("%" + num,)).fetchone()
            if row is None or (prefix != "pt" and row[0] != chart["id"]):
                errors.append(f"{rid} is not this patient's record")
            elif prefix != "pt" and row[1].lower() not in line.lower():
                errors.append(f"{rid}: line does not carry the record's text ({row[1]!r})")
            if prefix == "vit":
                v = db.execute("SELECT value FROM vitals WHERE id = ?", (num,)).fetchone()[0]
                if f"{v:g}" not in line:
                    errors.append(f"{rid}: value {v:g} not in line")
    return errors


def flagged(answer: str) -> set[tuple[str, str]]:
    found = set()
    checks = answer.split("Checks (what the record does or does not show):", 1)[-1]
    for line in checks.splitlines():
        if line.startswith("- No reason recorded for "):
            found.add(("medication_reason", line[len("- No reason recorded for "):].split(" [")[0]))
        elif " is on the problem list, and none of the medications" in line:
            found.add(("problem_treatment", line[2:].split(" [")[0]))
        elif "the office threshold" in line and line.startswith("- "):
            found.add(("vitals", re.split(r" \d", line[2:], maxsplit=1)[0]))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--charts", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--write", type=Path, help="write the report here (markdown)")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    if args.charts > len(NAMES):
        sys.exit(f"at most {len(NAMES)} charts (distinct names)")
    rng.shuffle(NAMES)
    charts = [make_chart(i, rng) for i in range(args.charts)]
    import tempfile
    db_path = Path(tempfile.mkdtemp()) / "charts.db"
    load(db_path, charts)
    P = load_profile("clinic-assistant")
    cfg = cb.load_config(P)
    allowed = list(P.roles["clinician"].data_classes)
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    errors, tp, fp, fn = [], {}, {}, {}
    lines_total = 0
    for c in charts:
        out = cb.brief(db_path, P, cfg, f"Give me a brief on {c['name']}", allowed)
        a = out["answer"]
        lines_total += sum(1 for ln in a.splitlines() if CITE.search(ln))
        errors += [f"{c['name']}: {e}" for e in faithful(db, c, a, out["results"])]
        got = flagged(a)
        want = set(c["planted"])
        for kind in ("medication_reason", "problem_treatment", "vitals"):
            g = {x for x in got if x[0] == kind}
            w = {x for x in want if x[0] == kind}
            # names in checks carry the medication's full description / the problem / the vital kind
            match = lambda x, s: any(x[1].lower() in y[1].lower() or y[1].lower() in x[1].lower() for y in s)  # noqa: E731
            tp[kind] = tp.get(kind, 0) + sum(1 for x in w if match(x, g))
            fn[kind] = fn.get(kind, 0) + sum(1 for x in w if not match(x, g))
            fp[kind] = fp.get(kind, 0) + sum(1 for x in g if not match(x, w))
    db.close()
    rows = []
    for kind in ("medication_reason", "problem_treatment", "vitals"):
        t, f_p, f_n = tp.get(kind, 0), fp.get(kind, 0), fn.get(kind, 0)
        prec = t / (t + f_p) if t + f_p else 1.0
        rec = t / (t + f_n) if t + f_n else 1.0
        rows.append((kind, t, f_p, f_n, prec, rec))
    report = ["# Chart brief accuracy (ADR 023)", "",
              f"{len(charts)} synthetic charts (seed {args.seed}), {lines_total} cited lines, built through the real tool code.",
              "", f"**Faithfulness: {len(errors)} error(s) in {lines_total} cited lines.** Every line was checked against the "
              "row it cites: the ID exists, belongs to the patient, and the line carries the record's text and value.", ""]
    if errors:
        report += ["Errors:"] + [f"- {e}" for e in errors[:50]] + [""]
    report += ["| check | planted & flagged | flagged, not planted | planted, missed | precision | recall |",
               "|---|---|---|---|---|---|"]
    report += [f"| {k} | {t} | {f_p} | {f_n} | {p:.3f} | {r:.3f} |" for k, t, f_p, f_n, p, r in rows]
    report += ["", "Planted issues: a medication with an empty reason field and no documenting problem; a problem "
               "with none of the office's expected medications; the latest value of a vital sign outside the "
               "threshold (an older out-of-range reading is planted in every chart and must not be flagged). "
               "Charts where the reason field is empty but the problem list documents the medication are planted "
               "as non-issues.", "",
               "What this measures: that the brief is the record and the checks are the lists, as designed. "
               "It does not measure whether the office's lists are clinically complete; that is the office's "
               "review of chart_brief.yaml."]
    text = "\n".join(report)
    print(text)
    if args.write:
        args.write.write_text(text + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
