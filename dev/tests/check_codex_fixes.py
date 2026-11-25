#!/usr/bin/env python3
"""Regression tests for Codex P0/P1 findings on literature-translator v0.3.2."""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = next(
    (cand for base in (os.path.dirname(HERE), os.path.dirname(os.path.dirname(HERE)))
     for cand in (os.path.join(base, "submission", "scripts"), os.path.join(base, "scripts"))
     if os.path.isdir(cand)),
    os.path.join(os.path.dirname(HERE), "scripts"),
)
TR = os.path.join(SCRIPTS, "tr.py")
sys.path.insert(0, SCRIPTS)
import lt_core as C  # noqa: E402
import lt_render as R  # noqa: E402


def run_tr(work, *args):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, TR, "--work", work, *args], capture_output=True, text=True, encoding="utf-8", env=env)


def main():
    results = {}
    with tempfile.TemporaryDirectory(prefix="lt_codex_") as td:
        # 1. P0-1 & P0-2: wrapper contracts, integer 1.5 rejection, missing_custom != [], status -> partial on gaps
        c1 = os.path.join(td, "c1")
        os.makedirs(c1)
        with open(os.path.join(c1, "paper.md"), "w", encoding="utf-8") as fh:
            fh.write("# Title\n\nBaseline beats baseline in this study.\n")
        case1 = {
            "case_id": "wrap-case",
            "source": "paper.md",
            "target_language": "zh",
            "output_fields": {
                "properties": {
                    "summary": {"type": "string"},
                    "int_count": {"type": "integer"},
                    "missing_custom": {"type": "string"},
                },
                "required": ["summary", "int_count", "missing_custom"],
            },
        }
        open(os.path.join(c1, "case.json"), "w", encoding="utf-8").write(json.dumps(case1))
        w1, o1 = os.path.join(td, "w1"), os.path.join(td, "o1")
        r1 = run_tr(w1, "init", "--case", os.path.join(c1, "case.json"), "--out", o1)
        open(os.path.join(c1, "b.txt"), "w", encoding="utf-8").write("@@ S0001\n标题\n@@ S0002\n本研究中基线优于基线。\n")
        run_tr(w1, "put", os.path.join(c1, "b.txt"))
        run_tr(w1, "finalize", "--set", "summary=完成", "--set", "int_count=1.5")
        res1 = json.load(open(os.path.join(o1, "result.json"), encoding="utf-8"))
        gap_fields = {g["field"] for g in res1.get("contract_gaps", [])}
        results["P0_1_2_contract"] = (
            r1.returncode == 0
            and res1.get("int_count") is None
            and "int_count" in gap_fields
            and "missing_custom" in gap_fields
            and res1.get("missing_custom") is None
            and res1.get("status") == "partial"
        )

        # 2. P0-3: explicit small `source` beats bigger `materials` file
        c2 = os.path.join(td, "c2")
        os.makedirs(c2)
        open(os.path.join(c2, "paper.md"), "w", encoding="utf-8").write("# Small Primary\n\nPrimary sentence.\n")
        open(os.path.join(c2, "big_supp.md"), "w", encoding="utf-8").write("# Big Supp\n\n" + ("Filler text. " * 200))
        open(os.path.join(c2, "case.json"), "w", encoding="utf-8").write(json.dumps({
            "case_id": "primary-wins",
            "source": "paper.md",
            "materials": ["big_supp.md"],
            "target_language": "zh",
        }))
        w2, o2 = os.path.join(td, "w2"), os.path.join(td, "o2")
        run_tr(w2, "init", "--case", os.path.join(c2, "case.json"), "--out", o2)
        res2 = json.load(open(os.path.join(o2, "result.json"), encoding="utf-8"))
        results["P0_3_primary_source"] = os.path.basename(res2["source"]["file"]) == "paper.md"

        # 3. P0-5: malformed case.json writes status=failed result.json
        c3 = os.path.join(td, "c3")
        os.makedirs(c3)
        open(os.path.join(c3, "case.json"), "w", encoding="utf-8").write("{broken json")
        w3, o3 = os.path.join(td, "w3"), os.path.join(td, "o3")
        r3 = run_tr(w3, "init", "--case", os.path.join(c3, "case.json"), "--out", o3)
        res3 = json.load(open(os.path.join(o3, "result.json"), encoding="utf-8"))
        results["P0_5_broken_case_result"] = r3.returncode == 2 and res3.get("status") == "failed"

        # 4. P1-3: --budget 1 respects reserve & stops batch + oversized paragraph splits
        c4 = os.path.join(td, "c4")
        os.makedirs(c4)
        huge = " ".join(f"Sentence number {i} evaluates method X." for i in range(600))
        open(os.path.join(c4, "huge.md"), "w", encoding="utf-8").write("# Huge\n\n" + huge + "\n")
        w4, o4 = os.path.join(td, "w4"), os.path.join(td, "o4")
        run_tr(w4, "init", "--source", os.path.join(c4, "huge.md"), "--out", o4, "--budget", "1")
        segs4 = C.read_jsonl(os.path.join(w4, "segments.jsonl"))
        rb4 = run_tr(w4, "batch")
        results["P1_3_budget_and_split"] = (
            len([s for s in segs4 if s["kind"] == "paragraph"]) >= 4
            and max(s["units"] for s in segs4) <= 950
            and "STOP" in rb4.stdout
        )

        # 5. P1-5 & P1-6: duplicate locked term count + mortality/worsen/wrong-language checks
        seg = {"id": "S1", "kind": "paragraph", "prot": "Baseline beats baseline and reduces mortality.", "ph": {}}
        gl = C.compile_glossary([{"source": "baseline", "target": "基线", "keep_original": False, "variants": [], "origin": "agent", "note": ""}])
        iss_dup = C.check_segment(seg, "基线优于参考模型并降低了死亡率。", gl, "en", "zh")
        iss_flip = C.check_segment({"id": "S2", "kind": "paragraph", "prot": "Treatment improves survival.", "ph": {}}, "治疗使生存率恶化。", [], "en", "zh")
        iss_eng = C.check_segment({"id": "S3", "kind": "paragraph", "prot": "We evaluate three models on the dataset.", "ph": {}}, "We tested three different models on the benchmark dataset.", [], "en", "zh")
        results["P1_5_6_semantic_checks"] = (
            any(i["code"] == "term_count_mismatch" for i in iss_dup)
            and any(i["code"] == "polarity_check" for i in iss_flip)
            and any(i["code"] == "wrong_target_language" and i["severity"] == "error" for i in iss_eng)
        )

        # 6. P1-1 & P1-4: mixed text+image PDF + out-of-range page scope
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        c6 = os.path.join(td, "c6")
        os.makedirs(c6)
        pdf6 = os.path.join(c6, "mixed.pdf")
        doc = fitz.open()
        for pno in range(2):
            page = doc.new_page(width=595, height=842)
            page.insert_text((72, 72), f"Page {pno + 1} short intro paragraph mentioning experimental outcomes.", fontsize=11)
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 300, 200), False)
            pix.clear_with(220)
            page.insert_image(fitz.Rect(72, 140, 500, 540), pixmap=pix)
        doc.save(pdf6)
        doc.close()
        w6, o6 = os.path.join(td, "w6"), os.path.join(td, "o6")
        run_tr(w6, "init", "--source", pdf6, "--out", o6)
        res6 = json.load(open(os.path.join(o6, "result.json"), encoding="utf-8"))
        w6b, o6b = os.path.join(td, "w6b"), os.path.join(td, "o6b")
        r6b = run_tr(w6b, "init", "--source", pdf6, "--out", o6b, "--pages", "99")
        res6b = json.load(open(os.path.join(o6b, "result.json"), encoding="utf-8"))
        results["P1_1_4_mixed_and_pages"] = (
            res6["metrics"]["regions_needing_transcription"] >= 2
            and r6b.returncode == 2
            and res6b["status"] == "failed"
        )

        # 7. v0.3.3 P0-1/P0-2/P0-3 + unspaced CJK hard split
        c7 = os.path.join(td, "c7")
        os.makedirs(c7)
        open(os.path.join(c7, "paper.md"), "w", encoding="utf-8").write("# Main\n\nSmall primary text.\n")
        open(os.path.join(c7, "appendix.md"), "w", encoding="utf-8").write("# Appendix\n\n" + ("Big supplement text. " * 250))
        open(os.path.join(c7, "case.json"), "w", encoding="utf-8").write(json.dumps({
            "case_id": 0,
            "materials": [
                {"path": "appendix.md", "role": "supplement"},
                {"path": "paper.md", "role": "primary"},
            ],
            "target_language": "zh",
            "output_fields": {
                "properties": {
                    "summary": {"type": "string"},
                    "opt_extra": {"type": "string"},
                    "review": {"type": "array", "items": {"type": "object", "properties": {"page": {"type": "integer"}}}},
                },
                "required": ["summary", "review"],
            },
            "deliverables": {"extra_bundle_note": {"type": "string"}},
        }))
        w7, o7 = os.path.join(td, "w7"), os.path.join(td, "o7")
        run_tr(w7, "init", "--case", os.path.join(c7, "case.json"), "--out", o7)
        open(os.path.join(c7, "t.txt"), "w", encoding="utf-8").write("@@ S0001\n正文\n@@ S0002\n主要短文。\n")
        run_tr(w7, "put", os.path.join(c7, "t.txt"))
        run_tr(w7, "finalize", "--set", "summary=ok", "--set", "extra_bundle_note=ready", "--set-json", "review=[]",
               "--set", "case_id=999", "--set", "status=failed")
        res7_ok = json.load(open(os.path.join(o7, "result.json"), encoding="utf-8"))
        # bad finalize must preserve result.json
        r7_bad = run_tr(w7, "finalize", "--set-json", "extra={")
        res7_pres = json.load(open(os.path.join(o7, "result.json"), encoding="utf-8"))
        # nested bad item triggers contract_gaps + partial
        run_tr(w7, "finalize", "--set-json", 'review=[{"page":"bad"}]')
        res7_nest = json.load(open(os.path.join(o7, "result.json"), encoding="utf-8"))

        # unspaced CJK 15,000-char paragraph
        c8 = os.path.join(td, "c8")
        os.makedirs(c8)
        open(os.path.join(c8, "cjk.md"), "w", encoding="utf-8").write("# 中文\n\n" + ("研究方法验证有效" * 2000) + "\n")
        w8, o8 = os.path.join(td, "w8"), os.path.join(td, "o8")
        run_tr(w8, "init", "--source", os.path.join(c8, "cjk.md"), "--out", o8)
        segs8 = C.read_jsonl(os.path.join(w8, "segments.jsonl"))

        results["v033_p0_nested_preserve_role_cjk"] = (
            os.path.basename(res7_ok["source"]["file"]) == "paper.md"
            and res7_ok.get("case_id") == 0
            and res7_ok.get("status") == "success"
            and res7_ok.get("extra_bundle_note") == "ready"
            and r7_bad.returncode == 2
            and len(res7_pres.get("segments", [])) == len(res7_ok.get("segments", []))
            and res7_nest.get("status") == "partial"
            and any(g["field"] == "review[0].page" for g in res7_nest.get("contract_gaps", []))
            and len([s for s in segs8 if s["kind"] == "paragraph"]) >= 8
            and max(s["units"] for s in segs8) <= 950
        )

    print(json.dumps(results, indent=2))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
