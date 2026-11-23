#!/usr/bin/env python3
"""False-alarm rate of a checker version on clean (reviewed) translations.

Runs check_segment from --scripts (any Skill version) over the translated segments of --work, so two rule
sets can be compared on the very same segments and translations.

    python tests/heldout_fp.py --scripts <submission/scripts> --work W --label v0.2 --out fp.json
"""
import argparse
import json
import os
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--scripts", required=True)
ap.add_argument("--work", required=True)
ap.add_argument("--label", default="")
ap.add_argument("--out")
a = ap.parse_args()
sys.path.insert(0, os.path.abspath(a.scripts))
import lt_core as C  # noqa: E402

st = C.read_json(os.path.join(a.work, "state.json"))
segs = C.read_jsonl(os.path.join(a.work, "segments.jsonl"))
trans = C.read_json(os.path.join(a.work, "translations.json"), {})
gl = C.compile_glossary(C.read_json(os.path.join(a.work, "glossary.json"), []))
rows = {}
for s in segs:
    t = trans.get(s["id"], {}).get("tgt")
    if t and s.get("translate"):
        rows[s["id"]] = [f"[{i['severity']}] {i['code']}: {i['detail']}" for i in C.check_segment(s, t, gl, st["src_lang"], st["tgt_lang"])]
n = len(rows)
err = sum(1 for v in rows.values() if any(x.startswith("[error]") for x in v))
warn = sum(1 for v in rows.values() if any(x.startswith("[warn]") for x in v))
rep = {"label": a.label, "checker_version": C.VERSION, "work": os.path.abspath(a.work), "segments": n,
       "segments_with_error": err, "segments_with_warning": warn,
       "error_false_alarm_rate": round(err / n, 4) if n else None, "warning_false_alarm_rate": round(warn / n, 4) if n else None,
       "issues": {k: v for k, v in rows.items() if v}}
print(f"{a.label} checker {C.VERSION}: {n} clean segments, {err} with error, {warn} with warning")
for k, v in rep["issues"].items():
    print("  ", k, v)
if a.out:
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(rep, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
