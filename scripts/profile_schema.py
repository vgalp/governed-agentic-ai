"""Write the profile file schemas (profiles/schema.py) as JSON Schema to docs/schemas/ (ADR 026).

    .venv/bin/python scripts/profile_schema.py            # write
    .venv/bin/python scripts/profile_schema.py --check    # exit 1 if docs/schemas/ is out of date

An office's IT can point an editor or a CI job at these files to validate a profile without
running Python. tests/test_schema.py runs --check, so the files cannot drift from the models.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from profiles import schema  # noqa: E402

OUT = ROOT / "docs" / "schemas"


def render() -> dict[str, str]:
    files = {}
    for name, model in schema.FILES.items():
        js = model.model_json_schema()
        js["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        js["title"] = f"{name}.yaml" + (" (each entry)" if name in ("tools", "actions") else "")
        files[f"{name}.schema.json"] = json.dumps(js, indent=2, sort_keys=True) + "\n"
    return files


def main() -> int:
    files = render()
    if "--check" in sys.argv:
        stale = [n for n, text in files.items() if not (OUT / n).exists() or (OUT / n).read_text() != text]
        if stale:
            print("out of date: " + ", ".join(stale) + " (run scripts/profile_schema.py)")
            return 1
        print(f"{len(files)} schema files are current")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for n, text in files.items():
        (OUT / n).write_text(text)
    print(f"wrote {len(files)} files to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
