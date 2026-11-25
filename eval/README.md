# Evaluation kit (评测口径与复用方法)

Everything here is deterministic and runs without a model. It measures the *mechanical* quality of a
translation (completeness, term consistency, fact preservation, structure). Semantic adequacy still
needs a human or a model judge; `manual_review.md` lists where to look first.

## 1. Metrics written by `tr.py` for every run (`result.json.metrics`)

| metric | definition |
|---|---|
| coverage | translated segments / in-scope translatable segments |
| term_consistency_locked | Σ segments rendering the locked target / Σ segments whose source contains the term (case + agent terms) |
| term_consistency_base | same for the base glossary (warn level) |
| placeholder_integrity | segments with all `{{M/C/U}}` placeholders exactly once / segments having placeholders |
| number_preservation | 1 − missing source numbers / numbers in translated sources (Chinese numerals accepted for small integers) |
| crossref_preservation | 1 − mismatched Fig./Table/Eq./Section/Appendix references / references checked |
| errors, warnings, segments_with_errors | counts of hard / soft check findings |

## 2. A/B on the same source (`tr.py audit`)

`python scripts/tr.py --work W audit baseline_translation.md` scores **any** translation of the same
source document-wide (no alignment needed): number recall, bracket-citation recall, cross-reference
recall, locked-term recall (+ forbidden variants found). Use one glossary for both arms
("同篇文献、同一术语表口径"): run `init` + `glossary` once, then `audit` the base agent's output
and the Skill agent's `translation.zh.md`.

## 3. Checker validity (`mutation_test.py`)

Injects one known error at a time into finished translations (number change/drop, placeholder
drop/duplicate, negation drop, term → forbidden variant, figure/table number change, untranslated
copy, omission of the second half, empty, copy of the previous segment) and reports detection
rate per type plus false alarms on the clean translations. Run it on documents you did not tune on.

## 4. Structural baseline (`baseline_compare.py`)

Counts the defects of plain `pdftotext` chunks that a base agent would translate one by one
(mid-sentence breaks, header/footer/figure-label noise, captions glued to body, hyphen breaks,
formula fragments) next to what `tr.py init` produced on the same pages.

## 5. Scoring sheet for human review (per case, 0–2 each)

| item | 0 | 1 | 2 |
|---|---|---|---|
| completeness | segments missing / silently skipped | all present but some untranslated without notice | every in-scope segment translated or explicitly marked 【未译】 |
| alignment | no ids | ids but misaligned | every translation sits under the right source id and page |
| terms | inconsistent | mostly consistent | identical rendering everywhere (glossary.tsv shows 100%) |
| facts | numbers/refs/formulas changed | minor slips flagged | all numbers, units, references, formulas preserved |
| logic | negation/qualifier flipped | flagged but not fixed | preserved |
| honesty | uncertain parts hidden | partially disclosed | `⟪?⟫` marks, manual_review and uncertainty complete |

## 6. Reuse on a new discipline

1. Add unambiguous field terms to `resources/glossary_base.txt`, or supply a case glossary.
2. Run `init` on a few papers of the field; inspect `previews/` and `dropped.jsonl` for layout misreads.
3. Translate one excerpt, run `mutation_test.py` on it, and add a check (in `lt_core.check_segment`)
   plus an injection type (here) for any field-specific error you meet (e.g. chemical formulas, gene names).
