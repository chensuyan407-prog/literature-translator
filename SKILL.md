---
name: literature-translator
description: Translate a scientific paper (PDF incl. scanned pages, Markdown, LaTeX, DOCX, HTML or text; English↔Chinese) into a paragraph-aligned bilingual result whose every segment points back to its page/block (with per-block boxes for paragraphs that cross columns or pages), with an executable term table (locked translations, forbidden variants, whole-document back-fill), protected formulas/citations/URLs, rebuilt display equations and table grids with crop images, a transcription entry for pages without a text layer, a time-budget scheduler with honest partial results and resume, and automatic checks for numbers (incl. 6 billion = 60 亿), units and counted nouns, figure/table/equation/section references in both directions, negation and qualifier scope, omissions and duplicates. Use for 文献翻译 / 论文翻译 / 双语对照 / 术语表 / 译文核验 tasks, whole papers or a page/section range.
---

# literature-translator 0.3 — 术语锁定、段落对齐、可自检的文献翻译

You (the agent) translate; the scripts do everything that must be exact and repeatable: reading order,
formula/citation protection, table grids, equation rebuilding, the term table, checks, time budget and
deliverables. **Never write translations into result.json by hand — always `tr.py put`. Outputs are
regenerated after every put, so a valid partial result exists at all times.**

`T=/skills/weekly-skill/scripts/tr.py` (or `scripts/tr.py` next to this file). Python ≥3.9; dependencies (`requirements.txt`):
`PyMuPDF>=1.23` for PDFs (1.28 and 2.x give identical segments) and `numpy>=1.22` for layout regions of scanned pages (without numpy every scanned page is
one region — still usable with page+box locators, and the degradation is recorded in result.json); PDFs
(automatic fallback to poppler `pdftotext`). State: `/app/work/lt`; deliverables: `/app/output`
(`--work`/`--out`, or env `LT_WORK`/`LT_OUT`).

## Budget plan (64 requests · 900 s · 16k tokens per response)

| step | requests | what |
|---|---|---|
| 1 | 1 | `cat /input/task.md /input/case.json` |
| 2 | 1 | `python $T init` (+ `--pages/--sections/--target` only if the task restricts them) |
| 2b | 1–3 | only if init prints `SCANNED`: transcribe regions (see below) |
| 3 | 1 | glossary file + `python $T glossary FILE && python $T batch` |
| 4 | 1 per batch | write the batch file + `python $T put FILE --next` |
| 5 | 1–3 | fix `[error]` lines (`put` only corrected blocks); `python $T enforce` if a term changed |
| 6 | 1 | `python $T finalize [--set FIELD=...]` |

After each put a `TIME:` line reports seconds used, measured throughput and the projection. If it says
**TIME WARNING**, keep going with `--next`: the scheduler sizes the next batch to the time left, puts all
remaining titles/headings into one batch first, and prints **STOP** when only the 120 s reserve remains — then
run `finalize` immediately. Untranslated ids, per-kind coverage (body / headings / captions / tables) and a
resume command are written to result.json; never pretend unfinished parts were translated.

## Steps

1. **Read the task**: target language, range (pages/sections/paragraphs), `output_contract`, glossary, questions.
2. **Init** finds the source and any glossary file in case.json / `/input`. Options: `--source FILE`,
   `--password PW`, `--pages 3-5` or `5,6,12`, `--sections "Introduction..Related Work"` / `"Methods|Results"`,
   `--target zh|en`. It prints segment counts, equations that need confirmation, scanned regions, term candidates
   and the terms already locked (case glossary > yours > `resources/glossary_base.txt`).
3. **Scanned pages (no text layer, no OCR).** Each layout region becomes a segment with a crop image
   (`/app/output/crops/S0012.png`; ids and boxes are drawn on `previews/page_NNN.png`). Region ids depend on the
   layout pass, so **address what you read by page and box** (PDF points, as on the preview) and give it a label:
   ```
   @@ p6 [72,90,540,330] #glue
   [table]
   System | MNLI-(m/mm) | QQP
   BERT_{BASE} | 84.6/83.4 | 71.2
   @@ p6 [72,335,540,360] #cap1
   Table 1: GLUE Test results ...
   @@ p6
   REST: NONTEXT running header and page number      (or DONE: the page is complete)
   @@ S0109
   UNREADABLE: lower half smeared        (or NONTEXT: figure)
   ```
   Locators: `@@ S0101..S0103` (region ids), `@@ p6 [x0,y0,x1,y1] #label` (a box; the text keeps that box as its own
   location, also when it lies inside a coarser region), `@@ p6` (whole page). `REST:`/`DONE` closes what is left of
   the page. Run `python $T transcribe FILE` (`--by ocr|human|agent_vision`). Transcribe exactly as printed; blank
   line between paragraphs; `# ` for headings; `[table]`/`[equation]`/`[footnote]` on the first line forces a kind;
   write `⟪?⟫` for any digit or word you cannot read — never guess. Transcribed parts become ordinary segments for
   batch/put; in `put` you may address them as `@@ #glue`. Without numpy the layout pass is skipped: every scanned
   page is one region (`granularity: page`, reason in result.json) and page+box locators work the same. Pages you
   cannot read stay `needs_transcription` with page, box and image; `metrics.scan` reports pages transcribed /
   partially transcribed / not started and regions pending.
4. **Glossary (术语表)** — 15–40 domain terms you are sure of:
   ```
   residual learning => 残差学习
   shortcut connection => 快捷连接 ! 捷径连接|跳跃连接
   ImageNet => =
   ```
   `=> =` keeps the source form; after `!` list wrong renderings (errors; `enforce` replaces them). Singular form;
   plurals and hyphen/space variants match automatically. A case glossary always wins.
5. **Translate batch by batch.** `batch` prints `@@ S0012 [kind · p.3 · col.1 · blocks · §section]` + protected
   source. Answer with the same ids, same order:
   ```
   @@ S0012
   译文……
   ```
   Machine-checked rules: keep every `{{M1}}` (formula), `{{C1}}` (citation/footnote mark), `{{U1}}` (URL) exactly
   once; use the LOCKED TERMS verbatim; keep all numbers (6 billion → 60 亿 is fine), units, counted nouns
   (tokens ≠ documents), Fig./Table/Eq./Section numbers *and kinds* (式（7） must not become 表（7））; keep every
   negation and limiter in its own sentence (not/only/at least/may/approximately; 只有……才); no merging, splitting,
   skipping or repeating; tables keep rows and ` | ` cells; mixed-language segments: translate the foreign
   sentences, keep the rest; unclear phrase → translate and append `⟪?⟫`. Academic register, no additions.
   Save (`cat > /app/work/b01.txt <<'EOF' … EOF`) and run `python $T put /app/work/b01.txt --next`.
6. **Fix.** Re-`put` corrected blocks for every `[error]` (old versions stay in `changes.jsonl`).
   `show S0012` = source, placeholders, translation, issues; `check` (errors) / `issues` (all).
   Warnings (negation_count/scope, qualifier_check, quantifier_added, polarity_check, subject_binding,
   counted_noun_changed, unit_missing, length_ratio …) are hints: fix real problems, leave the rest for
   `manual_review.md`. Errors include table_cell_mismatch (a number moved to another row/column), unit_changed
   (10 MB ≠ 10 兆比特), term_violation (a forbidden variant, even next to the locked term).
7. **Finalize**: `python $T finalize`. Contract fields are mapped by name and **coerced to the declared type**
   (e.g. `verification_notes: list` → a list; coercions listed in `contract_type_coercions`). Fields it cannot
   map are printed: set them with `--set NAME=value` / `--set-json NAME=file.json` (answers to questions, citing
   segment ids such as `S0015 (p.2)`). A value that cannot be given the declared type (e.g. `confidence_level:
   number` while unavailable) is set to null and listed in `contract_gaps` (field, expected, got, reason) — never
   silently a string. Add limits with `--uncertainty "…"`.

## Equations and tables

Display equations are rebuilt from the PDF spans (sub/superscripts, fraction bars from the vector drawing) and kept
untranslated. When the text layer cannot encode them (big ∑/∫ glyphs without Unicode, stacked limits, cases,
matrices) the segment has `eq_status: needs_confirmation`, notes, page/bbox and a crop image `crops/Sxxxx.png`, and
appears in `manual_review.md` — do not "repair" such an equation from memory. Tables anchored by a "Table N"
caption are rebuilt from line boxes (one cell per line; header rows kept inside the table) and also get a crop.

## Outputs (`/app/output`)

| file | content |
|---|---|
| `result.json` | `case_id`, `status` + `status_reason`, `source`, `direction`, `scope`, `metrics` (coverage, `coverage_by_kind`, term consistency, placeholders, numbers, cross-refs, equations needing confirmation, regions needing transcription/unreadable), `segments[]` (id, kind, `location` {pages, bbox, blocks, `boxes` per block, `page_breaks`}, status, source, translation, image, eq_status, source_origin, issues, revisions), `pairs`, `glossary`, `qa`, `verification_notes` (list), `manual_review`, `uncertainty`, `untranslated`, `resume`, `changes`, every `output_contract` field, `contract_type_coercions` |
| `bilingual.md` / `bilingual.html` | paragraph-level alignment with location headers, crop images, checks per segment |
| `translation.<lang>.md`, `alignment.jsonl`, `glossary.tsv`, `qa_report.json`, `manual_review.md` | target text with id anchors; one line per segment; term table with consistency; all issues; human checklist |
| `crops/`, `previews/` | crop per equation/table/scanned region; page images with segment boxes and ids |
| `changes.jsonl`, `dropped.jsonl`, `resume_state/` | every put/revise/enforce/transcribe; layout elements left out (headers, figure labels) and why; state to continue |

Segment statuses: `translated`, `untranslated`, `kept_original` (references, display equations, author block,
numeric tables, text already in the target language, NONTEXT regions — with reason), `needs_transcription`,
`unreadable`, `out_of_scope`.

**Continue later**: `python $T --work /app/output/resume_state session --budget 900` then `batch` as usual. The
resume state carries translations with their revisions and the full `changes.jsonl`; nothing is re-translated.

## Exceptions

- **Encrypted PDF**: `FATAL: encrypted`, status failed; use `--password` if given, otherwise stop and report.
- **Scanned pages**: OCR (tesseract) runs automatically when installed (segments flagged `ocr`); otherwise the transcription flow above. Never invent text.
- **Garbled fonts / unmapped glyphs**: shown as `⟨?⟩`, flagged `unmapped_glyph`, listed for review.
- **No source found**: failed result.json; pass `--source`.
- **Long documents**: follow the scheduler; status `partial` with untranslated ids and resume state.
- **Script crash**: report it with `--uncertainty`; result.json reflects the last good state.

## Verifying a translation from elsewhere

`python $T audit other.md` scores any translation of the same source document-wide (numbers, citations,
cross-references, locked terms). `eval/mutation_test.py` injects known errors and reports what the checks catch.
