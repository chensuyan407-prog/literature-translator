#!/usr/bin/env python3
"""Reproduce every number reported for literature-translator 0.3.0 (v0.1 demos, v0.2 and v0.3 regressions).

    python src/reproduce.py [--resnet PATH] [--attention PATH]

Real-paper PDFs are not redistributed in the bundle; pass their paths (or set LT_RESNET_PDF /
LT_ATTENTION_PDF). Steps whose PDF is missing are skipped and logged as skipped. The agent
translations used in the demos were written by the developing agent (Claude Opus 5.5) through the
Skill workflow and are stored verbatim under execution/results/demo_*/ — they are inputs here, not
re-generated. Writes execution/run.log, execution/results/metrics.json and trace/trace.jsonl.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUB = os.path.join(ROOT, "submission")
TR = os.path.join(SUB, "scripts", "tr.py")
RES = os.path.join(ROOT, "execution", "results")
WORK = os.path.join(ROOT, "execution", "_work")
LOG = os.path.join(ROOT, "execution", "run.log")
TRACE = os.path.join(ROOT, "trace", "trace.jsonl")
# paths to the real-paper PDFs (not redistributed); pass --resnet/--glove/... or LT_*_PDF env vars
LOCAL = {
    "glove": "",
    "gpt3": "",
    "bert_scan": "",
    "bert_text": "",
    "hubble": "",
    "resnet": "",
    "attention": "",
}
log_lines, trace, step = [], [], [0]


def ts():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def add_trace(**kw):
    step[0] += 1
    kw.setdefault("timestamp", ts())
    kw["step_order"] = step[0]
    trace.append(kw)


def log(msg):
    line = msg.rstrip()
    log_lines.append(line)
    print(line)


def run(args, label, env=None):
    cid = f"c{step[0] + 1}"
    add_trace(step_type="tool_call", tool_name="python", tool_call_id=cid, tool_args={"argv": [os.path.relpath(a, ROOT) if os.path.isabs(a) and a.startswith(ROOT) else a for a in args]}, title=label)
    t0 = time.time()
    e = dict(os.environ)
    e.update(env or {})
    e["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True, encoding="utf-8", errors="replace", env=e)
    out = (r.stdout or "").strip()
    log(f"$ {label}")
    for l in out.splitlines()[:60]:
        log("  " + l)
    if r.returncode not in (0, 2):
        log(f"  [exit {r.returncode}] " + (r.stderr or "")[-500:])
    add_trace(step_type="tool_result", tool_call_id=cid, tool_output=out.splitlines()[0][:200] if out else f"exit {r.returncode}",
              duration_s=round(time.time() - t0, 3))
    return r.returncode, out


def tr(work, *a, env=None, label=None):
    return run([TR, "--work", work] + list(a), label or ("tr.py " + " ".join(a[:1])), env=env)


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def read_jsonl(p):
    with open(p, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def demo_case(name, pdf, pages, gl, batch, case_id, metrics):
    """Full workflow on an example case built from a real paper (case.json + task.md + output_contract)."""
    cdir = os.path.join(RES, name)
    inp, out, work = os.path.join(cdir, "input"), os.path.join(cdir, "output"), os.path.join(WORK, name)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(inp, exist_ok=True)
    case = {"case_id": case_id, "note": "EXAMPLE case built by the Skill author from a public paper; not an official platform case",
            "materials": ["paper.pdf"], "pages": pages, "target_language": "zh-CN",
            "task": f"Translate pages {pages} of the paper into Chinese as a paragraph-aligned bilingual result with a term table and checks.",
            "output_contract": {"case_id": "string", "status": "success|partial|failed", "bilingual_pairs": "list of {id,page,source,translation}",
                                "glossary": "list", "qa_checks": "object", "untranslated": "list", "uncertainty": "list", "evidence": "list"}}
    with open(os.path.join(inp, "case.json"), "w", encoding="utf-8") as fh:
        json.dump(case, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(inp, "task.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# Example task\n\nTranslate pages {pages} of `paper.pdf` into Chinese. Keep terms consistent, keep formulas, citations and numbers, and give a bilingual alignment.\n")
    shutil.copyfile(pdf, os.path.join(inp, "paper.pdf"))
    add_trace(step_type="thought", title=f"{name}: run the full Skill workflow",
              body=f"init from case.json (pages {pages}) -> agent glossary -> put agent translation -> finalize -> mutation test -> baseline comparison")
    tr(work, "init", "--case", os.path.join(inp, "case.json"), "--out", out, label=f"{name}: tr.py init --case input/case.json")
    tr(work, "glossary", gl, label=f"{name}: tr.py glossary")
    tr(work, "put", batch, "--verbose", label=f"{name}: tr.py put agent translation")
    tr(work, "finalize", label=f"{name}: tr.py finalize")
    os.remove(os.path.join(inp, "paper.pdf"))  # not redistributed
    with open(os.path.join(inp, "paper.sha256"), "w") as fh:
        fh.write(sha(pdf) + "  paper.pdf (not redistributed)\n")
    res = load(os.path.join(out, "result.json"))
    mut = os.path.join(cdir, "mutation_report.json")
    run([os.path.join(SUB, "eval", "mutation_test.py"), "--work", work, "--out", mut], f"{name}: eval/mutation_test.py")
    cmp_ = os.path.join(cdir, "baseline_compare.json")
    run([os.path.join(SUB, "eval", "baseline_compare.py"), "--work", work, "--pdf", pdf, "--pages", pages, "--out", cmp_], f"{name}: eval/baseline_compare.py")
    m = load(mut)
    metrics[name] = {"status": res["status"], "contract_fields_unfilled": res["contract_fields_unfilled"], **{k: res["metrics"][k] for k in (
        "coverage", "translated_segments", "untranslated_segments", "term_consistency_locked", "term_occurrences_locked", "placeholder_integrity",
        "number_preservation", "crossref_preservation", "errors", "warnings")},
        "mutation_detection_rate": m["overall_detection_rate"], "mutations_applied": m["mutations_applied"], "mutations_detected": m["mutations_detected"],
        "mutation_by_type": {k: v["detection_rate"] for k, v in m["by_type"].items()},
        "clean_false_alarm_rate_error": m["clean_false_alarm_rate_error"], "baseline_compare": load(cmp_)}
    add_trace(step_type="artifact", title=f"{name} result", path=os.path.relpath(os.path.join(out, "result.json"), ROOT).replace("\\", "/"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resnet", default=os.environ.get("LT_RESNET_PDF", LOCAL["resnet"]))
    ap.add_argument("--attention", default=os.environ.get("LT_ATTENTION_PDF", LOCAL["attention"]))
    for k in ("glove", "gpt3", "bert_scan", "bert_text", "hubble"):
        ap.add_argument("--" + k.replace("_", "-"), default=os.environ.get("LT_" + k.upper() + "_PDF", LOCAL[k]))
    a = ap.parse_args()
    t_start = time.time()
    ran_at = ts()
    os.makedirs(RES, exist_ok=True)
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    metrics = {"skill": "literature-translator", "version": "0.3.0", "ran_at": ran_at}
    add_trace(step_type="thought", title="Plan",
              body="Re-run the toy example, two real-paper example cases (one used during development, one held out), "
                   "zh->en, exception cases and a determinism check; collect metrics for characterization.json.")
    # 1 toy example: flawed draft then corrected
    ex = os.path.join(SUB, "examples")
    w = os.path.join(WORK, "toy")
    o = os.path.join(RES, "toy_example", "output")
    tr(w, "init", "--source", os.path.join(ex, "sample.md"), "--out", o, label="toy: tr.py init examples/sample.md")
    tr(w, "glossary", os.path.join(ex, "sample_glossary.txt"), label="toy: tr.py glossary")
    _, draft = tr(w, "put", os.path.join(ex, "sample_translation_with_errors.zh.txt"), "--verbose", label="toy: put flawed draft")
    tr(w, "put", os.path.join(ex, "sample_translation.zh.txt"), label="toy: put corrected translation")
    tr(w, "finalize", label="toy: finalize")
    expected = ["term_violation", "negation_lost", "crossref_mismatch", "qualifier_check", "number_missing"]
    metrics["toy_example"] = {"injected_error_codes_expected": expected, "detected": [c for c in expected if c in draft],
                              "final_status": load(os.path.join(o, "result.json"))["status"]}
    # 2 real papers
    if os.path.exists(a.resnet):
        demo_case("case_example", a.resnet, "1-2", os.path.join(RES, "demo_resnet", "glossary_agent.txt"),
                  os.path.join(RES, "demo_resnet", "batch01.zh.txt"), "example-resnet-p1-2", metrics)
    else:
        log(f"SKIPPED case_example: ResNet PDF not found at {a.resnet}")
        metrics["case_example"] = "skipped (pdf not available)"
    if os.path.exists(a.attention):
        demo_case("heldout_attention", a.attention, "1-2", os.path.join(RES, "demo_attention", "glossary_agent.txt"),
                  os.path.join(RES, "demo_attention", "batch01.zh.txt"), "example-attention-p1-2", metrics)
    else:
        log(f"SKIPPED heldout_attention: PDF not found at {a.attention}")
        metrics["heldout_attention"] = "skipped (pdf not available)"
    # 3 zh -> en on the bundled synthetic Chinese sample
    smp = os.path.join(RES, "samples")
    w, o = os.path.join(WORK, "zh"), os.path.join(RES, "zh_en_sample", "output")
    tr(w, "init", "--source", os.path.join(smp, "single_column_zh.pdf"), "--out", o, label="zh->en: init single_column_zh.pdf")
    tr(w, "put", os.path.join(RES, "demo_zh_en", "batch01.en.txt"), label="zh->en: put")
    tr(w, "finalize", label="zh->en: finalize")
    r = load(os.path.join(o, "result.json"))
    metrics["zh_en_sample"] = {"direction": r["direction"], "status": r["status"], "coverage": r["metrics"]["coverage"], "errors": r["metrics"]["errors"]}
    # 4 exceptions
    add_trace(step_type="thought", title="Exception handling", body="encrypted PDF, scanned page without OCR, missing PyMuPDF, missing source")
    exc = {}
    for nm, src, env in (("encrypted", os.path.join(smp, "encrypted_en.pdf"), None),
                         ("scanned_no_ocr", os.path.join(smp, "scanned_en.pdf"), None),
                         ("no_pymupdf_fallback", os.path.join(smp, "two_column_en.pdf"), {"LT_NO_PYMUPDF": "1"}),
                         ("missing_source", os.path.join(smp, "does_not_exist.pdf"), None)):
        w, o = os.path.join(WORK, nm), os.path.join(RES, "exceptions", nm)
        if not os.path.exists(src) and nm != "missing_source":
            exc[nm] = "skipped (sample not bundled)"
            continue
        code, _ = tr(w, "init", "--source", src, "--out", o, "--no-preview", env=env, label=f"exception {nm}: init")
        rr = load(os.path.join(o, "result.json"))
        exc[nm] = {"exit_code": code, "status": rr["status"], "status_reason": rr["status_reason"],
                   "segments": len(rr["segments"]), "uncertainty": rr["uncertainty"][:3]}
    metrics["exceptions"] = exc
    # 6 v0.3 regressions on real papers (each skipped when its PDF is not available)
    V = {}
    TESTS = os.path.join(ROOT, "tests")
    S = os.path.join(SUB, "scripts")
    out3 = os.path.join(RES, "v0.3")
    os.makedirs(out3, exist_ok=True)
    have = lambda p: p and os.path.exists(p)
    if have(a.glove):
        add_trace(step_type="thought", title="GloVe formulas, Table 1 and reading order",
                  body="Table 1 (12 cells), joins at p2 S0021 / p5 S0059-S0061 / S0066-S0076 / S0075, eqs (1)-(22) against the page")
        run([os.path.join(TESTS, "check_glove.py"), "--scripts", S, "--pdf", a.glove, "--work", os.path.join(WORK, "glove"),
             "--out", os.path.join(out3, "glove_check.json")], "GloVe: tests/check_glove.py")
        g = load(os.path.join(out3, "glove_check.json"))
        V["glove"] = {"table1_data_cells": g["table1"]["data_cells_correct"], "table1_header_in_table": g["table1"].get("header_in_table"),
                      "joins": {k: v for k, v in g["joins"].items() if k != "leaked_table_header"},
                      "leaked_table_header": g["joins"]["leaked_table_header"], "equations_pass": g["equations_pass"],
                      "equations_rebuilt_exactly": g["equations_rebuilt_exactly"],
                      "needs_confirmation": [n for n, e in g["equations"].items() if e.get("status") == "needs_confirmation"]}
        run([os.path.join(TESTS, "run_validation.py"), "--scripts", S, "--pdf", a.glove, "--work", os.path.join(WORK, "val"),
             "--out", os.path.join(out3, "checker_validation_glove.json")], "GloVe: tests/run_validation.py (v0.1 grader set: 14 mutations + 7 clean)")
        vv = load(os.path.join(out3, "checker_validation_glove.json"))
        V["checker_validation_glove"] = {"detected": vv["detected"], "total": vv["total"], "clean_segments": vv["clean_segments"],
                                         "clean_with_error": vv["clean_with_error"], "clean_with_warn": vv["clean_with_warn"]}
    if have(a.gpt3):
        run([os.path.join(TESTS, "run_validation_v2.py"), "--scripts", S, "--pdf", a.gpt3, "--work", os.path.join(WORK, "val2"),
             "--out", os.path.join(out3, "checker_validation_v2.json")], "GPT-3 pp.10-15: tests/run_validation_v2.py (v0.2 grader set: 16 mutations + 10 clean)")
        vv = load(os.path.join(out3, "checker_validation_v2.json"))
        V["checker_validation_v2"] = {"detected": vv["detected"], "total": vv["total"], "clean_segments": vv["clean_segments"],
                                      "clean_with_error": vv["clean_with_error"], "clean_with_warn": vv["clean_with_warn"],
                                      "rows": {r["case"]: {"hit": r["hit"], "errors": r["errors"], "warnings": r["warnings"]} for r in vv["rows"]}}
    if have(a.attention):
        run([os.path.join(TESTS, "heldout_mutations.py"), "--scripts", S, "--pdf", a.attention, "--work", os.path.join(WORK, "hom"),
             "--out", os.path.join(out3, "heldout_attention_mutations.json")], "Transformer pp.8-9: tests/heldout_mutations.py (held out, rules frozen)")
        hm = load(os.path.join(out3, "heldout_attention_mutations.json"))
        V["heldout_attention_mutations"] = {"detected": hm["detected"], "total": hm["total"], "detected_as_error": hm["detected_as_error"],
                                            "clean_segments": hm["clean_segments"], "clean_with_error": hm["clean_with_error"],
                                            "clean_with_warn": hm["clean_with_warn"],
                                            "rows": {r["case"]: {"hit": r["hit"], "errors": r["errors"], "warnings": r["warnings"]} for r in hm["rows"]}}
    for name, pdf, pages, tfile in (("heldout_gpt3", a.gpt3, "3-4", "gpt3_p3-4.zh.txt"), ("heldout_bert", a.bert_text, "1-2", "bert_original_p1-2.zh.txt"),
                                    ("heldout_attention", a.attention, "8-9", "attention_p8-9.zh.txt")):
        if not have(pdf):
            V[name + "_clean"] = "skipped (pdf not available)"
            continue
        w = os.path.join(WORK, name)
        tr(w, "init", "--source", pdf, "--out", w + "_out", "--pages", pages, "--no-preview", "--no-base", label=f"{name}: init p.{pages}")
        tr(w, "put", os.path.join(TESTS, "heldout", tfile), label=f"{name}: put clean held-out translation")
        run([os.path.join(TESTS, "heldout_fp.py"), "--scripts", S, "--work", w, "--label", name,
             "--out", os.path.join(out3, name + "_fp.json")], f"{name}: tests/heldout_fp.py")
        fp = load(os.path.join(out3, name + "_fp.json"))
        V[name + "_clean"] = {"clean_segments": fp["segments"], "with_error": fp["segments_with_error"], "with_warning": fp["segments_with_warning"]}
        if name == "heldout_attention":  # term back-fill: a forbidden variant next to the locked term is rewritten by enforce
            tr(w, "glossary", os.path.join(TESTS, "enforce", "glossary_dropout.txt"), label="enforce: lock dropout => 丢弃 ! 随机失活")
            _, put_out = tr(w, "put", os.path.join(TESTS, "enforce", "attention_S0087_variant.zh.txt"), label="enforce: put S0087 with the forbidden variant")
            _, enf_out = tr(w, "enforce", label="enforce: tr.py enforce")
            t87 = load(os.path.join(w, "translations.json"))["S0087"]
            V["enforce_backfill"] = {"error_before": "term_violation" in put_out, "enforce_output": enf_out.splitlines()[-1] if enf_out else "",
                                     "variant_left": "随机失活" in t87["tgt"], "revision": t87.get("rev"),
                                     "logged": any(c.get("action") == "enforce_glossary" and c.get("seg") == "S0087"
                                                   for c in read_jsonl(os.path.join(w, "changes.jsonl")))}
    for case, pdf in (("bert", a.bert_scan), ("hubble", a.hubble)):
        for np_ in (True, False):
            key = f"scan_{case}_{'numpy' if np_ else 'no_numpy'}"
            if not have(pdf):
                V[key] = "skipped (pdf not available)"
                continue
            o = os.path.join(out3, key)
            if os.path.exists(o):
                shutil.rmtree(o)
            run([os.path.join(TESTS, "check_scanned.py"), "--scripts", S, "--case", case, "--pdf", pdf, "--work", os.path.join(WORK, key),
                 "--out", o, "--report", os.path.join(out3, key + ".json")] + ([] if np_ else ["--no-numpy"]),
                f"{key}: tests/check_scanned.py")
            sc = load(os.path.join(out3, key + ".json"))
            V[key] = {k: sc[k] for k in ("status", "status_reason", "errors", "warnings", "facts_found", "facts_in_translation", "scan_after",
                                         "children_with_own_bbox", "degraded_reason_in_result", "pending_in_scope")}
    run([os.path.join(TESTS, "check_contract.py"), "--scripts", S, "--work", os.path.join(WORK, "contract"),
         "--report", os.path.join(out3, "contract_check.json")], "output_contract types: tests/check_contract.py")
    cc = load(os.path.join(out3, "contract_check.json"))
    V["contract"] = {k: cc[k] for k in ("all_typed_or_reported", "silent_type_violations", "contract_gaps", "contract_type_coercions")}
    if have(a.gpt3):
        # real timed run: the agent's translations are inputs (execution/results/v0.3/gpt3_real_run/batches); the clock of that run is
        # recorded in run_report.json and cannot be replayed. The replay re-applies the same puts to check ids, checks and resume.
        rr_dir = os.path.join(out3, "gpt3_real_run")
        rep = load(os.path.join(rr_dir, "run_report.json"))
        w, o = os.path.join(WORK, "g3replay"), os.path.join(WORK, "g3replay_out")
        tr(w, "init", "--case", os.path.join(TESTS, "cases", "gpt3_case.json"), "--source", a.gpt3, "--out", o, "--no-preview",
           label="GPT-3 real run replay: init (75 pages)")
        tr(w, "glossary", os.path.join(rr_dir, "glossary_agent.txt"), label="GPT-3 replay: glossary")
        for i in range(1, 21):
            tr(w, "put", os.path.join(rr_dir, "batches", f"put{i:02d}.zh.txt"), label=f"GPT-3 replay: put {i:02d}")
        tr(w, "finalize", label="GPT-3 replay: finalize session 1")
        r1 = load(os.path.join(o, "result.json"))
        rs = os.path.join(WORK, "g3resume")
        shutil.copytree(os.path.join(o, "resume_state"), rs)
        tr(rs, "session", "--budget", "900", "--out", os.path.join(WORK, "g3resume_out"), label="GPT-3 replay: resume session 2 from resume_state")
        tr(rs, "put", os.path.join(rr_dir, "batches", "resume_put01.zh.txt"), label="GPT-3 replay: put after resume")
        tr(rs, "finalize", label="GPT-3 replay: finalize session 2")
        r2 = load(os.path.join(WORK, "g3resume_out", "result.json"))
        t1, t2 = load(os.path.join(w, "translations.json")), load(os.path.join(rs, "translations.json"))
        ch = read_jsonl(os.path.join(WORK, "g3resume_out", "changes.jsonl"))
        import re as _re
        words = lambda t: len(_re.findall(r"[A-Za-z][A-Za-z'\-]*|\d+(?:\.\d+)?", t or ""))
        by_kind = {}
        for s in r1["segments"]:
            if s["status"] in ("translated", "untranslated"):
                d = by_kind.setdefault(s["kind"], {"segments_done": 0, "segments_total": 0, "words_done": 0, "words_total": 0})
                d["segments_total"] += 1
                d["words_total"] += words(s.get("source"))
                if s["status"] == "translated":
                    d["segments_done"] += 1
                    d["words_done"] += words(s.get("source"))
        V["gpt3_real_run"] = {"recorded_finalize_elapsed_s": rep["finalize_elapsed_s"], "budget_s": 900, "puts": len(rep["puts"]),
                              "recorded_status": rep["result"]["status"], "translator": rep["translator"],
                              "replay_status": r1["status"], "replay_translated": r1["metrics"]["translated_segments"],
                              "in_scope": r1["metrics"]["translated_segments"] + r1["metrics"]["untranslated_segments"],
                              "replay_coverage_by_kind": r1["metrics"]["coverage_by_kind"], "source_words_by_kind": by_kind,
                              "replay_errors": r1["metrics"]["errors"], "replay_warnings": r1["metrics"]["warnings"],
                              "error_segments": sorted({sid for sid, iss in load(os.path.join(o, "qa_report.json"))["issues"].items()
                                                        if any(i["severity"] == "error" for i in iss)}),
                              "untranslated_listed": len(r1["untranslated"]), "resume_command": r1["resume"]["command"],
                              "resume": {"translations_before": len(t1), "translations_after": len(t2),
                                         "earlier_unchanged": sum(1 for k in t1 if t1[k].get("tgt") == t2.get(k, {}).get("tgt")
                                                                  and t1[k].get("rev") == t2.get(k, {}).get("rev")),
                                         "change_log_entries": len(ch), "session_logged": any(c.get("action") == "session" for c in ch),
                                         "status_after": r2["status"], "translated_after": r2["metrics"]["translated_segments"]},
                              "baseline_v0.2_B1": rep["baseline_v0.2_B1"], "caveat": rep["caveat"]}
        case = os.path.join(TESTS, "cases", "gpt3_case.json")
        run([os.path.join(TESTS, "sim_longdoc.py"), "--scripts", S, "--case", case, "--pdf", a.gpt3,
             "--work", os.path.join(WORK, "g3sim"), "--out", os.path.join(WORK, "g3sim_out"),
             "--reuse", os.path.join(TESTS, "longdoc", "gpt3_reuse_alignment.jsonl"), "--log", os.path.join(out3, "gpt3_longdoc_sim.json")],
            "GPT-3 75 pages: tests/sim_longdoc.py (emulated clock, scheduler logic only)")
        sim = load(os.path.join(out3, "gpt3_longdoc_sim.json"))["report"]
        V["gpt3_longdoc_sim"] = {k: sim[k] for k in ("emulated_seconds_at_finalize", "budget", "status", "coverage", "verification_notes_type")}
    metrics["v0.3"] = V
    # 5 determinism
    w1, w2 = os.path.join(WORK, "det1"), os.path.join(WORK, "det2")
    src = os.path.join(smp, "two_column_en.pdf")
    tr(w1, "init", "--source", src, "--out", os.path.join(WORK, "det1_out"), "--no-preview", label="determinism run 1")
    tr(w2, "init", "--source", src, "--out", os.path.join(WORK, "det2_out"), "--no-preview", label="determinism run 2")
    metrics["determinism"] = {"segments_sha256_run1": sha(os.path.join(w1, "segments.jsonl")),
                              "segments_sha256_run2": sha(os.path.join(w2, "segments.jsonl"))}
    metrics["determinism"]["identical"] = metrics["determinism"]["segments_sha256_run1"] == metrics["determinism"]["segments_sha256_run2"]
    metrics["wall_time_s"] = round(time.time() - t_start, 2)
    with open(os.path.join(RES, "metrics.json"), "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, ensure_ascii=False, indent=1)
    add_trace(step_type="artifact", title="metrics", path="execution/results/metrics.json")
    add_trace(step_type="decision", title="Done", body=f"metrics written; wall time {metrics['wall_time_s']} s; see characterization.json for the scored targets")
    log(f"DONE wall_time_s={metrics['wall_time_s']}")
    with open(LOG, "w", encoding="utf-8") as fh:
        fh.write("\n".join(log_lines) + "\n")
    os.makedirs(os.path.dirname(TRACE), exist_ok=True)
    with open(TRACE, "w", encoding="utf-8") as fh:
        for t in trace:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")
    shutil.rmtree(WORK, ignore_errors=True)
    with open(os.path.join(RES, "ran_at.txt"), "w") as fh:
        fh.write(f"{ran_at}\n{metrics['wall_time_s']}\n")


if __name__ == "__main__":
    main()
