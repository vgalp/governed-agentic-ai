# Recorded FHIR answers (ADR 036)

One folder per vendor sandbox, in the format of `fhir/recording.py`. `tests/test_fhir_replay.py`
runs the unchanged client, mapping and records search against each folder.

| Folder | Source | Patient |
|---|---|---|
| `epic/` | hand-built from Epic's sandbox answers of 2026-11-12 | invented |
| `oracle/` | hand-built from Oracle Health's open sandbox answers of 2026-11-12 | invented |

`index.json` says which (`"source": "hand-built"` or `"recorded"`). To replace a folder with a real
recording of the vendor's synthetic test patient:

```bash
.venv/bin/python scripts/fhir_record.py --profile clinic-oracle --patient-id 12724065 \
    --patient "Wilm Smart 1990-09-15" --out tests/fixtures/fhir/oracle
.venv/bin/python scripts/fhir_record.py --profile clinic-epic --patient-id <the test patient's id> \
    --patient "Camila Lopez 1987-09-12" --out tests/fixtures/fhir/epic
.venv/bin/python -m pytest tests/test_fhir_replay.py
```

The recorder accepts only vendor sandboxes and localhost, and writes no token or session header.
Review the diff before committing it, as for any test data. Some replay tests check quirks of the
hand-built folders (Oracle Health's two-page vitals, Epic's empty visits); after a real recording,
adjust those to what the sandbox actually holds.
