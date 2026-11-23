#!/usr/bin/env python3
"""Scanned-PDF regression: init (image-only pages) -> transcribe (page+box locators) -> put (label addressed)
-> finalize, then check page facts in the translation, scan coverage, child-segment boxes and the degradation record.

    python tests/check_scanned.py --scripts S --case bert|hubble --pdf PDF --work W --out O [--no-numpy] --report R
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = {
    "bert": {"pages": "5,6,12", "stem": "bert_p6", "facts": ["79.6", "82.1", "80.5", "72.8", "392k", "MNLI-(m/mm)", "平均"]},
    "hubble": {"pages": "3", "stem": "hubble_p170", "facts": ["+465 ± 50", "+513 ± 60", "306 km/s", "247 km/s", "10^{6}", "24 个天体", "9 组"]},
}
ap = argparse.ArgumentParser()
ap.add_argument("--scripts", required=True)
ap.add_argument("--case", required=True, choices=sorted(CASES))
ap.add_argument("--pdf", required=True)
ap.add_argument("--work", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--no-numpy", action="store_true", help="exercise the degraded path (LT_NO_NUMPY=1)")
ap.add_argument("--report", required=True)
a = ap.parse_args()
cfg = CASES[a.case]
env = dict(os.environ, PYTHONIOENCODING="utf-8")
if a.no_numpy:
    env["LT_NO_NUMPY"] = "1"
else:
    env.pop("LT_NO_NUMPY", None)
TR = os.path.join(a.scripts, "tr.py")
log = []


def tr(*args):
    r = subprocess.run([sys.executable, TR, "--work", a.work, *args], capture_output=True, text=True, encoding="utf-8", env=env)
    log.append({"args": list(args), "rc": r.returncode, "out": r.stdout[-3000:], "err": r.stderr[-1500:]})
    return r


tr("init", "--source", a.pdf, "--out", a.out, "--pages", cfg["pages"])
before = json.load(open(os.path.join(a.out, "result.json"), encoding="utf-8"))
t = tr("transcribe", os.path.join(HERE, "scanned", cfg["stem"] + ".transcription.txt"))
p = tr("put", os.path.join(HERE, "scanned", cfg["stem"] + ".zh.txt"), "--verbose")
tr("finalize")
res = json.load(open(os.path.join(a.out, "result.json"), encoding="utf-8"))
blob = json.dumps([s.get("translation") for s in res["segments"] if s.get("translation")], ensure_ascii=False)
kids = [s for s in res["segments"] if s.get("source_origin") == "agent_vision"]
page_img = os.path.exists(os.path.join(a.out, "previews")) and any(f.endswith(".png") for f in os.listdir(os.path.join(a.out, "previews")))
crops = [s.get("image") for s in res["segments"] if s["kind"] == "image_region" and s.get("image")]
rep = {"case": a.case, "numpy": not a.no_numpy, "status": res["status"], "status_reason": res["status_reason"],
       "errors": res["metrics"]["errors"], "warnings": res["metrics"]["warnings"],
       "facts_in_translation": {f: f in blob for f in cfg["facts"]},
       "facts_found": f"{sum(f in blob for f in cfg['facts'])}/{len(cfg['facts'])}",
       "transcribe_rc": t.returncode, "put_rc": p.returncode, "put_tail": p.stdout[-600:],
       "scan_before": (before["metrics"].get("scan") or {}), "scan_after": res["metrics"].get("scan"),
       "text_coverage": res["metrics"]["coverage"],
       "child_segments": [{"id": s["id"], "kind": s["kind"], "bbox": s["location"]["bbox"],
                           "granularity": s.get("location_granularity")} for s in kids],
       "children_with_own_bbox": sum(1 for s in res["segments"] if s.get("source_origin") == "agent_vision"),
       "page_previews_written": page_img, "region_crops": len(crops),
       "degraded_reason_in_result": any("numpy" in u for u in res.get("uncertainty", [])) or bool((res["metrics"].get("scan") or {}).get("degraded")),
       "pending_in_scope": [s["id"] for s in res["segments"] if s["status"] == "needs_transcription"],
       "log": log}
json.dump(rep, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps({k: v for k, v in rep.items() if k not in ("log", "child_segments", "put_tail")}, ensure_ascii=False))
