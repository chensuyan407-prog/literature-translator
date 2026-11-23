#!/usr/bin/env python3
"""Independent re-test of the frozen v0.3 checker on segments never used for tuning.

"Attention Is All You Need" pp.8-9 (Transformer results section). Clean translations: tests/heldout/attention_p8-9.zh.txt
(written before the mutations, checked with heldout_fp.py). Mutations were written after the rules were frozen and follow
the 16 categories of the tuning set, plus a few extra variants. Hit rule = run_validation_v2.py: a new (level, code) pair
that was not on the clean translation and whose code matches the expected pattern.

    python tests/heldout_mutations.py --scripts S --pdf attention.pdf --work W --out report.json
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
sys.path.insert(0, os.path.abspath(a.scripts))
import lt_core as C  # noqa: E402

env = dict(os.environ, PYTHONIOENCODING="utf-8")
subprocess.run([sys.executable, os.path.join(a.scripts, "tr.py"), "--work", a.work, "init", "--source", a.pdf, "--out", a.work + "_out",
                "--pages", "8-9", "--no-base", "--no-preview"], capture_output=True, env=env)
segs = {s["id"]: s for s in C.read_jsonl(os.path.join(a.work, "segments.jsonl"))}
st = C.read_json(os.path.join(a.work, "state.json"))
clean = {}
cur = None
for line in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "heldout", "attention_p8-9.zh.txt"), encoding="utf-8"):
    line = line.rstrip("\n")
    if line.startswith("@@ "):
        cur = line[3:].strip()
        clean[cur] = []
    elif cur:
        clean[cur].append(line)
clean = {k: "\n".join(v).strip() for k, v in clean.items()}


def repl(k, old, new):
    assert old in clean[k], (k, old)
    return {k: clean[k].replace(old, new, 1)}


def swap(k, x, y):
    assert x in clean[k] and y in clean[k], (k, x, y)
    return {k: clean[k].replace(x, "\0").replace(y, x).replace("\0", y)}


tests = [
    ('number', '最后 5 个检查点→6 个', repl('S0093', '最后 5 个检查点', '最后 6 个检查点'), r'number'),
    ('number_table', '表 2 BLEU 41.8→41.3', repl('S0086', '41.8', '41.3'), r'number|table'),
    ('unit', 'TFLOPS→GFLOPS', repl('S0097', 'TFLOPS', 'GFLOPS'), r'unit|number'),
    ('unit_time', '3.5 天→3.5 小时', repl('S0091', '{{M3}} 天', '{{M3}} 小时'), r'unit|noun|number'),
    ('crossref', '表 3 (A) 行→表 4', repl('S0100', '表 3', '表 4'), r'crossref|number'),
    ('crossref_kind', '表 2→图 2', repl('S0094', '表 2', '图 2'), r'crossref'),
    ('crossref_section', '第 3.2.2 节→第 3.2.1 节', repl('S0100', '3.2.2', '3.2.1'), r'crossref|number'),
    ('math_drop', '漏掉 28.4', repl('S0091', '{{M2}}', ''), r'placeholder'),
    ('math_dup', '0.1 丢弃率重复', repl('S0087', '{{M1}}', '{{M1}}{{M1}}'), r'placeholder'),
    ('cite_drop', '漏掉束搜索引用', repl('S0093', '{{C1}}', ''), r'placeholder'),
    ('cite_dup', '标签平滑引用重复', repl('S0088', '{{C1}}', '{{C1}}{{C1}}'), r'placeholder'),
    ('negation', '不做检查点平均→做检查点平均', repl('S0096', '但不做检查点平均', '并做检查点平均'), r'negation|polarity'),
    ('negation_scope', '不应与…相比较→应与…相比较（"未列出"仍在）', repl('S0098', '不应与', '应与'), r'negation|polarity|qualifier'),
    ('negation_scope2', '一直未能取得→一直能取得', repl('S0103', '一直未能取得', '一直能取得'), r'negation|polarity'),
    ('qualifier', '约 40K→40K', repl('S0104', '约 40K', '40K'), r'qualifier|quantifier'),
    ('qualifier2', '几乎相同→相同', repl('S0101', '几乎相同', '相同'), r'qualifier|quantifier|polarity'),
    ('term', '第一处丢弃→禁用变体随机失活', repl('S0087', '施加丢弃', '施加随机失活'), r'term'),
    ('half', '删掉后两句', {'S0091': clean['S0091'].split('。训练')[0] + '。'}, r'length|sentence|omission|placeholder|number|crossref'),
    ('swapped', '调换相邻两段', {'S0093': clean['S0094'], 'S0094': clean['S0093']}, r'placeholder|length|number|crossref|content'),
    ('semantic', '更大的模型更好→更小的模型更好', repl('S0101', '更大的模型更好', '更小的模型更好'), r'.'),
    ('semantic_cells', '表 2 基础/大模型英德 BLEU 互换', swap('S0086', '27.3', '28.4'), r'.'),
    ('semantic_binding', '仅 WSJ 16K / 半监督 32K 互换', swap('S0104', '16K', '32K'), r'.'),
]
gl = C.compile_glossary([{"source": "dropout", "target": "丢弃", "keep_original": False, "variants": ["随机失活"], "origin": "agent", "note": ""}])
sl, tl = st["src_lang"], st["tgt_lang"]


def flags(k, text):
    return {(i["severity"], i["code"]): i["detail"] for i in C.check_segment(segs[k], text, gl, sl, tl)}


clean_flags = {k: flags(k, v) for k, v in clean.items() if k in segs}
rows = []
for name, desc, edit, expected in tests:
    added = {}
    for k, text in edit.items():
        for key, det in flags(k, text).items():
            if key not in clean_flags.get(k, {}):
                added[key] = det
    hit = any(re.search(expected, code) for (_, code) in added)
    rows.append({"case": name, "mutation": desc, "segments": list(edit), "hit": hit,
                 "errors": sorted(c for (lv, c) in added if lv == "error"), "warnings": sorted(c for (lv, c) in added if lv == "warn"),
                 "details": [f"[{lv}] {c}: {d}" for (lv, c), d in added.items()]})
rep = {"version": C.VERSION, "paper": "Attention Is All You Need, pp.8-9", "detected": sum(r["hit"] for r in rows), "total": len(rows),
       "detected_as_error": sum(1 for r in rows if r["hit"] and r["errors"]), "rows": rows, "clean_segments": len(clean_flags),
       "clean_with_error": sum(1 for v in clean_flags.values() if any(lv == "error" for lv, _ in v)),
       "clean_with_warn": sum(1 for v in clean_flags.values() if any(lv == "warn" for lv, _ in v)),
       "clean_flags": {k: [f"[{lv}] {c}: {d}" for (lv, c), d in v.items()] for k, v in clean_flags.items() if v}}
os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
json.dump(rep, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"version {C.VERSION}: detected {rep['detected']}/{rep['total']} ({rep['detected_as_error']} as error); clean "
      f"{rep['clean_with_error']}/{rep['clean_segments']} with error, {rep['clean_with_warn']}/{rep['clean_segments']} with warning")
for r in rows:
    print(f"  {'HIT ' if r['hit'] else 'miss'} {r['case']:<17} err={r['errors']} warn={r['warnings']}")
