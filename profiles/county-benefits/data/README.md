# County benefits data

**Everything here is fictional.** Maple County Human Services does not exist. The
applicants, cases, amounts and dates in `seed.sql` are invented, and the policies in
`knowledge_base.json` are illustrative samples, not legal or program guidance. They are
not modeled on any real agency.

## Build the database

```bash
sqlite3 profiles/county-benefits/data/county.db < profiles/county-benefits/data/seed.sql
PROFILE=county-benefits ./scripts/dev.sh start
```

`county.db` is built locally and not committed. `seed.sql` is the source.

## Data minimization

The `applicants` table also holds a phone number and a street address. `records.yaml`
does not name those columns, so the records server never reads them and they can never
reach a model. The tests check this.