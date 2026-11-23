#!/usr/bin/env python3
"""Write characterization.json + arm_manifest.json from execution/results/metrics.json and zip the ARM bundle.
Run after src/reproduce.py.   python src/build_bundle.py"""
import hashlib
import json
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "execution", "results")
M = json.load(open(os.path.join(RES, "metrics.json"), encoding="utf-8"))
VERSION = M["version"]


def sha(p):
    return hashlib.sha256(open(os.path.join(ROOT, p), "rb").read()).hexdigest()


def dev(target, actual, ref, metric="absolute_error", tol=None, art="metrics", expl=""):
    if metric == "exact_match":
        score = 1.0 if actual == ref else 0.0
    else:
        d = abs(float(actual) - float(ref))
        t = tol if tol else 1.0
        score = round(max(0.0, 1 - d / t), 4) if d > 0 else 1.0
    out = {"target": target, "metric": metric, "actual_value": actual, "reference_value": ref, "score": score,
           "explanation": expl, "evidence_artifact": art}
    if tol is not None:
        out["tolerance"] = tol
    return out


devs = []
for key, label, art in (("case_example", "ResNet p.1-2 (dev)", "case_example_result"), ("heldout_attention", "Transformer p.1-2 (held-out)", "heldout_result")):
    c = M.get(key)
    if not isinstance(c, dict):
        continue
    devs += [
        dev(f"{label}: coverage", c["coverage"], 1.0, tol=0.1, art=art, expl=f"{c['translated_segments']} translated, {c['untranslated_segments']} untranslated in scope"),
        dev(f"{label}: locked-term consistency", c["term_consistency_locked"], 1.0, tol=0.1, art=art,
            expl=f"{c['term_occurrences_locked']} segment-term occurrences checked against the locked glossary"),
        dev(f"{label}: placeholder integrity (formulas/citations/URLs)", c["placeholder_integrity"], 1.0, tol=0.1, art=art),
        dev(f"{label}: number preservation", c["number_preservation"], 1.0, tol=0.1, art=art),
        dev(f"{label}: cross-reference preservation", c["crossref_preservation"], 1.0, tol=0.1, art=art),
        dev(f"{label}: checker detection rate on injected errors", c["mutation_detection_rate"], 1.0, tol=0.2, art="metrics",
            expl=f"{c['mutations_detected']}/{c['mutations_applied']} single-error mutations caught; by type: {c['mutation_by_type']}"),
        dev(f"{label}: false error flags on the clean translation", c["clean_false_alarm_rate_error"], 0.0, tol=0.2, art="metrics"),
    ]
toy = M["toy_example"]
devs.append(dev("toy example: injected error types detected", len(toy["detected"]), len(toy["injected_error_codes_expected"]), metric="exact_match",
                art="toy_result", expl=f"expected {toy['injected_error_codes_expected']}, detected {toy['detected']}"))
zh = M["zh_en_sample"]
devs.append(dev("zh->en sample: coverage", zh["coverage"], 1.0, tol=0.1, art="zh_en_result", expl=f"direction {zh['direction']}, errors {zh['errors']}"))
exc = M["exceptions"]
for nm, ref in (("encrypted", "failed"), ("scanned_no_ocr", "partial"), ("no_pymupdf_fallback", "partial"), ("missing_source", "failed")):
    e = exc.get(nm)
    if isinstance(e, dict):
        devs.append(dev(f"exception: {nm} -> honest status", e["status"], ref, metric="exact_match", art="metrics", expl=e["status_reason"][:200]))
devs.append(dev("repeat-run determinism (segment ids)", M["determinism"]["identical"], True, metric="exact_match", art="metrics"))
V = M.get("v0.3", {})
if isinstance(V.get("glove"), dict):
    g = V["glove"]
    devs += [dev("GloVe Table 1: data cells equal to the page", int(g["table1_data_cells"].split("/")[0]), 12, tol=12, art="glove_check",
                 expl="3 rows x 4 columns incl. 6.6×10^-5, 7.8×10^-4, 8.5×10^-2"),
             dev("GloVe Table 1 header inside the table (not in body text)", g["table1_header_in_table"] and not g["leaked_table_header"], True,
                 metric="exact_match", art="glove_check"),
             dev("GloVe reading order: p2 S0021 / p5 S0059-61 / S0066-76 / S0075 joins correct", sum(g["joins"].values()), len(g["joins"]),
                 tol=len(g["joins"]), art="glove_check", expl=f"{g['joins']}; v0.2: 0/4"),
             dev("GloVe eqs (1)-(22): rebuilt equal to the page or flagged with crop", int(g["equations_pass"].split("/")[0]), 22, tol=22,
                 art="glove_check", expl=f"rebuilt exactly {g['equations_rebuilt_exactly']} (v0.2: 7); needs_confirmation {g['needs_confirmation']}"),
             dev("GloVe eqs rebuilt exactly (no confirmation needed)", g["equations_rebuilt_exactly"], 22, tol=22, art="glove_check",
                 expl="2-D layouts (cases, aligned rows) and a glyph without Unicode stay needs_confirmation by design")]
for key, lab, ref_note in (("checker_validation_glove", "grader v0.1 set (GloVe)", "v0.1 = 9/14"),
                           ("checker_validation_v2", "grader v0.2 set (GPT-3 pp.10-15)", "v0.2 = 11/16, 1/10 clean with error")):
    v = V.get(key)
    if isinstance(v, dict):
        devs += [dev(f"{lab}: targeted mutations detected", v["detected"], v["total"], tol=v["total"], art="checker_validation", expl=ref_note),
                 dev(f"{lab}: clean controls with error flags", v["clean_with_error"], 0, tol=v["clean_segments"], art="checker_validation"),
                 dev(f"{lab}: clean controls with warning flags", v["clean_with_warn"], 0, tol=v["clean_segments"], art="checker_validation")]
hm = V.get("heldout_attention_mutations")
if isinstance(hm, dict):
    devs += [dev("held-out Transformer pp.8-9 (rules frozen): directed mutations detected", round(hm["detected"] / hm["total"], 4), 0.9, tol=0.9,
                 art="heldout_mutations", expl=f"{hm['detected']}/{hm['total']} ({hm['detected_as_error']} as error); misses: "
                                               f"{[k for k, r in hm['rows'].items() if not r['hit']]}; target ≥ 0.90 NOT met"),
             dev("held-out Transformer pp.8-9: clean segments with error flags", hm["clean_with_error"], 0, tol=hm["clean_segments"], art="heldout_mutations")]
for k, lab in (("heldout_gpt3_clean", "GPT-3 p.3-4"), ("heldout_bert_clean", "BERT text layer p.1-2"), ("heldout_attention_clean", "Transformer p.8-9")):
    h = V.get(k)
    if isinstance(h, dict):
        devs += [dev(f"held-out {lab}: clean segments with error flags", h["with_error"], 0, tol=h["clean_segments"], art="metrics",
                     expl=f"{h['clean_segments']} clean hand translations not used for tuning"),
                 dev(f"held-out {lab}: clean segments with warning flags", h["with_warning"], 0, tol=h["clean_segments"], art="metrics")]
for k, lab in (("scan_bert_numpy", "BERT scanned pp.5,6,12 (numpy)"), ("scan_bert_no_numpy", "BERT scanned (numpy absent, degraded)"),
               ("scan_hubble_numpy", "Hubble 1929 scanned p.3 (numpy)"), ("scan_hubble_no_numpy", "Hubble scanned (numpy absent, degraded)")):
    s = V.get(k)
    if isinstance(s, dict):
        n, t = map(int, s["facts_found"].split("/"))
        sc = s.get("scan_after") or {}
        devs.append(dev(f"{lab}: page facts present after transcription + translation", n, t, tol=t, art="metrics",
                        expl=f"{s['facts_in_translation']}; pages transcribed {sc.get('pages_transcribed')}, not started {sc.get('pages_not_started')}, "
                             f"granularity {sc.get('region_granularity')}, children with own box {s['children_with_own_bbox']}"))
        if "no_numpy" in k:
            devs.append(dev(f"{lab}: degradation reason recorded in result.json", s["degraded_reason_in_result"], True, metric="exact_match", art="metrics"))
eb = V.get("enforce_backfill")
if isinstance(eb, dict):
    devs.append(dev("term back-fill: forbidden variant replaced by tr.py enforce (and logged)", (not eb["variant_left"]) and eb["logged"], True,
                    metric="exact_match", art="metrics", expl=str(eb)))
c = V.get("contract")
if isinstance(c, dict):
    devs.append(dev("output_contract: every declared field typed or reported as a gap", c["all_typed_or_reported"], True, metric="exact_match",
                    art="contract_check", expl=f"gaps {c['contract_gaps']}; coercions {c['contract_type_coercions']}; v0.2: confidence_level string for number"))
g3 = V.get("gpt3_real_run")
if isinstance(g3, dict):
    kb = g3["replay_coverage_by_kind"]
    devs += [dev("GPT-3 75 pages, real translations: finalized inside the 900 s budget", g3["recorded_finalize_elapsed_s"] <= 900, True,
                 metric="exact_match", art="gpt3_real_run", expl=f"finalize at {g3['recorded_finalize_elapsed_s']} s after {g3['puts']} puts; "
                                                                 f"v0.2 B1 {g3['baseline_v0.2_B1']['elapsed_s']} s; {g3['translator']}"),
             dev("GPT-3 real run: headings translated", kb["headings"]["segment_coverage"], 1.0, tol=0.5, art="gpt3_real_run",
                 expl=f"{kb['headings']['segments_done']}/{kb['headings']['segments_total']}"),
             dev("GPT-3 real run: body word coverage", kb["body"]["unit_coverage"], 1.0, tol=1.0, art="gpt3_real_run",
                 expl=f"{g3['replay_translated']}/{g3['in_scope']} segments; by kind {kb}; v0.2 B1 114/563 segments. {g3['caveat']}"),
             dev("GPT-3 real run: hard-check errors on the delivered translation", g3["replay_errors"], 0, tol=5, art="gpt3_real_run",
                 expl=f"error segments {g3['error_segments']} (S0074: header text placed in the wrong row by the translator - true positive)"),
             dev("GPT-3 resume from resume_state: earlier translations unchanged", g3["resume"]["earlier_unchanged"], g3["resume"]["translations_before"],
                 tol=g3["resume"]["translations_before"], art="gpt3_real_run", expl=str(g3["resume"])),
             dev("GPT-3 resume: change log carried over (provenance)", g3["resume"]["session_logged"] and g3["resume"]["change_log_entries"] > g3["resume"]["translations_before"],
                 True, metric="exact_match", art="gpt3_real_run")]

char = {
    "deviations_from_paper": devs,
    "envelope": [
        {"param": "languages", "min": 1, "max": 2, "tested": True, "unit": "en->zh, zh->en"},
        {"param": "columns_per_page", "min": 1, "max": 2, "tested": True},
        {"param": "pages_per_run", "min": 1, "max": 75, "tested": True, "unit": "pages (init); GPT-3 75 pages translated for 723 s in a real run"},
        {"param": "input_formats", "min": 1, "max": 6, "tested": True, "unit": "pdf, md, txt, tex, docx, html (md/pdf exercised in reproduce.py)"},
        {"param": "words_translated_per_batch", "min": 25, "max": 1400, "tested": True, "unit": "words"},
        {"param": "words_translated_in_900s", "min": 0, "max": 23000, "tested": True, "unit": "source words (GPT-3 real run, Claude Opus 5.5 as translator; a slower model translates fewer)"},
    ],
    "failure_modes": [
        {"condition": "scanned page without tesseract", "symptom": "missing_page segment, status failed/partial with reason; no invented text", "evidence_artifact": "metrics"},
        {"condition": "password-protected PDF without --password", "symptom": "status=failed, error encrypted, takeover hint", "evidence_artifact": "metrics"},
        {"condition": "PyMuPDF missing", "symptom": "pdftotext fallback: no bbox, no math-font protection; flagged no_layout_backend", "evidence_artifact": "metrics"},
        {"condition": "complex layouts (3+ columns, wrapped floats)", "symptom": "reading order or joins may be wrong; visible in previews/ and dropped.jsonl", "evidence_artifact": "case_example_preview"},
        {"condition": "semantic mistranslation that keeps numbers/terms/negation", "symptom": "only detected when it swaps values/evaluations between named subjects (subject_binding) or table cells; comparative flips (larger->smaller) and swapped values between parallel conditions are missed on the held-out set", "evidence_artifact": "heldout_mutations"},
        {"condition": "unit changes outside data sizes (TFLOPS->GFLOPS, 天->小时 next to a formula placeholder), dropped approximators (约 40K -> 40K)", "symptom": "not flagged (held-out misses)", "evidence_artifact": "heldout_mutations"},
        {"condition": "scanned page without numpy", "symptom": "one page-level region per page (granularity page); transcription still possible with page+box locators; degradation recorded", "evidence_artifact": "scan_bert_degraded_result"},
        {"condition": "duplicate translation of a very short segment (<15 chars)", "symptom": "duplicate check skips it (1 miss in dev mutation test)", "evidence_artifact": "metrics"},
    ],
    "sensitivity": [],
    "notes": ("No paper figures are reproduced: this is a Skill-authoring task. Targets are acceptance properties of the Skill measured on "
              "example cases built by the author from public papers (ResNet p.1-2 used during development; Transformer p.1-2 held out), a toy "
              "example, a synthetic zh sample and exception inputs. reference_value is the ideal value. Agent translations used as inputs were "
              "written by the developing agent through the Skill workflow; they are not official platform outputs. Baseline structure comparison "
              "(pdftotext chunks vs Skill segments) is in metrics.json -> *.baseline_compare. v0.2 targets come from the grader's "
              "failure report (grading/GRADE_REPORT.md): GloVe Table 1 and equations, scanned BERT/Hubble pages, checker validation set, "
              "GPT-3 long-document budget. v0.3 targets come from grading/v0.2.0/GRADE_REPORT.md: declared numpy + degraded scanned path, "
              "page+box transcription locators, checker fixes validated on the grader's v0.2 set and re-tested on held-out Transformer pp.8-9 "
              "with rules frozen, GloVe reading order and equations (1)-(22), contract type gaps, and a REAL timed GPT-3 run whose translations "
              "were written by the developing agent (Claude Opus 5.5, not the platform's DeepSeek harness; harness requests 未测) plus a real "
              "resume from resume_state. The emulated-clock simulation is kept only for scheduler timing logic."),
}
json.dump(char, open(os.path.join(ROOT, "characterization.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

arts = [("metrics", "execution/results/metrics.json", "metric", "json"),
        ("case_example_result", "execution/results/case_example/output/result.json", "data", "json"),
        ("case_example_bilingual", "execution/results/case_example/output/bilingual.md", "data", "other"),
        ("case_example_preview", "execution/results/case_example/output/previews/page_001.png", "figure", "png"),
        ("heldout_result", "execution/results/heldout_attention/output/result.json", "data", "json"),
        ("toy_result", "execution/results/toy_example/output/result.json", "data", "json"),
        ("zh_en_result", "execution/results/zh_en_sample/output/result.json", "data", "json"),
        ("glove_check", "execution/results/v0.3/glove_check.json", "data", "json"),
        ("checker_validation", "execution/results/v0.3/checker_validation_v2.json", "data", "json"),
        ("checker_validation_glove", "execution/results/v0.3/checker_validation_glove.json", "data", "json"),
        ("heldout_mutations", "execution/results/v0.3/heldout_attention_mutations.json", "data", "json"),
        ("contract_check", "execution/results/v0.3/contract_check.json", "data", "json"),
        ("scan_bert_result", "execution/results/v0.3/scan_bert_numpy/result.json", "data", "json"),
        ("scan_bert_degraded_result", "execution/results/v0.3/scan_bert_no_numpy/result.json", "data", "json"),
        ("scan_hubble_result", "execution/results/v0.3/scan_hubble_numpy/result.json", "data", "json"),
        ("gpt3_real_run", "execution/results/v0.3/gpt3_real_run/run_report.json", "data", "json"),
        ("gpt3_longdoc_sim", "execution/results/v0.3/gpt3_longdoc_sim.json", "data", "json")]
arts = [a for a in arts if os.path.exists(os.path.join(ROOT, a[1]))]
ran_at, wall = open(os.path.join(RES, "ran_at.txt")).read().split()
manifest = {
    "arm_version": "1.1",
    "paper": {"title": "阅读03｜文献翻译 — literature-translator Skill", "challenge_id": "product-arena-01-5a9928c1"},
    "entrypoint": "src/reproduce.py",
    "environment": {"dependencies": "submission/requirements.txt", "python_version": ">=3.9", "gpu_required": False,
                    "extra_packages": ["PyMuPDF>=1.23", "numpy>=1.22", "poppler pdftotext (fallback, optional)", "tesseract (optional, scanned pages)"]},
    "execution": {"log_path": "execution/run.log", "exit_code": 0, "ran_at": ran_at.replace("+00:00", "Z"), "wall_time_s": float(wall), "ran_by": "agent",
                  "artifacts": [{"id": i, "path": p, "type": t, "format": f, "checksum_sha256": sha(p)} for i, p, t, f in arts]},
    "expected_outputs": [{"name": d["target"], "type": "metric", "produced_by": [d["evidence_artifact"]],
                          "comparison_method": "exact_match" if d["metric"] == "exact_match" else "numeric_tolerance"} for d in devs],
    "characterization": {"path": "characterization.json"},
    "trace": "trace/trace.jsonl",
    "skills_used": [{"skill_id": "literature-translator", "version": VERSION}],
    "provenance": {"created_by": "agent", "toolchain": f"Claude Code (claude-opus-5-5) + Python {sys.version.split()[0]} + PyMuPDF + numpy"},
    "handoff": {"status": "partial",
                "next_suggestion": "Held-out directed detection 17/22 < 0.90 target: add compute/time unit equivalence (TFLOPS/GFLOPS, 天/小时), "
                                   "dropped approximators (约/about), comparative polarity (larger/smaller) and swapped values between parallel conditions; "
                                   "publish the GitHub link; rebuild 2-D equations (cases, aligned rows).",
                "modality_notes": {"skills": "Skill lives in submission/ (SKILL.md + scripts + resources + eval + examples); copy of SKILL.md in skills/SKILL.md"}},
}
json.dump(manifest, open(os.path.join(ROOT, "arm_manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
os.makedirs(os.path.join(ROOT, "skills"), exist_ok=True)
shutil.copyfile(os.path.join(ROOT, "submission", "SKILL.md"), os.path.join(ROOT, "skills", "SKILL.md"))
shutil.copyfile(os.path.join(ROOT, "submission", "GITHUB_URL.txt"), os.path.join(ROOT, "GITHUB_URL.txt"))

zp = os.path.join(os.path.dirname(ROOT), f"literature-translator_arm_v{VERSION}.zip")
skip_dirs = {"__pycache__", "_work", "resume_state"}
with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in skip_dirs]
        for fn in fns:
            fp = os.path.join(dp, fn)
            z.write(fp, os.path.relpath(fp, ROOT).replace("\\", "/"))
print(f"characterization: {len(devs)} targets, mean score {sum(d['score'] for d in devs) / len(devs):.3f}")
print(f"bundle: {zp} ({os.path.getsize(zp) // 1024} KB)")
