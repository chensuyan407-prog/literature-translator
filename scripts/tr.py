#!/usr/bin/env python3
"""literature-translator CLI — glossary-locked, paragraph-aligned, self-checking translation of papers.

  python tr.py init [--case case.json] [--source paper.pdf] [--target zh|en] [--pages 1-4]
                    [--sections "Introduction..Conclusion"] [--glossary terms.tsv] [--password PW]
  python tr.py glossary FILE          # lock terms: `source => target ! forbidden1|forbidden2`
  python tr.py batch [--units N]      # print the next batch to translate
  python tr.py put FILE [--next]      # store translations (`@@ S0012` blocks), check them, refresh outputs
  python tr.py check | issues | show S0012 | enforce | status
  python tr.py finalize [--set key=value] [--set-json key=file.json]
  python tr.py audit TRANSLATION.md   # score any other translation of the same source (A/B comparison)

State lives in --work (default /app/work/lt), deliverables in --out (default /app/output).
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lt_core as C  # noqa: E402
import lt_render as R  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SRC_EXT = (".pdf", ".txt", ".md", ".markdown", ".tex", ".docx", ".html", ".htm")
GLOSS_HINT = re.compile(r"gloss|term|术语|vocab|dictionar|词表", re.I)


def default_dir(kind):
    env = os.environ.get("LT_WORK" if kind == "work" else "LT_OUT")
    if env:
        return env
    if os.path.isdir("/app"):
        return "/app/work/lt" if kind == "work" else "/app/output"
    return os.path.abspath("lt_work" if kind == "work" else "lt_output")


class Ctx:
    def __init__(self, work):
        self.work = work
        self.p = lambda *a: os.path.join(work, *a)
        self.st = C.read_json(self.p("state.json"), {})
        self.segs = C.read_jsonl(self.p("segments.jsonl"))
        self.trans = C.read_json(self.p("translations.json"), {})
        self.gl = C.compile_glossary(C.read_json(self.p("glossary.json"), []))
        self.idx = {s["id"]: i for i, s in enumerate(self.segs)}

    @property
    def out(self):
        return self.st["out"]

    def save_state(self):
        C.write_json(self.p("state.json"), self.st)

    def save_trans(self):
        C.write_json(self.p("translations.json"), self.trans)

    def save_gl(self):
        C.write_json(self.p("glossary.json"), [{k: v for k, v in e.items() if not k.startswith("_") and k != "rx"} for e in self.gl])

    def change(self, **kw):
        kw["ts"] = C.now_iso()
        C.append_jsonl(self.p("changes.jsonl"), kw)

    def elapsed(self):
        return round(time.time() - self.st.get("started", time.time()), 1)


# ---------------------------------------------------------------- case discovery
def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def discover(case_path, explicit_source):
    case = C.read_json(case_path, {}) if case_path and os.path.exists(case_path) else {}
    base = os.path.dirname(os.path.abspath(case_path)) if case_path else os.getcwd()
    roots = [base, "/input", os.path.join(base, "materials"), "/input/materials", os.getcwd()]
    files = []
    for s in _walk_strings(case):
        if len(s) < 260 and re.search(r"\.(pdf|txt|md|markdown|tex|docx|html?|tsv|csv|xlsx|json)$", s, re.I):
            for r in roots:
                cand = s if os.path.isabs(s) else os.path.join(r, s)
                if os.path.isfile(cand):
                    files.append(os.path.abspath(cand))
                    break
    if not files:
        for r in (base, os.path.join(base, "materials"), "/input", "/input/materials"):
            if os.path.isdir(r):
                for fn in sorted(os.listdir(r)):
                    fp = os.path.join(r, fn)
                    if os.path.isfile(fp) and fn not in ("case.json", "task.md") and fn.lower().endswith(SRC_EXT + (".tsv", ".csv", ".xlsx")):
                        files.append(os.path.abspath(fp))
    files = list(dict.fromkeys(files))
    gloss = [f for f in files if GLOSS_HINT.search(os.path.basename(f)) or f.lower().endswith((".tsv", ".csv", ".xlsx"))]
    srcs = [f for f in files if f not in gloss and f.lower().endswith(SRC_EXT)]
    if explicit_source:
        source = os.path.abspath(explicit_source)
    else:
        pdfs = [f for f in srcs if f.lower().endswith(".pdf")]
        source = max(pdfs, key=os.path.getsize) if pdfs else (max(srcs, key=os.path.getsize) if srcs else None)
    task_text = ""
    for tp in (os.path.join(base, "task.md"), "/input/task.md", os.path.join(os.getcwd(), "task.md")):
        if os.path.exists(tp):
            with open(tp, encoding="utf-8", errors="replace") as fh:
                task_text = fh.read()
            break
    return case, source, gloss, [f for f in srcs if f != source], task_text


def guess_target(case, task_text, src_lang):
    blob = json.dumps(case, ensure_ascii=False) + "\n" + task_text
    m = re.search(r'"(?:target_lang(?:uage)?|tgt_lang|target|to_lang|output_language|目标语言)"\s*:\s*"([^"]+)"', blob, re.I)
    val = m.group(1).lower() if m else ""
    if re.search(r"^(zh|cn|chinese|中文|简体)", val) or (not val and re.search(r"(译|翻译)(成|为|至)\s*(简体)?中文|into (simplified )?chinese|to chinese|英译中|英文.{0,6}中文", blob, re.I)):
        return "zh"
    if re.search(r"^(en|english|英文|英语)", val) or (not val and re.search(r"(译|翻译)(成|为|至)\s*英(文|语)|into english|to english|中译英", blob, re.I)):
        return "en"
    return "zh" if src_lang != "zh" else "en"


def guess_scope(case, task_text):
    """Return (pages, sections) restrictions found in the case, or (None, None)."""
    pages = sections = None
    for k in ("pages", "page_range", "page", "页码", "页"):
        v = case.get(k) if isinstance(case, dict) else None
        if v:
            pages = v if isinstance(v, str) else (f"{v[0]}-{v[-1]}" if isinstance(v, list) and v else str(v))
    for k in ("sections", "section", "range", "paragraphs", "章节", "范围"):
        v = case.get(k) if isinstance(case, dict) else None
        if v and isinstance(v, (str, list)):
            sections = v if isinstance(v, str) else "|".join(map(str, v))
    return pages, sections


def parse_pages(spec):
    if not spec:
        return None
    out = []
    for part in re.split(r"[,，;]", str(spec)):
        part = part.strip()
        m = re.match(r"^(\d+)\s*[-–~至到]\s*(\d+)$", part)
        if m:
            out.extend(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            out.append(int(part))
    return sorted(set(out)) or None


def compact_pages(pages):
    out, run = [], [pages[0]]
    for p in pages[1:]:
        if p == run[-1] + 1:
            run.append(p)
        else:
            out.append(run)
            run = [p]
    out.append(run)
    return ",".join(f"{r[0]}-{r[-1]}" if len(r) > 1 else str(r[0]) for r in out)


def apply_scope(segs, pages, sections, ids, max_units):
    note = []
    if pages:
        ps = set(pages)
        for s in segs:
            if s.get("pages") and not (set(s["pages"]) & ps):
                s["in_scope"], s["scope_reason"] = False, f"outside pages {compact_pages(pages)}"
        note.append("pages " + compact_pages(pages))
    if sections:
        groups = [g.strip() for g in re.split(r"\|", sections) if g.strip()]
        keep = set()
        for g in groups:
            a, _, b = g.partition("..")
            a, b = a.strip().lower(), (b or a).strip().lower()
            start = next((i for i, s in enumerate(segs) if s["kind"] in ("heading", "title") and a in s["src"].lower()), None)
            if start is None:
                start = next((i for i, s in enumerate(segs) if a in (s.get("section") or "").lower()), None)
            if start is None:
                note.append(f"section '{a}' not found")
                continue
            end = max((i for i, s in enumerate(segs) if b in (s.get("section") or "").lower() or
                       (s["kind"] == "heading" and b in s["src"].lower())), default=start)
            keep.update(range(start, max(start, end) + 1))
        if keep:
            for i, s in enumerate(segs):
                if i not in keep:
                    s["in_scope"], s["scope_reason"] = False, f"outside sections {sections}"
            note.append(f"sections {sections}")
    if ids:
        m = re.match(r"S?(\d+)\s*-\s*S?(\d+)", ids)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            for s in segs:
                if not lo <= C.seg_num(s["id"])[0] <= hi:
                    s["in_scope"], s["scope_reason"] = False, f"outside ids {ids}"
            note.append(f"ids {ids}")
    if max_units:
        tot = 0
        for s in segs:
            if s.get("translate") and s.get("in_scope", True):
                tot += s["units"]
                if tot > max_units:
                    s["in_scope"], s["scope_reason"] = False, f"beyond --max-units {max_units}"
        note.append(f"max {max_units} units")
    return ", ".join(note) or "whole document"


# ---------------------------------------------------------------- outputs
def refresh(ctx, quiet=True, extra=None):
    st, segs, trans, gl = ctx.st, ctx.segs, ctx.trans, ctx.gl
    out = ctx.out
    os.makedirs(out, exist_ok=True)
    st["elapsed_s"] = ctx.elapsed()
    issues, metrics, tstats = R.compute(segs, trans, gl, st["src_lang"], st["tgt_lang"])
    dropped = C.read_jsonl(ctx.p("dropped.jsonl"))
    changes = C.read_jsonl(ctx.p("changes.jsonl"))
    st["outputs"] = {"result": "result.json", "bilingual_markdown": "bilingual.md", "bilingual_html": "bilingual.html",
                     "translation_markdown": f"translation.{st['tgt_lang']}.md", "alignment": "alignment.jsonl",
                     "glossary": "glossary.tsv", "qa_report": "qa_report.json", "manual_review": "manual_review.md",
                     "dropped_layout_elements": "dropped.jsonl", "change_log": "changes.jsonl",
                     "page_previews": "previews/" if os.path.isdir(os.path.join(out, "previews")) else None}
    R.write_bilingual_md(os.path.join(out, "bilingual.md"), st, segs, trans, issues, metrics)
    R.write_bilingual_html(os.path.join(out, "bilingual.html"), st, segs, trans, issues, metrics)
    R.write_target_md(os.path.join(out, f"translation.{st['tgt_lang']}.md"), st, segs, trans)
    review = R.write_manual_review(os.path.join(out, "manual_review.md"), segs, trans, issues, dropped, st)
    with open(os.path.join(out, "alignment.jsonl"), "w", encoding="utf-8") as fh:
        for s in segs:
            t = trans.get(s["id"], {}).get("tgt")
            fh.write(json.dumps({"id": s["id"], "kind": s["kind"], "status": R.seg_status(s, trans), "pages": s.get("pages"),
                                 "bbox": s.get("bbox"), "blocks": s.get("blocks"), "section": s.get("section"),
                                 "source": C.restore(s.get("prot", ""), s.get("ph", {})),
                                 "translation": C.restore(t, s["ph"]) if t else None,
                                 "source_sentences": s.get("sentences"),
                                 "issues": [i["code"] for i in issues.get(s["id"], [])]}, ensure_ascii=False) + "\n")
    with open(os.path.join(out, "glossary.tsv"), "w", encoding="utf-8") as fh:
        fh.write("source\ttarget\tforbidden_variants\torigin\tsegments_with_term\tsegments_consistent\tnote\n")
        for v in tstats.values():
            if v["segments_with_term"] or v["origin"] != "base":
                fh.write(f"{v['source']}\t{'=' if v['keep_original'] else v['target']}\t{'|'.join(v['variants'])}\t{v['origin']}\t"
                         f"{v['segments_with_term']}\t{v['segments_consistent']}\t{v['note']}\n")
    C.write_json(os.path.join(out, "qa_report.json"), {"metrics": metrics, "issues": issues,
                                                        "terms": list(tstats.values())})
    rs = os.path.join(out, "resume_state")
    os.makedirs(rs, exist_ok=True)
    for name in ("segments.jsonl", "translations.json", "glossary.json", "state.json", "changes.jsonl", "dropped.jsonl"):
        if os.path.exists(ctx.p(name)):  # the change log travels with the state: a resumed run keeps every earlier revision
            with open(ctx.p(name), encoding="utf-8") as a_, open(os.path.join(rs, name), "w", encoding="utf-8") as b_:
                b_.write(a_.read())
    for name in ("dropped.jsonl", "changes.jsonl"):
        src = ctx.p(name)
        if os.path.exists(src):
            with open(src, encoding="utf-8") as a, open(os.path.join(out, name), "w", encoding="utf-8") as b:
                b.write(a.read())
    extra_fields = dict(st.get("extra_fields", {}))
    extra_fields.update(extra or {})
    res = R.build_result(st, segs, trans, gl, issues, metrics, tstats, dropped, changes, review, extra_fields)
    C.write_json(os.path.join(out, "result.json"), res)
    ctx.save_state()
    return res, issues, metrics


# ---------------------------------------------------------------- commands
def cmd_init(a):
    t0 = time.time()
    work = os.path.abspath(a.work)
    os.makedirs(work, exist_ok=True)
    for fn in ("segments.jsonl", "translations.json", "glossary.json", "changes.jsonl", "dropped.jsonl", "state.json"):
        if os.path.exists(os.path.join(work, fn)) and not a.keep:
            os.remove(os.path.join(work, fn))
    case, source, gloss_files, other_srcs, task_text = discover(a.case, a.source)
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    st = {"version": C.VERSION, "case_id": (case.get("case_id") or case.get("id")) if isinstance(case, dict) else None,
          "contract": case.get("output_contract") if isinstance(case, dict) else None, "out": out, "work": work,
          "started": t0 if not a.started else float(a.started), "started_iso": C.now_iso(), "budget_s": a.budget,
          "batch_units": a.batch_units, "n_puts": 0, "n_batches": 0, "uncertainty": [], "source": source or "",
          "other_sources": other_srcs, "src_lang": "en", "tgt_lang": a.target or "zh"}
    ctx = Ctx(work)
    ctx.st = st
    if not source or not os.path.exists(source):
        st["fatal"] = f"no source document found (case={a.case}); pass --source FILE"
        st["src_lang"], st["tgt_lang"] = "en", a.target or "zh"
        ctx.segs = []
        res, _, _ = refresh(ctx)
        print(f"FAILED: {st['fatal']}\nresult.json written with status=failed -> {out}")
        return 2
    st["sha256"] = C.sha256_file(source)
    ext = os.path.splitext(source)[1].lower()
    st["format"] = ext.lstrip(".")
    pages = parse_pages(a.pages)
    case_pages, case_sections = guess_scope(case if isinstance(case, dict) else {}, task_text)
    if not pages and case_pages and not a.ignore_case_scope:
        pages = parse_pages(case_pages)
    sections = a.sections or (case_sections if not a.ignore_case_scope else None)
    if ext == ".pdf":
        segs, dropped, parsed = C.segments_from_pdf(source, work, password=a.password, pages=None, ocr=a.ocr)
        st["parser"] = (parsed.get("meta") or {}).get("parser")
        st["page_count"] = (parsed.get("meta") or {}).get("page_count")
        if parsed.get("status") == "failed":
            st["fatal"] = f"{parsed.get('error')}: {parsed.get('message')}"
        for w in parsed.get("warnings", [])[:20]:
            st["uncertainty"].append(f"parser warning p.{w.get('page', '-')}: {w.get('code')} {w.get('detail', '')}"[:300])
    else:
        segs, dropped, parsed = C.segments_from_text(source)
        st["parser"] = "text/markdown/latex/docx splitter"
    src_text = " ".join(C.strip_ph(s.get("prot", "")) for s in segs if s.get("translate"))[:200000]
    st["src_lang"] = a.source_lang or (C.detect_lang(src_text) if src_text else "en")
    if st["src_lang"] == "und":
        st["src_lang"] = "en"
    st["tgt_lang"] = a.target or guess_target(case if isinstance(case, dict) else {}, task_text, st["src_lang"])
    segs = C.finish_segments(segs, st["tgt_lang"])
    st["scope"] = apply_scope(segs, pages, sections, a.ids, a.max_units)
    title = next((s["src"] for s in segs if s["kind"] == "title"), None)
    st["title"] = title
    ctx.segs = segs
    with open(ctx.p("segments.jsonl"), "w", encoding="utf-8") as fh:
        for s in segs:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    with open(ctx.p("dropped.jsonl"), "w", encoding="utf-8") as fh:
        for d in dropped:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    # glossaries: case-provided > (agent, later) > base
    case_gl = []
    for g in gloss_files + (a.glossary or []):
        case_gl += C.load_glossary_file(g, "case")
    base = C.load_base_glossary(st["src_lang"], st["tgt_lang"]) if not a.no_base else []
    base_c = C.compile_glossary([dict(e) for e in base])
    alltext = "\n".join(s["prot"] for s in segs if s.get("translate") and s.get("in_scope", True))
    base_hits = C.find_terms(alltext, base_c)
    base_used = [e for e in base if e["source"] in base_hits]
    ctx.gl = C.compile_glossary(C.merge_glossaries(case_gl, base_used))
    ctx.save_gl()
    st["glossary_sources"] = {"case": [os.path.basename(g) for g in gloss_files + (a.glossary or [])], "case_terms": len(case_gl),
                              "base_terms_hit": len(base_used)}
    if ext == ".pdf" and not st.get("fatal") and not a.no_preview:
        try:
            scope_pages = sorted({p for s in segs if s.get("in_scope", True) for p in (s.get("pages") or [])})
            R.render_previews(source, segs, os.path.join(out, "previews"), scope_pages, password=a.password)
        except Exception as ex:
            st["uncertainty"].append(f"preview rendering failed: {ex}")
    if ext == ".pdf" and not st.get("fatal"):
        try:
            n_crops = R.render_crops(source, segs, out, password=a.password)
            st["crops"] = n_crops
            with open(ctx.p("segments.jsonl"), "w", encoding="utf-8") as fh:
                for s in segs:
                    fh.write(json.dumps(s, ensure_ascii=False) + "\n")
        except Exception as ex:
            st["uncertainty"].append(f"crop rendering failed: {ex}")
    st["reserve_s"] = a.reserve
    st["elapsed_s"] = ctx.elapsed()
    ctx.st = st
    refresh(ctx)
    # ---- report
    kinds = Counter(s["kind"] for s in segs)
    todo = [s for s in segs if s.get("translate") and s.get("in_scope", True)]
    units = sum(s["units"] for s in todo)
    nb = max(1, -(-units // a.batch_units))
    print(f"SOURCE {os.path.basename(source)} ({st.get('format')}, {st.get('page_count') or '-'} pages) lang {st['src_lang']} -> {st['tgt_lang']}"
          f"  case_id={st['case_id']}  scope: {st['scope']}")
    if st.get("fatal"):
        print(f"FATAL: {st['fatal']}  -> result.json status=failed. Ask for a readable/decrypted file or password (--password).")
        return 2
    print("segments: " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()) + f"; dropped layout elements={len(dropped)}")
    print(f"to translate: {len(todo)} segments, ~{units} {'words' if st['src_lang'] == 'en' else 'units'} -> ~{nb} batches of {a.batch_units}")
    miss = [s for s in segs if s["kind"] == "missing_page"]
    if miss:
        print(f"WARNING: {len(miss)} page(s) without text layer (OCR unavailable): {[s['page'] for s in miss]} -> status will be partial")
    regs = [s for s in segs if s["kind"] == "image_region" and s.get("in_scope", True)]
    if regs:
        if any(s.get("degraded") for s in regs):
            print("DEGRADED: " + next(s["degraded"] for s in regs if s.get("degraded")))
        pages_r = sorted({s["page"] for s in regs})
        print(f"SCANNED: {len(regs)} region(s) on page(s) {pages_r} have no text layer and OCR is unavailable. Each region has a crop "
              f"image ({out}/crops/Sxxxx.png) and the page preview shows its id. If you can read images: `tr.py regions`, then write "
              "`@@ ID` blocks with the transcribed source text and run `tr.py transcribe FILE` (or `@@ ID` + `UNREADABLE: reason` / "
              "`NONTEXT: figure`). Otherwise they stay listed as needs_transcription (status partial).")
    eqs = [s for s in segs if s["kind"] == "equation" and s.get("in_scope", True)]
    if eqs:
        bad = [s["id"] for s in eqs if s.get("eq_status") == "needs_confirmation"]
        print(f"EQUATIONS: {len(eqs)} display equations kept verbatim; {len(bad)} need confirmation against their crop image"
              + (f" ({', '.join(bad[:10])}{' ...' if len(bad) > 10 else ''})" if bad else ""))
    if units > 9000:
        print("TIME: long document. 900 s rarely covers > ~8000 words. Translate in order; finalize early; untranslated parts are reported honestly.")
    cands = C.extract_term_candidates(todo, st["src_lang"])
    have = {e["source"].lower() for e in ctx.gl}
    print("\nTERM CANDIDATES (freq kind term) - lock the domain terms you are sure about with `tr.py glossary FILE`:")
    line = []
    for c in cands:
        if c["term"].lower() in have:
            continue
        line.append(f"{c['freq']}x {c['term']}" + (f" ({c['acronym']})" if c.get("acronym") else ""))
    print("; ".join(line[:70]))
    if ctx.gl:
        print(f"\nALREADY LOCKED ({len(ctx.gl)}; case>agent>base; override base by listing the term in your glossary): " +
              "; ".join(f"{e['source']}=>{e['target']}" for e in ctx.gl[:45]) + (" ..." if len(ctx.gl) > 45 else ""))
    print(f"\noutputs (refreshed after every put): {out}/result.json bilingual.md bilingual.html manual_review.md")
    print("NEXT: write glossary lines `english term => 中文译名 ! 禁用译法1|禁用译法2` (`=> =` keeps the term in English), "
          "run `tr.py glossary FILE`, then `tr.py batch`.")
    return 0


def cmd_glossary(a):
    ctx = Ctx(a.work)
    new = []
    for f in a.files:
        new += C.load_glossary_file(f, "agent")
    if not new:
        print("no glossary lines parsed; format: `source => target ! variant1|variant2`")
        return 1
    old = {e["source"].lower(): e for e in ctx.gl}
    changed = []
    for e in new:
        o = old.get(e["source"].lower())
        if o and o["origin"] == "case" and o["target"] != e["target"]:
            print(f"KEPT case glossary: {o['source']} => {o['target']} (yours: {e['target']} ignored)")
            continue
        if o and o["target"] != e["target"] and o["origin"] != "base":
            e["variants"] = list(dict.fromkeys(e["variants"] + [o["target"]] + o.get("variants", [])))
            changed.append((e["source"], o["target"], e["target"]))
        if o and o["origin"] == "base" and o["target"] != e["target"]:
            e["variants"] = list(dict.fromkeys(e["variants"] + [o["target"]]))
        old[e["source"].lower()] = e
    case = [e for e in old.values() if e["origin"] == "case"]
    agent = [e for e in old.values() if e["origin"] == "agent"]
    base = [e for e in old.values() if e["origin"] == "base"]
    ctx.gl = C.compile_glossary(C.merge_glossaries(case, agent, base))
    ctx.save_gl()
    for s, o, n in changed:
        ctx.change(action="glossary_update", term=s, old=o, new=n)
    alltext = "\n".join(s["prot"] for s in ctx.segs if s.get("translate") and s.get("in_scope", True))
    hits = C.find_terms(alltext, ctx.gl)
    unused = [e["source"] for e in ctx.gl if e["origin"] == "agent" and e["source"] not in hits]
    print(f"glossary: {len(case)} case + {len(agent)} agent + {len(base)} base terms locked; "
          f"{sum(1 for e in ctx.gl if e['source'] in hits)} occur in scope.")
    if unused:
        print("not found in source (check spelling/singular form): " + "; ".join(unused[:20]))
    if changed:
        print(f"{len(changed)} term(s) changed after translation started -> run `tr.py enforce` to back-fill: " +
              "; ".join(f"{s}: {o}->{n}" for s, o, n in changed))
    if ctx.trans:
        refresh(ctx)
    return 0


def schedule(ctx):
    """Throughput-based plan: units/s measured from init to now over translated units."""
    budget, reserve = ctx.st.get("budget_s", 900), ctx.st.get("reserve_s", 120)
    el = ctx.elapsed()
    todo = [s for s in ctx.segs if s.get("translate") and s.get("in_scope", True) and not s.get("needs_transcription")]
    done_u = sum(s["units"] for s in todo if ctx.trans.get(s["id"], {}).get("tgt"))
    left_u = sum(s["units"] for s in todo if not ctx.trans.get(s["id"], {}).get("tgt"))
    rate = done_u / el if done_u and el > 0 else None
    proj = el + left_u / rate if rate else None
    return {"elapsed": el, "budget": budget, "reserve": reserve, "time_left": budget - reserve - el, "done_units": done_u,
            "left_units": left_u, "rate": rate, "projected": proj, "overrun": bool(proj and proj > budget - reserve)}


def next_batch(ctx, units=None, kinds=None, ids=None, redo=False, plan=True):
    units = units or ctx.st.get("batch_units", 1400)
    sch = schedule(ctx) if plan and not (kinds or ids or redo) else None
    if sch and sch["rate"]:
        fit = int(sch["rate"] * max(0.0, sch["time_left"]) * 0.85)
        if fit < 40:
            ctx.st["stop_reason"] = f"budget: {sch['elapsed']:.0f}s used, reserve {sch['reserve']}s kept for check/finalize"
            return [], 0
        units = min(units, max(80, fit))
        nxt = next((s for s in ctx.segs if s.get("translate") and s.get("in_scope", True) and not s.get("needs_transcription")
                    and not ctx.trans.get(s["id"], {}).get("tgt")), None)
        if nxt and nxt["units"] > 1.5 * fit and fit < 0.5 * ctx.st.get("batch_units", 1400):
            ctx.st["stop_reason"] = (f"budget: {sch['elapsed']:.0f}s used; next segment {nxt['id']} ({nxt['units']} units) does not fit "
                                     f"the ~{fit} units left before the {sch['reserve']}s reserve")
            return [], 0
        if sch["overrun"] and not ctx.st.get("structure_pass_done"):
            # time will run out: translate all remaining titles and headings first (short; keeps the document navigable),
            # then body paragraphs and captions in reading order
            kinds = {"title", "heading"}
            units = max(units, sum(s["units"] for s in ctx.segs if s["kind"] in kinds and s.get("in_scope", True)))
            ctx.st["structure_pass_done"] = True
            ctx.st["structure_pass_at"] = round(sch["elapsed"], 1)
    pick, tot = [], 0
    lo = hi = None
    if ids:
        m = re.match(r"S?(\d+)\s*-\s*S?(\d+)", ids)
        lo, hi = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    for s in ctx.segs:
        if not s.get("translate") or not s.get("in_scope", True) or s.get("needs_transcription"):
            continue
        if not redo and ctx.trans.get(s["id"], {}).get("tgt"):
            continue
        if kinds and s["kind"] not in kinds:
            continue
        if lo is not None and not lo <= C.seg_num(s["id"])[0] <= hi:
            continue
        if pick and tot + s["units"] > units:
            if kinds:
                continue
            break
        pick.append(s)
        tot += s["units"]
    if not pick and kinds and sch:  # structure pass found nothing: fall back to body order
        return next_batch(ctx, units, None, None, False, plan=False)
    return pick, tot


def print_batch(ctx, pick, tot):
    pend = [s["id"] for s in ctx.segs if s.get("needs_transcription") and s.get("in_scope", True)]
    if pend:
        print(f"NOTE: {len(pend)} scanned region(s) have no text yet ({', '.join(pend[:8])}{' ...' if len(pend) > 8 else ''}): "
              "`tr.py regions` lists their images; `tr.py transcribe FILE` adds the text.")
    if not pick:
        if ctx.st.get("stop_reason"):
            print(f"STOP ({ctx.st['stop_reason']}). Run `tr.py finalize` now; untranslated segments are reported with their ids "
                  "and result.json.resume tells how to continue later.")
            ctx.save_state()
            return
        print("NO MORE SEGMENTS TO TRANSLATE in scope. Next: `tr.py check`, fix errors with `put`, then `tr.py finalize`.")
        return
    if set(s["kind"] for s in pick) <= {"title", "heading"} and ctx.st.get("structure_pass_at") is not None \
            and ctx.st.get("structure_pass_printed") is None:
        ctx.st["structure_pass_printed"] = True
        print("SCHEDULE: projected time exceeds the budget -> this batch holds all remaining titles and headings; "
              "body paragraphs and captions continue in reading order afterwards.")
    ctx.st["n_batches"] = ctx.st.get("n_batches", 0) + 1
    ctx.st["last_batch_ids"] = [s["id"] for s in pick]
    ctx.st["last_batch_at"] = time.time()
    ctx.save_state()
    remaining = sum(s["units"] for s in ctx.segs if s.get("translate") and s.get("in_scope", True) and not ctx.trans.get(s["id"], {}).get("tgt"))
    tl = "中文" if ctx.st["tgt_lang"] == "zh" else "English"
    print(f"=== BATCH {ctx.st['n_batches']}: {pick[0]['id']}..{pick[-1]['id']} ({len(pick)} segments, ~{tot} units; {remaining - tot} units left after this) ===")
    text = "\n".join(s["prot"] for s in pick)
    hits = C.find_terms(text, ctx.gl)
    if hits:
        print("LOCKED TERMS (must use exactly): " + "; ".join(f"{e['source']} => {e['target']}" for e in ctx.gl if e["source"] in hits))
    first = ctx.idx[pick[0]["id"]]
    prev = next((ctx.segs[i] for i in range(first - 1, -1, -1) if ctx.trans.get(ctx.segs[i]["id"], {}).get("tgt")), None)
    if prev:
        print(f"CONTEXT (previous {prev['id']}, already translated): …{ctx.trans[prev['id']]['tgt'][-120:]}")
    print(f"RULES: translate into {tl}; one `@@ ID` block per segment, same order; keep every {{{{M1}}}}/{{{{C1}}}}/{{{{U1}}}} placeholder exactly once; "
          "keep all numbers, units, Fig./Table/Eq./Section numbers, acronyms; do not merge, split or skip segments; tables: same rows and ' | ' cells; "
          "if a part is unclear write it and append ⟪?⟫ (flagged for review).")
    for s in pick:
        tag = f"@@ {s['id']}"
        meta = f"[{s['kind']}{'/' + s['subkind'] if s.get('subkind') else ''} · {R.loc_str(s)}]"
        print(f"{tag}  {meta}")
        if s.get("mixed_note"):
            print(f"   (mixed language: {s['mixed_note']})")
        print(s["prot"])
        if s.get("ph"):
            ms = [f"{k}={v[:50]}" for k, v in s["ph"].items() if k[0] == "M"]
            if ms:
                print("   (math: " + "; ".join(ms) + ")")
    print(f"=== END BATCH {ctx.st['n_batches']} — write the {tl} blocks to a file and run: tr.py put FILE --next ===")


def cmd_batch(a):
    ctx = Ctx(a.work)
    kinds = set(a.kinds.split(",")) if a.kinds else None
    pick, tot = next_batch(ctx, a.units, kinds, a.ids, a.redo)
    print_batch(ctx, pick, tot)
    return 0


def parse_put(text):
    blocks, cur, buf = [], None, []
    for line in text.splitlines():
        m = re.match(r"^\s*@@\s*(S\d{3,5}(?:\.\d+)?|#[\w.\-]+?)(?![\d.\w\-]).*$", line)
        if m:
            if cur:
                blocks.append((cur, "\n".join(buf).strip()))
            cur, buf = m.group(1), []
        elif cur:
            buf.append(line)
    if cur:
        blocks.append((cur, "\n".join(buf).strip()))
    return blocks


def cmd_put(a):
    ctx = Ctx(a.work)
    raw = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8", errors="replace").read()
    blocks = parse_put(raw)
    if not blocks:
        print("ERROR: no `@@ S0001` blocks found in the file. Format: a line `@@ S0012`, then the translation, then the next `@@` line.")
        return 1
    seen, stored, rejected = Counter(), [], []
    labels = {s.get("label"): s["id"] for s in ctx.segs if s.get("label")}
    for sid, tgt in blocks:
        if sid.startswith("#"):  # transcribed segment addressed by its label (tr.py transcribe ... #label)
            sid = labels.get(sid[1:], sid)
        seen[sid] += 1
        if sid not in ctx.idx:
            rejected.append(f"{sid}: unknown id")
            continue
        s = ctx.segs[ctx.idx[sid]]
        if not s.get("translate"):
            rejected.append(f"{sid}: kept-original segment ({s['kind']}), not translatable")
            continue
        tgt = C.normalize_placeholders(tgt)
        if s["kind"] != "table":
            tgt = re.sub(r"\s*\n\s*", " ", tgt).strip()
        old = ctx.trans.get(sid, {}).get("tgt")
        if old == tgt:
            continue
        rev = ctx.trans.get(sid, {}).get("rev", 0) + 1
        ctx.trans[sid] = {"tgt": tgt, "rev": rev, "ts": C.now_iso(), "uncertain": "⟪?⟫" in tgt}
        ctx.change(action="put" if old is None else "revise", seg=sid, old=old, new=tgt, by="agent")
        stored.append(sid)
    dup = [k for k, v in seen.items() if v > 1]
    ctx.st["n_puts"] = ctx.st.get("n_puts", 0) + 1
    ctx.st.setdefault("put_times", []).append(time.time())
    ctx.save_trans()
    res, issues, metrics = refresh(ctx)
    # report on this put only
    errs = []
    for sid in stored:
        for i in issues.get(sid, []):
            if i["severity"] == "error" or a.verbose:
                errs.append(f"{sid} [{i['severity']}] {i['code']}: {i['detail']}")
    warn_n = sum(1 for sid in stored for i in issues.get(sid, []) if i["severity"] == "warn")
    last = ctx.st.get("last_batch_ids") or []
    skipped = [x for x in last if x not in ctx.trans or not ctx.trans[x].get("tgt")]
    print(f"stored {len(stored)} segment(s); coverage {metrics['translated_segments']}/{metrics['translated_segments'] + metrics['untranslated_segments']}; "
          f"errors in this put: {len(errs) if not a.verbose else sum(1 for e in errs if '[error]' in e)}, warnings: {warn_n}; elapsed {ctx.elapsed()}s")
    if rejected:
        print("REJECTED: " + "; ".join(rejected))
    if dup:
        print("DUPLICATE ids in file (last one kept): " + ", ".join(dup))
    if skipped:
        print("MISSING from last batch (still untranslated): " + ", ".join(skipped))
    for e in errs[:25]:
        print("  " + e)
    if any("[error]" in e for e in errs):
        print("fix: re-`put` only the corrected `@@ ID` blocks (revisions are logged).")
    # time / budget report (throughput measured on this run)
    sch = schedule(ctx)
    if sch["rate"]:
        msg = (f"TIME: {sch['elapsed']:.0f}s used of {sch['budget']}s (reserve {sch['reserve']}s); {sch['done_units']} units done at "
               f"{sch['rate'] * 60:.0f} units/min; {sch['left_units']} left ~ {sch['left_units'] / sch['rate']:.0f}s")
        if sch["overrun"]:
            msg += (" -> TIME WARNING: will not finish. The next batch is sized to the time left; remaining titles and headings "
                    "come first; then finalize. Untranslated ids and resume state are written to result.json.")
        print(msg)
    if a.next:
        pick, tot = next_batch(ctx)
        print()
        print_batch(ctx, pick, tot)
    return 0


def cmd_session(a):
    """Start a continuation session on a saved state (e.g. <output>/resume_state): new clock and budget."""
    ctx = Ctx(a.work)
    ctx.st["started"] = time.time()
    ctx.st["started_iso"] = C.now_iso()
    if a.budget:
        ctx.st["budget_s"] = a.budget
    if a.out:
        ctx.st["out"] = os.path.abspath(a.out)
    for k in ("stop_reason", "structure_pass_done", "structure_pass_at", "structure_pass_printed"):
        ctx.st.pop(k, None)
    ctx.st["sessions"] = ctx.st.get("sessions", 1) + 1
    ctx.save_state()
    ctx.change(action="session", session=ctx.st["sessions"], budget_s=ctx.st["budget_s"], by="script")
    todo = [s for s in ctx.segs if s.get("translate") and s.get("in_scope", True) and not ctx.trans.get(s["id"], {}).get("tgt")]
    print(f"session {ctx.st['sessions']}: budget {ctx.st['budget_s']}s; {len(todo)} segments still to translate "
          f"(next: {todo[0]['id'] if todo else '-'}); outputs -> {ctx.out}. Continue with `tr.py batch`.")
    return 0


def cmd_regions(a):
    """List scanned-page regions (no text layer) with their crop image and position."""
    ctx = Ctx(a.work)
    rows = [s for s in ctx.segs if s["kind"] == "image_region" and s.get("in_scope", True)
            and (a.all or s.get("needs_transcription")) and (not a.page or s.get("page") == a.page)]
    if not rows:
        print("no scanned regions awaiting transcription")
        return 0
    print(f"{len(rows)} region(s). Read each image, then write a file of blocks:\n"
          "  @@ S0012\n  <source text exactly as printed; blank line between paragraphs; '# ' for a heading; table rows as 'a | b | c'>\n"
          "  @@ S0013\n  UNREADABLE: <why>      (or NONTEXT: figure/rule/stamp)\n"
          "and run `tr.py transcribe FILE`. Never guess digits you cannot read: write ⟪?⟫ in their place.")
    for s in rows:
        pv = os.path.join(ctx.out, "previews", f"page_{s['page']:03d}.png")
        print(f"{s['id']}  p.{s['page']}  bbox={s['bbox']}  image={os.path.join(ctx.out, s.get('image', ''))}"
              + (f"  page_preview={pv}" if os.path.exists(pv) else "") + f"  [{R.seg_status(s, ctx.trans)}]")
    return 0


RE_LOC_IDS = re.compile(r"^\s*@@\s*(S\d{3,5})(?:\s*(?:\.\.|–|-)\s*(S\d{3,5}))?(?:\s+#([\w.\-]+))?\s*$")
RE_LOC_BOX = re.compile(r"^\s*@@\s*p\.?\s*(\d+)\s*(?:\[\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\])?(?:\s+#([\w.\-]+))?\s*$", re.I)


def parse_transcription(raw):
    """Blocks of `@@ <locator>` + text. Locators:
       `@@ S0012` / `@@ S0101..S0103`      region ids printed by `tr.py regions` (one text for adjacent regions)
       `@@ p6 [x0,y0,x1,y1] #label`        page + box in PDF points (top-left origin): independent of how the
                                           page was cut into regions, so it also works on whole-page regions
       `@@ p6 #label`                      the whole page
    `#label` (optional) names the resulting segment(s) so a translation file can address them as `@@ #label`."""
    blocks, cur, buf = [], None, []
    for line in raw.splitlines():
        m1, m2 = RE_LOC_IDS.match(line), RE_LOC_BOX.match(line)
        if m1 or m2:
            if cur:
                blocks.append((cur, "\n".join(buf).strip()))
            if m1:
                cur = {"ids": [m1.group(1)], "to": m1.group(2), "label": m1.group(3)}
            else:
                box = [float(m2.group(k)) for k in (2, 3, 4, 5)] if m2.group(2) else None
                cur = {"page": int(m2.group(1)), "box": box, "label": m2.group(6)}
            buf = []
        elif cur:
            buf.append(line)
    if cur:
        blocks.append((cur, "\n".join(buf).strip()))
    return blocks


def _center_in(bb, box, pad=3.0):
    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    return box[0] - pad <= cx <= box[2] + pad and box[1] - pad <= cy <= box[3] + pad


def resolve_locator(ctx, loc):
    """-> (parent_region, covered_regions, child_bbox, granularity) or (None, msg)."""
    regs = [s for s in ctx.segs if s["kind"] == "image_region"]
    if "ids" in loc:
        sid = loc["ids"][0]
        if sid not in ctx.idx or ctx.segs[ctx.idx[sid]]["kind"] != "image_region":
            return None, f"{sid}: not a scanned region id"
        members = [ctx.segs[ctx.idx[sid]]]
        if loc.get("to"):
            lo, hi = C.seg_num(sid)[0], C.seg_num(loc["to"])[0]
            members += [s for s in regs if lo < C.seg_num(s["id"])[0] <= hi and "." not in s["id"]]
        bb = [min(m["bbox"][0] for m in members), min(m["bbox"][1] for m in members),
              max(m["bbox"][2] for m in members), max(m["bbox"][3] for m in members)]
        return (members[0], members[1:], bb, members[0].get("granularity", "region")), None
    page_regs = [s for s in regs if s.get("page") == loc["page"] and "." not in s["id"]]
    if not page_regs:
        return None, f"p{loc['page']}: no scanned region on this page (in this run)"
    box = loc["box"]
    if box is None:  # whole page
        bb = [min(m["bbox"][0] for m in page_regs), min(m["bbox"][1] for m in page_regs),
              max(m["bbox"][2] for m in page_regs), max(m["bbox"][3] for m in page_regs)]
        return (page_regs[0], page_regs[1:], bb, "page"), None
    inside = [s for s in page_regs if _center_in(s["bbox"], box)]
    if inside and all(s.get("granularity") != "page" for s in inside):
        return (inside[0], inside[1:], box, "box"), None
    containing = [s for s in page_regs if s["bbox"][0] - 3 <= box[0] and s["bbox"][1] - 3 <= box[1]
                  and s["bbox"][2] + 3 >= box[2] and s["bbox"][3] + 3 >= box[3]]
    if containing:  # coarser region (whole page without numpy, or a big XY-cut block): child gets the box
        return (containing[0], [], box, "box_in_" + containing[0].get("granularity", "region")), None
    overl = [s for s in page_regs if min(s["bbox"][2], box[2]) > max(s["bbox"][0], box[0]) and min(s["bbox"][3], box[3]) > max(s["bbox"][1], box[1])]
    if overl:
        return (overl[0], overl[1:], box, "box"), None
    return None, f"p{loc['page']} {box}: the box does not overlap any scanned region"


def cmd_transcribe(a):
    """Store a transcription (from vision, OCR or a human) as the source text of scanned regions."""
    ctx = Ctx(a.work)
    raw = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8", errors="replace").read()
    blocks = parse_transcription(raw)
    if not blocks:
        print("ERROR: no `@@ S0012` / `@@ p6 [x0,y0,x1,y1]` blocks found")
        return 1
    done, rejected, made = [], [], []
    for loc, text in blocks:
        res, msg = resolve_locator(ctx, loc)
        if res is None:
            rejected.append(msg)
            continue
        parent, covered, bbox, gran = res
        sid = parent["id"]
        partial_parent = gran.startswith("box_in_")
        closing = bool(re.match(r"^\s*(?:DONE|REST\s*:\s*NONTEXT)", text.strip(), re.I))
        if not partial_parent and not closing:
            # re-transcription of the whole region replaces its earlier children
            ctx.segs = [x for x in ctx.segs if not x["id"].startswith(sid + ".")]
            for k_old in [k for k in ctx.trans if k.startswith(sid + ".")]:
                ctx.trans.pop(k_old)
            ctx.idx = {x["id"]: i for i, x in enumerate(ctx.segs)}
            ctx.save_trans()
        rest = re.match(r"^\s*(?:DONE|REST\s*:\s*NONTEXT)\s*:?\s*(.*)$", text.strip(), re.S | re.I)
        if rest:  # close the page / region: everything not transcribed yet carries no text to translate
            targets = [r_ for r_ in ctx.segs if r_["kind"] == "image_region" and "." not in r_["id"] and r_.get("needs_transcription")
                       and (r_ is parent or r_ in covered or ("page" in loc and r_.get("page") == loc["page"]))]
            for r_ in targets:
                has_kids = any(x["id"].startswith(r_["id"] + ".") for x in ctx.segs)
                r_.update(needs_transcription=False, translate=False, transcription="split" if has_kids else "nontext",
                          reason=("transcribed parts kept; rest closed as non-text: " if has_kids else "no text to translate: ") + (rest.group(1).strip() or "closed by transcriber"))
                done.append(r_["id"])
            ctx.change(action="transcribe_close", seg=sid, old=None, new=text.strip(), by=a.by, locator=loc)
            continue
        m = re.match(r"^\s*(UNREADABLE|NONTEXT)\s*:\s*(.*)$", text.strip(), re.S | re.I)
        if m and not partial_parent:
            for r_ in [parent] + covered:
                r_["transcription"] = m.group(1).lower()
                r_["needs_transcription"] = False
                r_["translate"] = False
                r_["reason"] = ("unreadable in the page image: " if m.group(1).upper() == "UNREADABLE" else "no text to translate: ") + m.group(2).strip()
                done.append(r_["id"])
            ctx.change(action="transcribe", seg=sid, old=None, new=text.strip(), by=a.by, locator=loc)
            continue
        if m and partial_parent:
            parent.setdefault("unreadable_boxes", []).append({"bbox": bbox, "note": text.strip()})
            ctx.change(action="transcribe", seg=sid, old=None, new=text.strip(), by=a.by, locator=loc)
            done.append(sid)
            continue
        parts = C.transcription_parts(text)
        if not parts:
            rejected.append(f"{sid}: empty transcription")
            continue
        existing = [C.seg_num(x["id"]) for x in ctx.segs if x["id"].startswith(sid + ".")]
        start = 1 + max((int(x["id"].rsplit(".", 1)[1]) for x in ctx.segs if x["id"].startswith(sid + ".")), default=0)
        kids = []
        for k, (kind, body) in enumerate(parts):
            prot, ph, disp = (body, {}, body) if kind in ("table", "equation", "reference") else C.protect(body)
            label = loc.get("label")
            if label and len(parts) > 1:
                label = f"{label}.{k + 1}"
            kid = {"id": f"{sid}.{start + k}", "kind": kind, "src": disp, "prot": prot, "ph": ph,
                   "translate": kind not in ("equation", "reference"),
                   "reason": None if kind not in ("equation", "reference") else f"{kind}: kept as transcribed",
                   "page": parent["page"], "pages": parent["pages"], "bbox": [round(v, 1) for v in bbox],
                   "boxes": [[parent["page"], [round(v, 1) for v in bbox]]], "column": None,
                   "blocks": [b for r_ in [parent] + covered for b in r_.get("blocks", [])], "section": parent.get("section", ""),
                   "image": parent.get("image"), "label": label,
                   "location_granularity": {"box": "transcription box (PDF points)", "region": "layout region",
                                            "page": "whole page (no finer location)"}.get(gran, gran.replace("box_in_", "box inside a ") + " region"),
                   "flags": ["transcribed"] + (["unclear"] if "⟪?⟫" in body else []) + (["page_level_location"] if gran == "page" else []),
                   "source_origin": a.by, "in_scope": parent.get("in_scope", True)}
            kid["lang"] = C.detect_lang(C.strip_ph(prot))
            kid["sentences"] = C.split_sentences(disp, kid["lang"] if kid["lang"] != "und" else "en")
            kid["units"] = C.size_units(prot, kid["lang"] if kid["lang"] != "und" else "en")
            if kind == "table":
                kid["n_rows"] = len([r for r in body.split("\n") if r.strip()])
            kids.append(kid)
        last = max([ctx.idx[sid]] + [i for i, x in enumerate(ctx.segs) if x["id"].startswith(sid + ".")])
        ctx.segs[last + 1:last + 1] = kids
        ctx.idx = {x["id"]: i for i, x in enumerate(ctx.segs)}
        if partial_parent:
            parent.setdefault("transcribed_boxes", []).append([round(v, 1) for v in bbox])
            parent["reason"] = (f"{len(parent['transcribed_boxes'])} box(es) of this {parent.get('granularity', 'region')} transcribed; "
                                "the rest still needs transcription (or `@@ p{0} ... NONTEXT:` / close it with `@@ {1}` + `NONTEXT: rest`)").format(
                parent["page"], sid)
        else:
            parent.update(transcription="split", needs_transcription=False, translate=False,
                          reason=f"transcribed ({a.by}) into {kids[0]['id']}-{kids[-1]['id']}")
            for o in covered:
                o.update(transcription="covered", needs_transcription=False, translate=False,
                         reason=f"transcribed together with {sid} (one table/paragraph spanning several layout regions)")
                done.append(o["id"])
        made += [(k["id"], k.get("label")) for k in kids]
        ctx.change(action="transcribe", seg=sid, old=None, new=text.strip(), by=a.by, locator=loc)
        done.append(sid)
    with open(ctx.p("segments.jsonl"), "w", encoding="utf-8") as fh:
        for s in ctx.segs:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    ctx.idx = {x["id"]: i for i, x in enumerate(ctx.segs)}
    refresh(ctx)
    left = [s["id"] for s in ctx.segs if s.get("needs_transcription") and s.get("in_scope", True)]
    print(f"transcribed {len(set(done))} region(s) into {len(made)} segment(s); {len(left)} region(s) still need transcription"
          + (f"; REJECTED: {'; '.join(rejected)}" if rejected else ""))
    if made:
        print("new segments: " + ", ".join(f"{i}" + (f" (#{l})" if l else "") for i, l in made))
    print("transcribed text is now ordinary source: `tr.py batch` includes it; checks compare translation with the transcription "
          "(not with the image) — transcription mistakes must be caught by a human (listed in manual_review.md).")
    return 0 if not rejected else 4


def cmd_check(a):
    ctx = Ctx(a.work)
    res, issues, metrics = refresh(ctx)
    print(json.dumps({k: v for k, v in metrics.items()}, ensure_ascii=False))
    rows = [(0 if i["severity"] == "error" else 1, sid, i) for sid, v in issues.items() for i in v]
    rows.sort(key=lambda r: (r[0], r[1]))
    shown = [r for r in rows if r[0] == 0 or a.all]
    for _, sid, i in shown[: a.limit]:
        print(f"{sid} [{i['severity']}] {i['code']}: {i['detail']}")
    if len(shown) > a.limit:
        print(f"... {len(shown) - a.limit} more (see {ctx.out}/manual_review.md)")
    untr = [s["id"] for s in ctx.segs if R.seg_status(s, ctx.trans) == "untranslated"]
    if untr:
        print(f"untranslated in scope: {len(untr)} ({', '.join(untr[:15])}{' ...' if len(untr) > 15 else ''})")
    print(f"status now: {res['status']} — {res['status_reason']}")
    return 0


def cmd_show(a):
    ctx = Ctx(a.work)
    ids = []
    for part in a.ids:
        m = re.match(r"S?(\d+)\s*-\s*S?(\d+)$", part)
        ids += [f"S{k:04d}" for k in range(int(m.group(1)), int(m.group(2)) + 1)] if m else [part]
    _, metrics, _ = None, None, None
    for sid in ids:
        if sid not in ctx.idx:
            print(f"{sid}: unknown")
            continue
        s = ctx.segs[ctx.idx[sid]]
        t = ctx.trans.get(sid, {}).get("tgt")
        print(f"@@ {sid} [{s['kind']} · {R.loc_str(s)} · {R.seg_status(s, ctx.trans)}]")
        print("SRC: " + s["prot"])
        if s.get("ph"):
            print("PH : " + "; ".join(f"{k}={v[:60]}" for k, v in s["ph"].items()))
        print("TGT: " + (t or "-"))
        if t:
            for i in C.check_segment(s, t, ctx.gl, ctx.st["src_lang"], ctx.st["tgt_lang"]):
                print(f"   [{i['severity']}] {i['code']}: {i['detail']}")
    return 0


def cmd_enforce(a):
    """Back-fill locked terms: replace forbidden variants / superseded translations in every segment containing the term."""
    ctx = Ctx(a.work)
    n = 0
    for s in ctx.segs:
        t = ctx.trans.get(s["id"], {}).get("tgt")
        if not t:
            continue
        hits = C.find_terms(s["prot"], ctx.gl)
        new = t
        for e in ctx.gl:
            if e["source"] not in hits or e["keep_original"] or (e["origin"] == "base" and not a.include_base):
                continue
            # a forbidden variant is replaced even when the locked target already occurs elsewhere in the segment
            for v in sorted(e.get("variants", []), key=len, reverse=True):
                if v and v in new and e["target"] not in v:
                    new = new.replace(v, e["target"])
        if new != t:
            ctx.trans[s["id"]].update(tgt=new, rev=ctx.trans[s["id"]].get("rev", 1) + 1, ts=C.now_iso())
            ctx.change(action="enforce_glossary", seg=s["id"], old=t, new=new, by="tr.py enforce")
            n += 1
    ctx.save_trans()
    res, issues, metrics = refresh(ctx)
    left = sum(1 for v in issues.values() for i in v if i["code"] == "term_violation" and i["severity"] == "error")
    print(f"enforce: {n} segment(s) rewritten; locked-term consistency now {metrics['term_consistency_locked']}; "
          f"remaining term errors {left} (need manual rewording: `tr.py issues`, then `put`).")
    return 0


def cmd_status(a):
    ctx = Ctx(a.work)
    todo = [s for s in ctx.segs if s.get("translate") and s.get("in_scope", True)]
    done = [s for s in todo if ctx.trans.get(s["id"], {}).get("tgt")]
    print(f"{len(done)}/{len(todo)} segments, {sum(s['units'] for s in done)}/{sum(s['units'] for s in todo)} units translated; "
          f"elapsed {ctx.elapsed()}s of budget {ctx.st.get('budget_s')}s; batches printed {ctx.st.get('n_batches')}, puts {ctx.st.get('n_puts')}")
    return 0


def cmd_finalize(a):
    ctx = Ctx(a.work)
    extra = dict(ctx.st.get("extra_fields", {}))
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        extra[k.strip()] = v
    for kv in a.set_json or []:
        k, _, v = kv.partition("=")
        v = v.strip()
        extra[k.strip()] = json.load(open(v[1:] if v.startswith("@") else v, encoding="utf-8")) if os.path.exists(v.lstrip("@")) else json.loads(v)
    for u in a.uncertainty or []:
        ctx.st.setdefault("uncertainty", []).append(u)
    ctx.st["extra_fields"] = extra
    res, issues, metrics = refresh(ctx)
    m = metrics
    print(f"FINAL status={res['status']} ({res['status_reason']})")
    print(f"coverage={m['coverage']} term_consistency(locked)={m['term_consistency_locked']} placeholders={m['placeholder_integrity']} "
          f"numbers={m['number_preservation']} crossrefs={m['crossref_preservation']} errors={m['errors']} warnings={m['warnings']}")
    print(f"files: {ctx.out}/result.json, bilingual.md, bilingual.html, translation.{ctx.st['tgt_lang']}.md, alignment.jsonl, glossary.tsv, "
          f"qa_report.json, manual_review.md, changes.jsonl" + (", previews/" if os.path.isdir(os.path.join(ctx.out, 'previews')) else ""))
    if res.get("contract_fields_unfilled"):
        print("CONTRACT FIELDS NOT AUTO-FILLED (set them: tr.py finalize --set NAME=value or --set-json NAME=file.json): "
              + ", ".join(res["contract_fields_unfilled"]))
    return 0


def cmd_audit(a):
    """Document-level audit of an arbitrary translation file against the segmented source (for A/B comparisons)."""
    ctx = Ctx(a.work)
    with open(a.file, encoding="utf-8", errors="replace") as fh:
        tgt = fh.read()
    todo = [s for s in ctx.segs if s.get("translate") and s.get("in_scope", True)]
    if a.only_translated:
        todo = [s for s in todo if ctx.trans.get(s["id"], {}).get("tgt")]
    src_all = "\n".join(C.restore(s["prot"], s["ph"]) for s in todo)
    ns, nt = Counter(C.numbers(src_all)), Counter(C.numbers(tgt))
    num_recall = sum(min(c, nt[v]) for v, c in ns.items()) / max(1, sum(ns.values()))
    cites_s = Counter(re.findall(r"\[\s*\d{1,4}(?:\s*[,–\-—]\s*\d{1,4})*\s*\]", src_all))
    cites_t = Counter(re.sub(r"\s+", "", c) for c in re.findall(r"\[\s*\d{1,4}(?:\s*[,–\-—]\s*\d{1,4})*\s*\]", tgt))
    cite_recall = sum(min(c, cites_t[re.sub(r"\s+", "", k)]) for k, c in cites_s.items()) / max(1, sum(cites_s.values()))
    refs = [(m.group(1), m.group(2)) for m in C.REF_EN.finditer(src_all)] if ctx.st["src_lang"] == "en" else []
    ref_ok = 0
    for w, n in refs:
        kind = next(k for k, rx in C.REF_KIND_EN if re.match(rx, w, re.I))
        words = C.REF_ZH_WORDS[kind]
        if re.search(rf"(?:{words})\s*[\(（]?\s*{re.escape(n)}(?![0-9])|{re.escape(n)}\s*(?:{words})|[\(（]{re.escape(n)}[\)）]", tgt):
            ref_ok += 1
    term_rows, tw, tc = [], 0, 0
    for e in ctx.gl:
        occ = sum(C.find_terms(s["prot"], ctx.gl).get(e["source"], 0) for s in todo)
        if not occ:
            continue
        found = len(re.findall(re.escape(e["target"]), tgt, re.I if e["keep_original"] else 0))
        var = {v: len(re.findall(re.escape(v), tgt)) for v in e.get("variants", []) if v}
        tw += occ
        tc += min(occ, found)
        term_rows.append({"term": e["source"], "locked": e["target"], "source_occurrences": occ, "locked_in_translation": found,
                          "forbidden_variants_found": {k: v for k, v in var.items() if v}})
    paras = [p for p in re.split(r"\n\s*\n", tgt) if C.han_count(p) + len(C.latin_words(p)) > 3]
    out = {"file": os.path.abspath(a.file), "source_segments": len(todo), "translation_paragraphs": len(paras),
           "number_recall": round(num_recall, 4), "numbers_in_source": sum(ns.values()),
           "bracket_citation_recall": round(cite_recall, 4), "citations_in_source": sum(cites_s.values()),
           "crossref_recall": round(ref_ok / len(refs), 4) if refs else None, "crossrefs_in_source": len(refs),
           "locked_term_recall": round(tc / tw, 4) if tw else None, "term_occurrences": tw,
           "chars_translation_han": C.han_count(tgt), "words_source": sum(len(C.latin_words(C.strip_ph(s["prot"]))) for s in todo),
           "terms": term_rows}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if a.save:
        C.write_json(a.save, out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default=default_dir("work"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--case", default="/input/case.json" if os.path.exists("/input/case.json") else None)
    p.add_argument("--source")
    p.add_argument("--out", default=default_dir("out"))
    p.add_argument("--target", choices=["zh", "en"])
    p.add_argument("--source-lang", choices=["zh", "en"])
    p.add_argument("--pages")
    p.add_argument("--sections", help='e.g. "Abstract..Introduction" or "Methods|Results"')
    p.add_argument("--ids", help="e.g. S0010-S0060")
    p.add_argument("--max-units", type=int)
    p.add_argument("--glossary", nargs="*")
    p.add_argument("--password")
    p.add_argument("--ocr", default="auto", choices=["auto", "force", "off"])
    p.add_argument("--budget", type=int, default=900)
    p.add_argument("--reserve", type=int, default=120, help="seconds kept for check/finalize at the end of the budget")
    p.add_argument("--batch-units", type=int, default=1300)
    p.add_argument("--started", help="unix time the run started (for the time budget)")
    p.add_argument("--no-base", action="store_true", help="do not auto-apply resources/glossary_base.tsv")
    p.add_argument("--no-preview", action="store_true")
    p.add_argument("--ignore-case-scope", action="store_true")
    p.add_argument("--keep", action="store_true", help="keep existing translations in --work (re-segmenting the same source)")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("glossary")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_glossary)
    p = sub.add_parser("batch")
    p.add_argument("--units", type=int)
    p.add_argument("--kinds")
    p.add_argument("--ids")
    p.add_argument("--redo", action="store_true")
    p.set_defaults(fn=cmd_batch)
    p = sub.add_parser("put")
    p.add_argument("file")
    p.add_argument("--next", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_put)
    for name in ("check", "issues"):
        p = sub.add_parser(name)
        p.add_argument("--all", action="store_true", help="also list warnings")
        p.add_argument("--limit", type=int, default=40)
        p.set_defaults(fn=cmd_check, all=(name == "issues"))
    p = sub.add_parser("session")
    p.add_argument("--budget", type=int)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_session)
    p = sub.add_parser("regions")
    p.add_argument("--page", type=int)
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_regions)
    p = sub.add_parser("transcribe")
    p.add_argument("file")
    p.add_argument("--by", default="agent_vision", help="who produced the transcription: agent_vision | ocr | human")
    p.set_defaults(fn=cmd_transcribe)
    p = sub.add_parser("show")
    p.add_argument("ids", nargs="+")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("enforce")
    p.add_argument("--include-base", action="store_true", help="also rewrite variants of base-glossary terms")
    p.set_defaults(fn=cmd_enforce)
    p = sub.add_parser("status")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("finalize")
    p.add_argument("--set", action="append")
    p.add_argument("--set-json", action="append")
    p.add_argument("--uncertainty", action="append")
    p.set_defaults(fn=cmd_finalize)
    p = sub.add_parser("audit")
    p.add_argument("file")
    p.add_argument("--only-translated", action="store_true")
    p.add_argument("--save")
    p.set_defaults(fn=cmd_audit)
    a = ap.parse_args(argv)
    if a.cmd != "init" and not os.path.exists(os.path.join(a.work, "state.json")):
        print(f"no state in {a.work}; run `tr.py init` first (or pass --work)")
        return 2
    try:
        return a.fn(a)
    except Exception as ex:  # keep outputs honest even on crashes
        import traceback
        traceback.print_exc()
        print(f"tr.py {a.cmd} crashed: {ex}. result.json (if any) still reflects the last good state; report this in uncertainty.")
        return 3


if __name__ == "__main__":
    sys.exit(main())
