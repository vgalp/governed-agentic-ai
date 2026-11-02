# Chart brief accuracy (ADR 023)

300 synthetic charts (seed 7), 4030 cited lines, built through the real tool code.

**Faithfulness: 0 error(s) in 4030 cited lines.** Every line was checked against the row it cites: the ID exists, belongs to the patient, and the line carries the record's text and value.

| check | planted & flagged | flagged, not planted | planted, missed | precision | recall |
|---|---|---|---|---|---|
| medication_reason | 103 | 0 | 0 | 1.000 | 1.000 |
| problem_treatment | 101 | 0 | 0 | 1.000 | 1.000 |
| vitals | 303 | 0 | 0 | 1.000 | 1.000 |

Planted issues: a medication with an empty reason field and no documenting problem; a problem with none of the office's expected medications; the latest value of a vital sign outside the threshold (an older out-of-range reading is planted in every chart and must not be flagged). Charts where the reason field is empty but the problem list documents the medication are planted as non-issues.

What this measures: that the brief is the record and the checks are the lists, as designed. It does not measure whether the office's lists are clinically complete; that is the office's review of chart_brief.yaml.
