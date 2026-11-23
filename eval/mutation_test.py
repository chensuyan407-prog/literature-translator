#!/usr/bin/env python3
"""Adversarial test of the translation checker.

Takes a work dir that already holds reviewed translations (tr.py put), injects ONE known error at a
time into a copy of a segment translation, re-runs the checks and records whether the error was caught.
Clean (unmutated) translations are scored too, to report false alarms.

    python mutation_test.py --work /app/work/lt [--out mutation_report.json]

Mutation types (each applied to every segment where it is applicable):
  number_change   first Arabic number changed (3.57 -> 4.57)       expected: number_missing / number_added
  number_drop     first number deleted                             expected: number_missing
  placeholder_drop  one {{M/C/U}} placeholder deleted (formula/citation lost)   expected: placeholder_missing
  placeholder_dup   one placeholder duplicated                     expected: placeholder_extra
  negation_drop   Chinese negators removed (不/未/没有/无/非)       expected: negation_lost
  term_swap       locked term replaced by a forbidden variant      expected: term_violation
  crossref_change 图/表 N -> N+1                                   expected: crossref_mismatch / number_*
  untranslated    translation replaced by the source text          expected: untranslated_copy
  omission        second half of the translation deleted           expected: length_ratio / sentence_count / number_missing
  empty           translation emptied                              expected: empty
  duplicate       translation of the previous segment copied here  expected: duplicate_translation (document level)
"""
import argparse
import copy
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import lt_core as C  # noqa: E402
import lt_render as R  # noqa: E402

EXPECT = {
    "number_change": {"number_missing", "number_added", "number_count"},
    "number_drop": {"number_missing", "number_count", "crossref_mismatch"},
    "placeholder_drop": {"placeholder_missing"},
    "placeholder_dup": {"placeholder_extra"},
    "negation_drop": {"negation_lost"},
    "term_swap": {"term_violation"},
    "crossref_change": {"crossref_mismatch", "number_missing", "number_added"},
    "untranslated": {"untranslated_copy"},
    "omission": {"length_ratio", "sentence_count", "number_missing", "placeholder_missing", "crossref_mismatch", "term_violation", "negation_lost", "qualifier_check"},
    "empty": {"empty"},
    "duplicate": {"duplicate_translation", "length_ratio", "number_missing", "placeholder_missing", "placeholder_extra", "crossref_mismatch", "term_violation", "sentence_count"},
    # v0.2: relation errors that keep every number and placeholder intact
    "crossref_kind": {"crossref_mismatch", "crossref_kind_mismatch"},
    "negation_one": {"negation_lost", "negation_count", "negation_scope"},
    "qualifier_flip": {"qualifier_check", "quantifier_added"},
    "noun_swap": {"counted_noun_changed", "unit_changed", "unit_missing"},
}


def mutate(kind, seg, tgt, gl, prev_tgt):
    masked = C.PH_RE.sub(lambda m: "#" * len(m.group(0)), tgt)  # never edit inside {{M1}} placeholders
    if kind == "number_change":
        m = C.NUM_RE.search(masked)
        if not m:
            return None
        v = m.group(0)
        nv = str(int(v[0]) + 1 if v[0] != "9" else 1) + v[1:]
        return tgt[:m.start()] + nv + tgt[m.end():]
    if kind == "number_drop":
        m = re.search(r"(?<![A-Za-z0-9_.#])\d+(?:\.\d+)?%?", masked)
        return tgt[:m.start()] + tgt[m.end():] if m and C.numbers(seg["prot"]) else None
    if kind == "placeholder_drop":
        m = C.PH_RE.search(tgt)
        return tgt[:m.start()] + tgt[m.end():] if m else None
    if kind == "placeholder_dup":
        m = C.PH_RE.search(tgt)
        return tgt + " " + m.group(0) if m else None
    if kind == "negation_drop":
        if not C.NEG_EN.search(C.strip_ph(seg["prot"])):
            return None
        new = re.sub(r"并非|并不|不|没有|未能|未|无法|无|非", "", C.NEG_ZH_NOISE.sub(lambda m: m.group(0).replace("不", "\0").replace("无", "\1").replace("非", "\2"), tgt))
        new = new.replace("\0", "不").replace("\1", "无").replace("\2", "非")
        return new if new != tgt else None
    if kind == "term_swap":
        hits = C.find_terms(seg["prot"], gl)
        for e in gl:
            if e["source"] in hits and e.get("variants") and e["target"] in tgt and not e["keep_original"]:
                return tgt.replace(e["target"], e["variants"][0])
        return None
    if kind == "crossref_change":
        m = re.search(r"([图表])\s*(\d+)", tgt)
        return tgt[:m.start()] + f"{m.group(1)} {int(m.group(2)) + 1}" + tgt[m.end():] if m else None
    if kind == "untranslated":
        return seg["prot"] if len(C.latin_words(C.strip_ph(seg["prot"]))) >= 3 else None
    if kind == "omission":
        if len(C.latin_words(C.strip_ph(seg["prot"]))) < 25:
            return None
        cut = re.split(r"(?<=[。！？])", tgt)
        cut = [c for c in cut if c]
        return "".join(cut[: max(1, len(cut) // 2)]) if len(cut) >= 2 else None
    if kind == "empty":
        return ""
    if kind == "duplicate":
        return prev_tgt
    if kind == "crossref_kind":  # 图 3 / 式 (7) -> 表 3 / 表 (7): same number, wrong object
        m = re.search(r"(?<![代列报外发仪电时课统年形方公模格样正算等程版句试企意地构视制插附蓝])(图|式|公式)\s*([\(（]?\s*\d+)", tgt)
        if not m:
            return None
        return tgt[:m.start(1)] + ("表" if m.group(1) != "表" else "图") + tgt[m.end(1):]
    if kind == "negation_one":  # drop ONE negator when the source has two or more (scope error, counts still > 0)
        if len(C.NEG_EN.findall(C.strip_ph(seg["prot"]))) < 2:
            return None
        m = re.search(r"并不|并非|未能|没有|不|未|无法", C.NEG_ZH_NOISE.sub(lambda x: "#" * len(x.group(0)), tgt))
        return tgt[:m.start()] + tgt[m.end():] if m else None
    if kind == "qualifier_flip":  # 仅/只 -> 普遍 (widens a restricted claim)
        if not re.search(r"\b(only|solely|merely)\b", C.strip_ph(seg["prot"]), re.I):
            return None
        m = re.search(r"只有|仅仅|仅|只", tgt)
        return tgt[:m.start()] + "普遍" + tgt[m.end():] if m else None
    if kind == "noun_swap":  # counted noun after a number replaced by another one (tokens -> documents)
        m = C.RE_COUNT_NOUN.search(C.strip_ph(seg["prot"]))
        if not m:
            return None
        noun = m.group(2).lower()
        alts = C.COUNT_NOUNS.get(noun) or C.COUNT_NOUNS.get(re.sub(r"(es|s)$", "", noun))
        if not alts:
            return None
        hit = re.search(alts, tgt, re.I)
        if not hit:
            return None
        repl = "篇文档" if "文档" not in alts else "个参数"
        return tgt[:hit.start()] + repl + tgt[hit.end():]
    return None


def detected(kind, issues):
    codes = {i["code"] for i in issues}
    return bool(codes & EXPECT[kind]), sorted(codes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    st = C.read_json(os.path.join(a.work, "state.json"))
    segs = C.read_jsonl(os.path.join(a.work, "segments.jsonl"))
    trans = C.read_json(os.path.join(a.work, "translations.json"), {})
    gl = C.compile_glossary(C.read_json(os.path.join(a.work, "glossary.json"), []))
    sl, tl = st["src_lang"], st["tgt_lang"]
    done = [s for s in segs if s.get("translate") and trans.get(s["id"], {}).get("tgt")]
    clean_issues = {s["id"]: C.check_segment(s, trans[s["id"]]["tgt"], gl, sl, tl) for s in done}
    fp_err = sum(1 for v in clean_issues.values() if any(i["severity"] == "error" for i in v))
    fp_warn = sum(1 for v in clean_issues.values() if any(i["severity"] == "warn" for i in v))
    rows, summary = [], {}
    prev = None
    for s in done:
        tgt = trans[s["id"]]["tgt"]
        for kind in EXPECT:
            if kind == "duplicate":
                if prev is None or prev[1] == tgt:
                    continue
                t2 = copy.deepcopy(trans)
                t2[s["id"]]["tgt"] = prev[1]
                iss, _, _ = R.compute(segs, t2, gl, sl, tl)
                hit, codes = detected(kind, iss.get(s["id"], []))
            else:
                mt = mutate(kind, s, tgt, gl, None)
                if mt is None or mt == tgt:
                    continue
                iss = C.check_segment(s, mt, gl, sl, tl)
                hit, codes = detected(kind, iss)
            d = summary.setdefault(kind, {"applied": 0, "detected": 0})
            d["applied"] += 1
            d["detected"] += int(hit)
            rows.append({"segment": s["id"], "mutation": kind, "detected": hit, "codes": codes})
        prev = (s["id"], tgt)
    for d in summary.values():
        d["detection_rate"] = round(d["detected"] / d["applied"], 3) if d["applied"] else None
    tot_a = sum(d["applied"] for d in summary.values())
    tot_d = sum(d["detected"] for d in summary.values())
    rep = {"work": os.path.abspath(a.work), "segments_tested": len(done), "mutations_applied": tot_a, "mutations_detected": tot_d,
           "overall_detection_rate": round(tot_d / tot_a, 3) if tot_a else None, "by_type": summary,
           "clean_segments_with_error_flags": fp_err, "clean_segments_with_warn_flags": fp_warn,
           "clean_false_alarm_rate_error": round(fp_err / len(done), 3) if done else None,
           "clean_issue_codes": {k: [i["code"] for i in v] for k, v in clean_issues.items() if v},
           "missed": [r for r in rows if not r["detected"]]}
    print(json.dumps({k: v for k, v in rep.items() if k not in ("missed", "clean_issue_codes")}, ensure_ascii=False, indent=1))
    if rep["missed"]:
        print("missed:", "; ".join(f"{r['segment']}:{r['mutation']}" for r in rep["missed"][:30]))
    if a.out:
        C.write_json(a.out, rep)


if __name__ == "__main__":
    main()
