#!/usr/bin/env python3
"""output_contract typing regression (toy input): list/object/string/number/boolean fields, a number field set to
"unavailable" (must become a reported gap, not a silent string), a boolean given as "yes", an unmapped custom field.

    python tests/check_contract.py --scripts S --work W --report R
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--scripts", required=True)
ap.add_argument("--work", required=True)
ap.add_argument("--report", required=True)
a = ap.parse_args()
ex = os.path.join(a.scripts, "..", "examples")
os.makedirs(a.work + "_case", exist_ok=True)
case_dir = a.work + "_case"
with open(os.path.join(ex, "sample.md"), encoding="utf-8") as fh:
    open(os.path.join(case_dir, "paper.md"), "w", encoding="utf-8").write(fh.read())
CONTRACT = {"case_id": "string", "status": "string", "qa_checks": "list", "verification_notes": "list", "terminology": "list",
            "glossary_table": "object", "coverage_ratio": "number", "translation": "string",
            "confidence_level": "number", "is_complete": "boolean", "reviewer_comment": "string"}
json.dump({"case_id": "contract-types", "materials": ["paper.md"], "target_language": "zh", "output_contract": CONTRACT},
          open(os.path.join(case_dir, "case.json"), "w", encoding="utf-8"))
env = dict(os.environ, PYTHONIOENCODING="utf-8")
TR = os.path.join(a.scripts, "tr.py")
out = a.work + "_out"
for args in (["init", "--case", os.path.join(case_dir, "case.json"), "--out", out],
             ["put", os.path.join(ex, "sample_translation.zh.txt")],
             ["finalize", "--set", "confidence_level=unavailable", "--set", "is_complete=yes"]):
    subprocess.run([sys.executable, TR, "--work", a.work, *args], capture_output=True, env=env)
r = json.load(open(os.path.join(out, "result.json"), encoding="utf-8"))
want = {"string": str, "list": list, "object": dict, "number": (int, float), "boolean": bool}
fields = {}
for k, t in CONTRACT.items():
    v = r.get(k)
    ok = isinstance(v, want[t]) and not (t == "number" and isinstance(v, bool))
    fields[k] = {"declared": t, "delivered": type(v).__name__, "value": v if isinstance(v, (str, int, float, bool)) and len(str(v)) < 60 else None,
                 "type_ok": ok}
gaps = r.get("contract_gaps")
rep = {"fields": fields, "contract_type_coercions": r.get("contract_type_coercions"), "contract_gaps": gaps,
       "contract_fields_unfilled": r.get("contract_fields_unfilled"), "status_reason": r.get("status_reason"),
       # acceptance: every delivered value has its declared type OR is null with a recorded gap
       "all_typed_or_reported": all(f["type_ok"] or (f["delivered"] == "NoneType" and any(g.get("field") == k for g in (gaps or [])))
                                    for k, f in fields.items()),
       "silent_type_violations": [k for k, f in fields.items() if not f["type_ok"] and f["delivered"] != "NoneType"]}

# JSON Schema + deliverables contract regression (v0.3.1)
schema_case = os.path.join(case_dir, "case_schema.json")
SCHEMA_CONTRACT = {
    "type": "object",
    "required": ["case_id", "status", "verification_notes", "custom_summary"],
    "properties": {
        "case_id": {"type": "string"},
        "status": {"type": "string"},
        "verification_notes": {"type": "array", "items": {"type": "string"}},
        "glossary_table": {"type": "object"},
        "coverage_ratio": {"type": "number"},
        "is_complete": {"type": "boolean"},
    },
    "deliverables": {"bilingual_pairs": {"type": "array"}},
}
json.dump({"case_id": "contract-schema", "materials": ["paper.md"], "target_language": "zh", "output_contract": SCHEMA_CONTRACT},
          open(schema_case, "w", encoding="utf-8"))
sw, sout = a.work + "_schema", a.work + "_schema_out"
for args in (["init", "--case", schema_case, "--out", sout],
             ["put", os.path.join(ex, "sample_translation.zh.txt")],
             ["finalize", "--set", "custom_summary=done", "--set", "is_complete=yes"]):
    subprocess.run([sys.executable, TR, "--work", sw, *args], capture_output=True, env=env)
sr = json.load(open(os.path.join(sout, "result.json"), encoding="utf-8"))
schema_ok = (
    isinstance(sr.get("verification_notes"), list)
    and isinstance(sr.get("glossary_table"), dict)
    and isinstance(sr.get("coverage_ratio"), (int, float))
    and sr.get("is_complete") is True
    and isinstance(sr.get("bilingual_pairs"), list)
    and sr.get("custom_summary") == "done"
    and "properties" not in sr
    and "required" not in sr
    and not sr.get("contract_gaps")
)
rep["json_schema_contract_ok"] = schema_ok
rep["all_typed_or_reported"] = rep["all_typed_or_reported"] and schema_ok
json.dump(rep, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps({k: rep[k] for k in ("all_typed_or_reported", "json_schema_contract_ok", "silent_type_violations", "contract_gaps", "contract_type_coercions")},
                 ensure_ascii=False)[:1500])
