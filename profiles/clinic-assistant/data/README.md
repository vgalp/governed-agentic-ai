# Clinic assistant data

**All data here is synthetic.** It was generated with [Synthea](https://github.com/synthetichealth/synthea)
(Apache-2.0). No real person's information is used anywhere in this project.

## Regenerate the data

Needs Java 17+. The seeds (`-s 42 -cs 42`) make the same patients every time.

```bash
java -jar synthea.jar -p 50 -s 42 -cs 42 --exporter.csv.export=true --exporter.fhir.export=false Massachusetts
uv run python profiles/clinic-assistant/data/build_db.py --csv <synthea>/output/csv
uv run python profiles/clinic-assistant/data/make_sample.py --csv <synthea>/output/csv   # refresh the sample
```

`clinic.db` is built locally and not committed. `sample_csv/` (3 patients, plus 1 deceased to test
that they are left out) is committed for tests and CI.

## Data minimization

The database keeps only what the assistant needs. These synthetic columns are dropped: SSN,
driver's licence, passport, street address, ZIP, coordinates, marital status, race, ethnicity,
birthplace, income, and billing-internal IDs. Data that is never stored cannot leak or reach a model.
The files in `sample_csv/` are unmodified Synthea output; only the database drops these columns.