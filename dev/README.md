# dev/ — reproduction and regression scripts (not part of the installed Skill)

The Skill itself is the repository root (`SKILL.md`, `scripts/`, `resources/`, `eval/`, `examples/`).
This folder holds what was used to build and verify it:

| file | purpose |
|---|---|
| `ITERATION_REPORT_v0.3.md`, `ITERATION_REPORT_v0.2.md` | per-fix changes with before/after numbers, evidence paths and remaining defects |
| `metrics_v0.3.json` | output of `reproduce.py` for v0.3.0 (clean venv with only `requirements.txt`; identical under PyMuPDF 1.28.2 and 2.1) |
| `reproduce.py`, `build_bundle.py` | full reproduction run and ARM bundle builder |
| `tests/check_glove.py` | GloVe Table 1 (12 cells), 4 reading-order joins, equations (1)-(22) against the page |
| `tests/check_scanned.py` | scanned BERT / Hubble pages: page+box transcription, scan coverage, child boxes, numpy-free degraded path |
| `tests/check_contract.py` | output_contract types: coercions and `contract_gaps` |
| `tests/run_validation.py`, `tests/run_validation_v2.py` | the grader's checker sets (14 + 7 on GloVe; 16 + 10 on GPT-3 pp.10-15) |
| `tests/heldout_mutations.py`, `tests/heldout_fp.py` | held-out re-test with frozen rules; false-alarm rate on clean translations |
| `tests/sim_longdoc.py` | emulated-clock run of the time-budget scheduler (timing logic only) |

The scripts were run in the bundle layout (`submission/` = the Skill, `src/`, `tests/`); to rerun them, put the
Skill files under `submission/` and these files under `src/` and `tests/`, and pass the PDFs with
`--resnet/--attention/--glove/--gpt3/--bert-scan/--bert-text/--hubble` (or `LT_*_PDF` environment variables).

Not published here: the real-paper PDFs, run outputs and test fixtures that contain verbatim text, transcriptions,
full translations or page images of those papers (ResNet, Attention Is All You Need, GloVe, BERT, GPT-3, Hubble 1929),
including the batches of the real timed GPT-3 run — they are third-party works.
