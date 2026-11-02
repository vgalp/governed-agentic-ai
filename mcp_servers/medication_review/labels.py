"""Drug-label lookups shared by the medication review (ADR 017) and scripts/fetch_drug_labels.py.

Two public, free U.S. government services; no key, no account:
  RxNorm (National Library of Medicine): a medication name or code -> its active ingredients.
  openFDA (Food and Drug Administration): an ingredient -> its most recent prescription label.

What is sent: medication names and RxNorm codes only. Never a patient, never a question.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

RXNAV = "https://rxnav.nlm.nih.gov/REST"
OPENFDA = "https://api.fda.gov/drug/label.json"
FIELDS = ["contraindications", "drug_interactions"]
# Words in a generic name that only name the salt or form, e.g. "warfarin SODIUM".
SALT_WORDS = {"sodium", "potassium", "calcium", "magnesium", "hydrochloride", "hcl", "sulfate", "mesylate",
              "maleate", "besylate", "succinate", "tartrate", "citrate", "acetate", "phosphate", "bromide",
              "xinafoate", "fumarate", "hyclate", "monohydrate", "dihydrate", "trihydrate", "extended-release",
              "delayed-release", "tablets", "tablet", "capsules", "oral", "injection", "and"}


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "governed-agentic-ai/medication-review"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:                      # openFDA: no matching label
                return {}
            if attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(1 + attempt)
    return {}


def _close(a: str, b: str) -> bool:
    """The same word, or a near spelling (same first four letters, length within two)."""
    a, b = a.lower().strip(), b.lower().strip()
    return a == b or (len(a) >= 5 and a[:4] == b[:4] and abs(len(a) - len(b)) <= 2)


def rxnorm(name: str, strict: bool = False) -> tuple[str | None, list[str]]:
    """(RxNorm code, ingredient names) for a medication name. strict: accept the approximate match
    only if one of RxNorm's candidate names is the word itself or a near spelling of it; the drug
    reference uses this to reject ordinary words. (RxNav's score is not a 0-100 similarity.)"""
    found = get(f"{RXNAV}/approximateTerm.json?" + urllib.parse.urlencode({"term": name, "maxEntries": 5}))
    cands = (found.get("approximateGroup") or {}).get("candidate") or []
    if strict:
        cands = [c for c in cands if c.get("name") and _close(c["name"], name)]
    if not cands:
        return None, []
    rxcui = cands[0]["rxcui"]
    rel = get(f"{RXNAV}/rxcui/{rxcui}/related.json?tty=IN")
    names = [p["name"].lower() for g in (rel.get("relatedGroup") or {}).get("conceptGroup") or []
             for p in g.get("conceptProperties") or []]
    return rxcui, sorted(set(names))


def label(ingredient: str, sections: list[str]) -> dict | None:
    """The most recent single-ingredient prescription label for the ingredient that has a
    drug-interaction section (or, failing that, any section we search)."""
    base = [f'openfda.generic_name:"{ingredient}"', 'openfda.product_type:"HUMAN PRESCRIPTION DRUG"']
    for extra in (["_exists_:drug_interactions"], []):
        q = "+AND+".join(urllib.parse.quote(p, safe=':_') for p in base + extra)
        rows = get(f"{OPENFDA}?search={q}&limit=50").get("results") or []
        rows = [r for r in rows if any(r.get(s) for s in sections)]
        if rows:
            break
    if not rows:
        return None

    want = set(ingredient.lower().split())

    def fit(r):   # 2: this ingredient alone (salt words aside) · 1: alone, other words · 0: a combination
        best = 0
        for g in (x.lower() for x in (r.get("openfda") or {}).get("generic_name", [])):
            if ingredient not in g or " and " in g or "," in g:
                continue
            extra = set(re.findall(r"[a-z0-9-]+", g)) - want - SALT_WORDS
            best = max(best, 2 if not extra else 1)
        return best
    rows.sort(key=lambda r: (fit(r), r.get("effective_time", "")), reverse=True)
    names = (rows[0].get("openfda") or {}).get("generic_name", [""])
    if not fit(rows[0]) and max(n.count(",") for n in names) >= 2:
        return None       # only many-ingredient products (e.g. a multivitamin): not this drug's own label
    r, o = rows[0], rows[0].get("openfda") or {}
    return {
        "set_id": r["set_id"],
        "label_id": r.get("id"),
        "effective_time": r.get("effective_time"),
        "title": ((o.get("generic_name") or [ingredient])[0]).title(),
        "brand_names": sorted({b.title() for b in o.get("brand_name", [])} - {ingredient.title()}),
        "classes": sorted({c.replace(" [EPC]", "") for c in o.get("pharm_class_epc", [])}),
        "sections": {s: r[s] for s in sections if r.get(s)},
    }


def rxcui_ingredients(rxcui: str) -> list[str]:
    rel = get(f"{RXNAV}/rxcui/{rxcui}/related.json?tty=IN")
    return sorted({p["name"].lower() for g in (rel.get("relatedGroup") or {}).get("conceptGroup") or []
                   for p in g.get("conceptProperties") or []})




def fetch_into(labels: dict, medications: list[dict], sections: list[str]) -> list[dict]:
    """Look up what the label file lacks for these medications (name, rxcui) and add it, in place.
    Returns what was fetched, for the audit: [{"medication", "ingredients", "labels": [{ingredient,
    set_id, effective_time}]}]. A lookup that fails is skipped (the review says "not checked")."""
    labels.setdefault("medications", {}); labels.setdefault("rxcui", {}); labels.setdefault("ingredients", {})
    fetched = []
    for m in medications:
        name, code = m["name"], m.get("rxcui")
        entry = {"medication": name, "ingredients": [], "labels": []}
        try:
            if code and code not in labels["rxcui"]:
                labels["rxcui"][code] = rxcui_ingredients(code)
                entry["ingredients"] = labels["rxcui"][code]
            elif not code and name not in labels["medications"]:
                code, ings = rxnorm(name)
                labels["medications"][name] = {"rxcui": code, "ingredients": ings}
                entry["ingredients"] = ings
            ings = labels["rxcui"].get(code or "") or (labels["medications"].get(name) or {}).get("ingredients") or []
            for ing in ings:
                if ing in labels["ingredients"]:
                    continue
                lab = label(ing, sections)
                if lab:
                    lab["downloaded"] = date.today().isoformat()
                    labels["ingredients"][ing] = lab
                    entry["labels"].append({"ingredient": ing, "set_id": lab["set_id"],
                                            "effective_time": lab.get("effective_time")})
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            continue        # offline or an odd reply: this drug stays "not checked"
        if entry["ingredients"] or entry["labels"]:
            fetched.append(entry)
    return fetched
