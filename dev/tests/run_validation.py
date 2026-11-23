#!/usr/bin/env python3
"""Replay the grader's GloVe checker validation (grading/validation/test.py) against any Skill version.

14 single-defect mutations + 7 clean hand translations from GloVe p.2-7. Segments are located by their
source text (not by id) so v0.1 and v0.2 segmentations are compared on identical inputs.
A mutation counts as detected when check_segment reports a NEW issue (code+detail not present on the
clean translation) whose code belongs to the expected family for that mutation (`any` = any new issue).

    python tests/run_validation.py --scripts <submission/scripts> --pdf glove.pdf --work W --out report.json
"""
import argparse
import json
import os
import subprocess
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--scripts", required=True)
ap.add_argument("--pdf", required=True)
ap.add_argument("--work", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()
sys.path.insert(0, os.path.abspath(a.scripts))
import lt_core as C  # noqa: E402  (the version under test)

TR = os.path.join(a.scripts, "tr.py")
env = dict(os.environ, PYTHONIOENCODING="utf-8")
subprocess.run([sys.executable, TR, "--work", a.work, "init", "--source", a.pdf, "--out", a.work + "_out", "--no-preview"],
               capture_output=True, env=env)
segs = C.read_jsonl(os.path.join(a.work, "segments.jsonl"))
st = C.read_json(os.path.join(a.work, "state.json"))

PREFIX = {  # grader's v0.1 id -> start of the source text
    "S0017": "In the skip-gram and ivLBL models", "S0018": "Unlike the matrix factorization methods",
    "S0023": "Table 1: Co-occurrence probabilities", "S0049": "We propose a new weighted least squares",
    "S0057": "3. f (x) should be relatively small", "S0116": "We are interested in how |X| is related",
    "S0132": "Figure 2: Accuracy on the analogy task"}
seg_of = {}
for k, pre in PREFIX.items():
    hit = [s for s in segs if C.restore(s.get("prot", ""), s.get("ph", {})).startswith(pre)]
    seg_of[k] = hit[0] if hit else None

# clean hand translations written by the grader (grading/validation/test.py), unchanged
clean = {
    'S0017': 'skip-gram 和 ivLBL 模型给定词本身来预测上下文，而 CBOW 和 vLBL 模型给定上下文来预测词。在词类比任务上的评估表明，这些模型能够把语言模式学成词向量之间的线性关系。',
    'S0018': '与矩阵分解方法不同，浅层窗口方法的缺点是它们不直接利用语料库的共现统计量。这些模型转而扫描整个语料库中的上下文窗口，因而未能利用数据中的大量重复。',
    'S0023': '表 1：从含 60 亿词元（6 billion tokens）的语料库中统计目标词 ice 和 steam 与所选上下文词的共现概率。只有在比值中，water 和 fashion 这类缺乏区分力的词所产生的噪声才会抵消，因此大的比值（远大于 1）与 ice 特有的性质高度相关，小的比值（远小于 1）则与 steam 特有的性质高度相关。',
    'S0049': '我们提出一种新的加权最小二乘回归模型来解决这些问题。将式（7）写成最小二乘问题，并在代价函数中引入权重函数 f(X{{M1}})，便可得到以下模型。',
    'S0057': '3. 当 x 很大时，f(x) 应相对较小，以免给频繁共现的词对赋予过高的权重。',
    'S0116': '我们关注的是 |X| 和 |C| 均很大时二者之间的关系，因此可以在 |X| 很大时展开等式右侧。为此，采用广义调和数的展开式 {{C1}}。',
    'S0132': '图 2：向量维数和窗口大小、类型与词类比任务准确率的关系。所有模型均在 60 亿词元（6 billion tokens）的语料库上训练。（a）中窗口大小为 10；（b）和（c）中向量维数为 100。'}


def repl(k, x, y):
    assert x in clean[k], x
    return {k: clean[k].replace(x, y)}


tests = [
    ("number", repl('S0132', '为 10；', '为 11；'), "number"),
    ("unit", repl('S0023', '词元（6 billion tokens）', '篇文章（6 billion documents）'), "unit"),
    ("crossref", repl('S0049', '式（7）', '式（8）'), "crossref"),
    ("crossref_kind", repl('S0049', '式（7）', '表（7）'), "crossref"),
    ("math_drop", repl('S0049', '{{M1}}', ''), "placeholder_missing"),
    ("cite_drop", repl('S0116', '{{C1}}', ''), "placeholder_missing"),
    ("math_dup", repl('S0049', '{{M1}}', '{{M1}}{{M1}}'), "placeholder_extra"),
    ("cite_dup", repl('S0116', '{{C1}}', '{{C1}}{{C1}}'), "placeholder_extra"),
    ("negation", repl('S0018', '不直接利用', '直接利用'), "negation"),
    ("qualifier", repl('S0023', '只有在比值中', '在比值内外普遍'), "qualifier"),
    ("term", repl('S0017', '词向量', '词矢量'), "term"),
    ("half", {'S0017': clean['S0017'].split('。')[0] + '。'}, "any"),
    ("swapped", {'S0017': clean['S0018'], 'S0018': clean['S0017']}, "any"),
    ("semantic", {'S0057': '3. 当 x 很大时，f(x) 应相对较大，以免给频繁共现的词对赋予过低的权重。'}, "any"),
]
FAMILY = {"number": ("number_",), "unit": ("unit_", "counted_noun"), "crossref": ("crossref",), "negation": ("negation",),
          "qualifier": ("qualifier", "quantifier"), "term": ("term_",)}
gl = C.compile_glossary([{"source": "word vector", "target": "词向量", "keep_original": False, "variants": ["词矢量"], "origin": "agent", "note": ""}])
sl, tl = st["src_lang"], st["tgt_lang"]


def check(k, text):
    return C.check_segment(seg_of[k], text, gl, sl, tl)


key = lambda i: (i["code"], i["detail"])
clean_issues = {k: check(k, v) for k, v in clean.items()}
rows = []
for name, edit, fam in tests:
    new = []
    for k, text in edit.items():
        before = {key(i) for i in clean_issues[k]}
        new += [dict(i, seg=k) for i in check(k, text) if key(i) not in before]
    if fam == "any":
        hit = bool(new)
    else:
        prefixes = FAMILY.get(fam, (fam,))
        hit = any(i["code"].startswith(prefixes) for i in new)
    rows.append({"case": name, "segments": {k: seg_of[k]["id"] for k in edit}, "detected": hit,
                 "new_issues": [f"{i['seg']} [{i['severity']}] {i['code']}: {i['detail']}" for i in new]})
clean_rows = {k: [f"[{i['severity']}] {i['code']}: {i['detail']}" for i in v] for k, v in clean_issues.items()}
rep = {"scripts": os.path.abspath(a.scripts), "version": C.VERSION, "segment_map": {k: (v["id"] if v else None) for k, v in seg_of.items()},
       "detected": sum(r["detected"] for r in rows), "total": len(rows), "rows": rows,
       "clean_segments": len(clean), "clean_with_error": sum(1 for v in clean_issues.values() if any(i["severity"] == "error" for i in v)),
       "clean_with_warn": sum(1 for v in clean_issues.values() if any(i["severity"] == "warn" for i in v)), "clean_issues": clean_rows}
os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
json.dump(rep, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"version {C.VERSION}: detected {rep['detected']}/{rep['total']}; clean: {rep['clean_with_error']} with error, "
      f"{rep['clean_with_warn']} with warning (of {len(clean)})")
for r in rows:
    print(f"  {'HIT ' if r['detected'] else 'miss'} {r['case']:<14} {r['segments']} {r['new_issues'][:2]}")
for k, v in clean_rows.items():
    if v:
        print(f"  clean {k}: {v}")
