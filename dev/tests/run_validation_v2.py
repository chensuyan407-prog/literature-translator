#!/usr/bin/env python3
"""Replay the grader's v0.2.0 checker validation (grading/v0.2.0/validation/test_v2.py) against any Skill version.

GPT-3 PDF pp.10-15: 10 clean hand translations (written by the grader) and 16 single-defect mutations. Segments are
located by source text, so different segmentations are compared on identical inputs. Hit rule = the grader's: a new
(level, code) pair that was not on the clean translation and whose code matches the expected pattern.

    python tests/run_validation_v2.py --scripts S --pdf gpt3.pdf --work W --out report.json
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
                "--pages", "10-15", "--no-base", "--no-preview"], capture_output=True, env=env)
segs = C.read_jsonl(os.path.join(a.work, "segments.jsonl"))
st = C.read_json(os.path.join(a.work, "state.json"))
PREFIX = {
    "S0080": "For few-shot learning, we evaluate each example in the evaluation set",
    "S0081": "can be any value from 0 to the maximum amount allowed by the mo",
    "S0085": "On tasks that involve binary classification, we give the options more",
    "S0086": "On tasks with free-form completion, we use beam search with the same p",
    "S0087": "Final results are reported on the test set when publicly available",
    "S0094": "Table 3.1: Zero-shot results on PTB language modeling dataset",
    "S0110": "One note of caution is that an analysis of test set contamination",
    "S0112": "The HellaSwag dataset",
    "S0114": "We next evaluate GPT-3 on the StoryCloze 2016 dataset",
    "S0123": "For GPT-2 a filter was used on a multilingual collection of documents"}
seg_of = {}
for k, pre in PREFIX.items():
    hit = [s for s in segs if pre in C.restore(s.get("prot", ""), s.get("ph", {}))[:200]]
    seg_of[k] = hit[0] if hit else None
missing = [k for k, v in seg_of.items() if v is None]

clean = {  # the grader's clean hand translations, unchanged
    'S0080': '对于少样本学习，我们对评估集中的每个样本，均从该任务的训练集中随机抽取 {{M1}} 个样本作为条件示例，并依任务用 1 或 2 个换行符分隔。LAMBADA 和 Storycloze 没有可用的监督训练集，所以我们从开发集中抽取条件示例，并在测试集上评估。对于 Winograd（原版，非 SuperGLUE 版），只有一个数据集，因此直接从该数据集抽取条件示例。',
    'S0081': '{{M1}} 可以取从 0 到模型上下文窗口所允许的最大值之间的任意值。所有模型的 {{M2}} = 2048，通常可容纳 10 到 100 个样本。较大的 {{M3}} 往往表现更好，但并非总是如此。因此，如果分别有开发集和测试集，我们就在开发集尝试若干 {{M4}} 值，然后在测试集使用表现最好的值。某些任务（见附录 G）也使用自然语言提示作为示例的补充，或者在 {{M5}} = 0 时取代示例。',
    'S0085': '对于二分类任务，我们给各选项起意义更明确的名字，例如用“真”或“假”代替 0 或 1，然后将其作为多选题处理。有时我们也按类似 {{C1}} 的方式组织任务，详见附录 G。',
    'S0086': '对于自由补全任务，我们采用与 {{C1}} 相同参数的束搜索，即束宽为 4，长度惩罚为 {{M1}}。我们按该数据集的通行做法，用 F1 相似度分数、BLEU 或完全匹配率给模型评分。',
    'S0087': '对于每种模型规模和学习设置（零样本、单样本和少样本），测试集公开时便报告其最终结果。测试集不公开时，我们的模型通常因太大而无法放在测试服务器上，因此报告开发集结果。对于少数可以成功提交的数据集（SuperGLUE、TriviaQA、PiQa），我们确实提交到测试服务器，而且仅提交 200B 的少样本结果。其余情况均报告开发集结果。',
    'S0094': '表 3.1：在 PTB 语言建模数据集上的零样本结果。许多其他常用的语言建模数据集均未纳入，因为它们来自维基百科或 GPT-3 训练数据所包含的其他来源。^{a}{{C1}}',
    'S0110': '有一点须留意：测试集污染分析发现，LAMBADA 数据集有不可忽视的一小部分似乎出现在我们的训练数据中。不过第 4 节的分析表明，这对性能的影响可忽略不计。',
    'S0112': '在 HellaSwag 数据集 {{C1}} 中，需要为故事或一系列操作说明选出最佳结尾。这些样本经过对抗式挖掘，力求难倒语言模型，却仍便于人类作答（人类准确率为 95.6%）。GPT-3 的单样本准确率为 78.1%，少样本准确率为 79.3%，高于一个拥有 15 亿参数的微调语言模型 {{C2}} 的 75.4%，但仍明显低于微调多任务模型 ALUM 达到的总体最佳水平 85.6%。',
    'S0114': '接着我们在 StoryCloze 2016 数据集 {{C1}} 上评估 GPT-3。该任务是为五句话组成的故事选出正确的结尾句。GPT-3 的零样本准确率达到 83.2%，少样本时达到 87.7%（{{M1}}）。该结果仍比用基于 BERT 的模型 {{C2}} 微调得到的最佳结果低 4.1%，但比先前的零样本结果提高约 10%。',
    'S0123': '由于容量限制，GPT-2 对多语言文档集合做了过滤，以得到纯英文数据集。即便如此，GPT-2 仍展现出一定的多语言能力。它训练时接触的法语文本仅剩 10 兆字节，但法语和英语互译表现已非微不足道。GPT-3 的容量比 GPT-2 增加了两个数量级以上，因此我们扩大了训练数据集的范围，让更多语言获得更充分的代表性，不过仍有改进空间。如第 2.2 节所述，我们的数据大部分来自仅经过质量筛选的原始 Common Crawl。尽管 GPT-3 的训练数据仍以英语为主（按单词数计算占 93%），也包含 7% 的其他语言文本。补充材料记录了这些语言。为更好地理解翻译能力，我们又将德语和罗马尼亚语这两种常见研究语言纳入分析。'}


def repl(k, old, new):
    assert old in clean[k], (k, old)
    return {k: clean[k].replace(old, new, 1)}


tests = [
    ('number', '束宽 4→5', repl('S0086', '束宽为 4', '束宽为 5'), r'number'),
    ('unit', '10 兆字节→10 兆比特', repl('S0123', '兆字节', '兆比特'), r'unit|number|noun'),
    ('crossref', '表3.1→表3.2', repl('S0094', '表 3.1', '表 3.2'), r'crossref|number'),
    ('crossref_kind', '表3.1→图3.1', repl('S0094', '表 3.1', '图 3.1'), r'crossref'),
    ('math_drop', '漏掉长度惩罚公式', repl('S0086', '{{M1}}', ''), r'placeholder'),
    ('math_dup', '长度惩罚公式重复', repl('S0086', '{{M1}}', '{{M1}}{{M1}}'), r'placeholder'),
    ('cite_drop', '漏掉束搜索引用', repl('S0086', '{{C1}}', ''), r'placeholder'),
    ('cite_dup', '束搜索引用重复', repl('S0086', '{{C1}}', '{{C1}}{{C1}}'), r'placeholder'),
    ('negation', '非 SuperGLUE 版→即 SuperGLUE 版', repl('S0080', '（原版，非 SuperGLUE 版）', '（原版，即 SuperGLUE 版）'), r'negation|qualifier|polarity'),
    ('negation_scope', '但并非总是→且总是', repl('S0081', '但并非总是如此', '且总是如此'), r'negation|quantifier|polarity|qualifier'),
    ('qualifier', '约10%→恰好10%', repl('S0114', '提高约 10%', '恰好提高 10%'), r'qualifier|quantifier|polarity'),
    ('term', '第一处语言模型→禁用变体', repl('S0112', '语言模型', '语言模式'), r'term'),
    ('half', '删掉后两句', {'S0081': clean['S0081'].split('。因此')[0] + '。'}, r'length|sentence|omission|placeholder|number|xref|crossref'),
    ('swapped', '调换相邻两段', {'S0085': clean['S0086'], 'S0086': clean['S0085']}, r'placeholder|length|number|crossref|content'),
    ('semantic', '谁容易作答反过来', repl('S0112', '力求难倒语言模型，却仍便于人类作答', '力求难倒人类，却仍便于语言模型作答'), r'.'),
    ('semantic_cells', '交换人类与基线正确率', {'S0112': clean['S0112'].replace('95.6', 'TEMP').replace('75.4', '95.6').replace('TEMP', '75.4')}, r'.'),
]
gl = C.compile_glossary([{"source": "language model", "target": "语言模型", "keep_original": False, "variants": ["语言模式"], "origin": "agent", "note": ""}])
sl, tl = st["src_lang"], st["tgt_lang"]


def flags(k, text):
    return {(i["severity"], i["code"]): i["detail"] for i in C.check_segment(seg_of[k], text, gl, sl, tl)}


clean_flags = {k: flags(k, v) for k, v in clean.items() if seg_of[k]}
rows = []
for name, desc, edit, expected in tests:
    added = {}
    for k, text in edit.items():
        if not seg_of[k]:
            continue
        for key, det in flags(k, text).items():
            if key not in clean_flags.get(k, {}):
                added[key] = det
    hit = any(re.search(expected, code) for (_, code) in added)
    rows.append({"case": name, "mutation": desc, "segments": {k: seg_of[k]["id"] if seg_of[k] else None for k in edit},
                 "hit": hit, "errors": sorted(c for (lv, c) in added if lv == "error"), "warnings": sorted(c for (lv, c) in added if lv == "warn"),
                 "details": [f"[{lv}] {c}: {d}" for (lv, c), d in added.items()]})
rep = {"version": C.VERSION, "missing_segments": missing, "detected": sum(r["hit"] for r in rows), "total": len(rows),
       "rows": rows, "clean_segments": len(clean_flags),
       "clean_with_error": sum(1 for v in clean_flags.values() if any(lv == "error" for lv, _ in v)),
       "clean_with_warn": sum(1 for v in clean_flags.values() if any(lv == "warn" for lv, _ in v)),
       "clean_flags": {k: [f"[{lv}] {c}: {d}" for (lv, c), d in v.items()] for k, v in clean_flags.items() if v}}
json.dump(rep, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"version {C.VERSION}: detected {rep['detected']}/{rep['total']}; clean {rep['clean_with_error']}/{rep['clean_segments']} with error, "
      f"{rep['clean_with_warn']}/{rep['clean_segments']} with warning; missing segments {missing}")
for r in rows:
    print(f"  {'HIT ' if r['hit'] else 'miss'} {r['case']:<15} err={r['errors']} warn={r['warnings']}")
for k, v in rep["clean_flags"].items():
    print(f"  clean {k}: {v}")
