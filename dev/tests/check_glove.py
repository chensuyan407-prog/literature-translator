#!/usr/bin/env python3
"""GloVe (Pennington et al., 2014) regression, pages 2-6: Table 1 grid (12 data cells), sentence joins across page / column /
inline formulas, and all 22 display equations. Expected values were read from the PDF page images.

An equation passes when it is rebuilt equal to the page (normalised comparison) and marked ok, OR when it is marked
needs_confirmation with a crop image and its number (the Skill must never mark a wrong rebuild as ok).

    python tests/check_glove.py --scripts S --pdf glove.pdf --work W --out report.json
"""
import argparse
import json
import os
import re
import subprocess
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--scripts", required=True)
ap.add_argument("--pdf", required=True)
ap.add_argument("--work", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()
env = dict(os.environ, PYTHONIOENCODING="utf-8")
subprocess.run([sys.executable, os.path.join(a.scripts, "tr.py"), "--work", a.work, "init", "--source", a.pdf, "--out", a.work + "_out",
                "--pages", "2-6"], capture_output=True, env=env)
segs = [json.loads(l) for l in open(os.path.join(a.work, "segments.jsonl"), encoding="utf-8")]


def norm(t):
    t = (t or "").replace("̃", "~").replace("˜", "~").replace("−", "-").replace("ˆ", "^").replace("∼", "~")
    t = re.sub(r"([A-Za-z])~", r"~\1", t)                      # w˜ / w̃ / ˜w -> ~w
    for _ in range(3):                                         # x^{a}_{b} -> x_{b}^{a}
        t = re.sub(r"\^\{([^{}]*)\}_\{([^{}]*)\}", r"_{\2}^{\1}", t)
    return re.sub(r"[\s{}()\[\]_^,.]", "", t)


EXPECT_ROWS = {"P(k|ice)": ["1.9×10-4", "6.6×10-5", "3.0×10-3", "1.7×10-5"],
               "P(k|steam)": ["2.2×10-5", "7.8×10-4", "2.2×10-3", "1.8×10-5"],
               "P(k|ice)/P(k|steam)": ["8.9", "8.5×10-2", "1.36", "0.96"]}
EQ = {  # page content in the extractor's notation; None = 2-D layout (cases / aligned rows / unknown glyph): must be flagged
    "1": "F(w_{i},w_{j},~w_{k}) = P_{ik}/P_{jk}", "2": "F(w_{i} − w_{j},~w_{k}) = P_{ik}/P_{jk}",
    "3": "F((w_{i} − w_{j})^{T}~w_{k}) = P_{ik}/P_{jk}", "4": "F((w_{i} − w_{j})^{T}~w_{k}) = F(w_{i}^{T}~w_{k})/F(w_{j}^{T}~w_{k})",
    "5": "F(w_{i}^{T}~w_{k}) = P_{ik} = X_{ik}/X_{i}", "6": "w_{i}^{T}~w_{k} = log(P_{ik}) = log(X_{ik}) − log(X_{i})",
    "7": "w_{i}^{T}~w_{k} + b_{i} + ~b_{k} = log(X_{ik})", "8": "J = ∑_{i,j=1}^{V} f(X_{ij})(w_{i}^{T}~w_{j} + b_{i} + ~b_{j} − log X_{ij})^{2}",
    "9": None, "10": "Q_{ij} = exp(w_{i}^{T}~w_{j})/∑_{k=1}^{V} exp(w_{i}^{T}~w_{k})", "11": "J = − ∑_{i∈corpus, j∈context(i)} log Q_{ij}",
    "12": "J = − ∑_{i=1}^{V}∑_{j=1}^{V} X_{ij} log Q_{ij}", "13": "J = − ∑_{i=1}^{V} X_{i} ∑_{j=1}^{V} P_{ij} log Q_{ij} = ∑_{i=1}^{V} X_{i} H(P_{i},Q_{i})",
    "14": "ˆJ = ∑_{i,j} X_{i}(ˆP_{ij} − ˆQ_{ij})^{2}", "15": None, "16": "ˆJ = ∑_{i,j} f(X_{ij})(w_{i}^{T}~w_{j} − log X_{ij})^{2}",
    "17": "X_{ij} = k/(r_{ij})^{α}", "18": "|C| ∼ ∑_{ij} X_{ij} = ∑_{r=1}^{|X|} k/r^{α} = kH_{|X|,α}", "19": "|C| ∼ |X|^{α} H_{|X|,α}",
    "20": None, "21": "|C| ∼ |X|/(1 − α) + ζ(α) |X|^{α} + O(1)", "22": None}
EQ_N = {k: (norm(v).replace("^J", "J^").replace("^P", "P^").replace("^Q", "Q^") if v else None) for k, v in EQ.items()}
rep = {"scripts": os.path.abspath(a.scripts), "table1": {}, "joins": {}, "equations": {}}
tab = next((s for s in segs if s["kind"] == "table" and "P(k|ice)" in s.get("src", "")), None)
ok_cells = 0
if tab:
    rows = [[c.strip() for c in r.split(" | ")] for r in tab["src"].split("\n") if r.strip()]
    rep["table1"]["header_in_table"] = any("Probability and Ratio" in r[0] for r in rows)
    for label, exp in EXPECT_ROWS.items():
        row = next((r for r in rows if r[0].replace(" ", "") == label), None)
        got = [re.sub(r"[\s{}^]", "", c).replace("−", "-") for c in row[1:]] if row else []
        ok_cells += sum(1 for k, e in enumerate(exp) if len(got) == 4 and got[k] == e)
rep["table1"]["data_cells_correct"] = f"{ok_cells}/12"
src = lambda s: s.get("src", "")
J = rep["joins"]
s21 = next((s for s in segs if "First we establish some notation" in src(s)), None)
J["p2_S0021_inline_sum_and_p3_join"] = bool(s21) and "∑" in src(s21) and "⟨?⟩" not in src(s21) and src(s21).rstrip().endswith("context of word i.")
s59 = next((s for s in segs if "Recalling our notation" in src(s)), None)
J["p5_S0059_S0061_inline_definition_one_segment"] = bool(s59) and "we can rewrite J as" in src(s59) and "∑" in src(s59)
s64 = next((s for s in segs if "are the unnormalized distributions" in src(s)), None)
J["p5_S0066_S0076_cross_column_order"] = bool(s64) and "squared error of the logarithms" in src(s64) and src(s64).startswith("where")
e17 = next((s for s in segs if s["kind"] == "equation" and s.get("eq_number") == "17"), None)
J["p5_S0075_no_other_column_in_equation"] = bool(e17) and "unnormalized" not in src(e17) and "where" not in src(e17) and not src(e17).startswith("r")
J["leaked_table_header"] = [s["id"] for s in segs if s["kind"] == "paragraph" and "Probability and Ratio" in src(s)]
passed = 0
for num, exp in EQ_N.items():
    s = next((x for x in segs if x["kind"] == "equation" and x.get("eq_number") == num), None)
    if not s:
        rep["equations"][num] = {"found": False, "pass": False}
        continue
    body = re.sub(r"[,.]?\s*\(" + num + r"\)\s*$", "", s["src"]).strip().rstrip(",.").strip()
    got = norm(body).replace("^J", "J^").replace("^P", "P^").replace("^Q", "Q^")
    flagged = s.get("eq_status") == "needs_confirmation" and bool(s.get("image"))
    if exp is None:
        ok = flagged
        verdict = "flagged (2-D layout / unknown glyph)" if ok else "WRONG: marked ok although not reliably rebuildable"
    else:
        same = got == exp
        ok = (same and s.get("eq_status") == "ok") or (not same and flagged)
        verdict = "rebuilt = page" if same and s.get("eq_status") == "ok" else ("flagged for confirmation" if flagged else "WRONG and marked ok")
    passed += ok
    rep["equations"][num] = {"id": s["id"], "page": s.get("page"), "status": s.get("eq_status"), "image": s.get("image"),
                             "text": s["src"], "pass": ok, "verdict": verdict}
rep["equations_pass"] = f"{passed}/22"
rep["equations_rebuilt_exactly"] = sum(1 for v in rep["equations"].values() if v.get("verdict") == "rebuilt = page")
json.dump(rep, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"table1 {rep['table1']['data_cells_correct']} (header in table: {rep['table1'].get('header_in_table')}); joins {J}; "
      f"equations pass {rep['equations_pass']}, rebuilt exactly {rep['equations_rebuilt_exactly']}")
for n, v in rep["equations"].items():
    if not v.get("pass"):
        print(f"  ({n}) FAIL {v.get('verdict')} :: {v.get('text', '')[:120]}")
