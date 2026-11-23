#!/usr/bin/env python3
"""Emulated-clock run of the batch scheduler on a long document (GPT-3, 75 pages).

What is real: segmentation, batch selection, budget arithmetic, checks, outputs, resume.
What is emulated: wall-clock time (state.started is shifted to the simulated elapsed time) and the
model's translation speed (--overhead s per request + units / --rate units per second).
Translations: real ones from another agent's earlier run on the same paper where available
(--reuse alignment.jsonl files, matched by source text); every other segment gets a stand-in
"〔SIM 模拟译文 S0123〕…" that copies numbers and placeholders so the run can proceed. Stand-ins are a
test fixture: they measure scheduling and bookkeeping, never translation quality.

    python tests/sim_longdoc.py --scripts S --case case.json --pdf paper.pdf --work W --out O [--reuse a.jsonl ...]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("--scripts", required=True)
ap.add_argument("--case", required=True)
ap.add_argument("--pdf", required=True)
ap.add_argument("--work", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--reuse", nargs="*", default=[])
ap.add_argument("--budget", type=int, default=900)
ap.add_argument("--rate", type=float, default=6.0, help="emulated translation speed, units (words) per second")
ap.add_argument("--overhead", type=float, default=14.0, help="emulated fixed cost per model request, seconds")
ap.add_argument("--startup", type=float, default=45.0, help="emulated time before init (reading SKILL.md and the task)")
ap.add_argument("--log")
a = ap.parse_args()
sys.path.insert(0, os.path.abspath(a.scripts))
import lt_core as C  # noqa: E402

TR = os.path.join(a.scripts, "tr.py")
env = dict(os.environ, PYTHONIOENCODING="utf-8")
log = []


def run(*args):
    r = subprocess.run([sys.executable, TR, "--work", a.work, *args], capture_output=True, text=True, encoding="utf-8", env=env)
    log.append({"args": list(args), "stdout": r.stdout[-4000:], "rc": r.returncode})
    return r.stdout


def set_clock(sim):
    st = C.read_json(os.path.join(a.work, "state.json"))
    st["started"] = time.time() - sim
    C.write_json(os.path.join(a.work, "state.json"), st)


key = lambda t: re.sub(r"\W+", "", C.strip_ph(t or "").lower())[:80]
reuse = {}
for f in a.reuse:
    for row in C.read_jsonl(f):
        if row.get("translation") and row.get("source"):
            reuse[key(row["source"])] = row["translation"]

sim = a.startup
run("init", "--case", a.case, "--source", a.pdf, "--out", a.out, "--no-preview", "--budget", str(a.budget), "--started", str(time.time() - sim))
sim += 8
steps, real_used, stand_ins = [], 0, 0
while True:
    set_clock(sim)
    out = run("batch")
    ids = re.findall(r"^@@ (S\d{4}(?:\.\d+)?)", out, re.M)
    head = next((l for l in out.splitlines() if l.startswith(("=== BATCH", "STOP", "NO MORE", "SCHEDULE"))), "")
    if not ids:
        steps.append({"t": round(sim), "event": head})
        break
    segs = {s["id"]: s for s in C.read_jsonl(os.path.join(a.work, "segments.jsonl"))}
    blocks, units = [], 0
    for sid in ids:
        s = segs[sid]
        units += s["units"]
        src_disp = C.restore(s["prot"], s["ph"])
        t = reuse.get(key(src_disp))
        if t:
            # restore -> re-protect: put the placeholders back where the reused translation has the literal tokens
            for k, v in sorted(s["ph"].items(), key=lambda kv: -len(kv[1])):
                t = t.replace(v, "{{" + k + "}}", 1) if v in t else t
            real_used += 1
        else:
            stand_ins += 1
            if s["kind"] == "table":
                t = s["prot"]
            else:
                t = f"〔SIM 模拟译文 {sid}〕" + " ".join(C.PH_RE.findall(s["prot"]) and ["{{" + p + "}}" for p in C.PH_RE.findall(s["prot"])]) + \
                    " " + " ".join(C.numbers(s["prot"]))
        blocks.append(f"@@ {sid}\n{t}")
    sim += a.overhead + units / a.rate
    set_clock(sim)
    pf = os.path.join(a.work, f"sim_put_{len(steps) + 1:02d}.txt")
    open(pf, "w", encoding="utf-8").write("\n".join(blocks))
    pout = run("put", pf)
    steps.append({"t": round(sim), "batch": head, "ids": f"{ids[0]}..{ids[-1]}", "n": len(ids), "units": units,
                  "time_line": next((l for l in pout.splitlines() if l.startswith("TIME")), "")})
    if sim > a.budget + 60:
        steps.append({"t": round(sim), "event": "harness guard: exceeded budget + 60 s"})
        break
sim += 10
set_clock(sim)
fin = run("finalize")
res = C.read_json(os.path.join(a.out, "result.json"))
GROUP = {"paragraph": "body", "footnote": "body", "title": "headings", "heading": "headings", "caption": "captions", "table": "tables"}
by_kind = {}
for x in res["segments"]:  # computed here so that results of any Skill version can be compared
    g = GROUP.get(x["kind"])
    if g and x["status"] in ("translated", "untranslated"):
        d = by_kind.setdefault(g, {"done": 0, "total": 0})
        d["total"] += 1
        d["done"] += x["status"] == "translated"
for d in by_kind.values():
    d["coverage"] = round(d["done"] / d["total"], 4)
rep = {"emulated_seconds_at_finalize": round(sim), "budget": a.budget, "rate_units_per_s": a.rate, "overhead_s": a.overhead,
       "batches": steps, "reused_real_translations": real_used, "stand_in_translations": stand_ins,
       "status": res["status"], "status_reason": res["status_reason"],
       "coverage": res["metrics"]["coverage"], "coverage_by_kind": by_kind,
       "untranslated_count": len(res.get("untranslated") or []), "resume": res.get("resume"),
       "contract_type_coercions": res.get("contract_type_coercions"),
       "verification_notes_type": type(res.get("verification_notes")).__name__,
       "contract_fields_unfilled": res.get("contract_fields_unfilled")}
print(json.dumps({k: v for k, v in rep.items() if k != "batches"}, ensure_ascii=False, indent=1))
for s in steps:
    print("  ", s)
if a.log:
    json.dump({"report": rep, "log": log}, open(a.log, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
