"""literature-translator output writers: bilingual.md / bilingual.html / translation.<lang>.md /
alignment.jsonl / glossary.tsv / qa_report.json / manual_review.md / result.json / page previews."""
import html
import json
import os
import re
from collections import Counter

import lt_core as C

KIND_ZH = {"title": "标题", "heading": "章节标题", "paragraph": "正文段落", "caption": "图表题注", "footnote": "脚注", "table": "表格",
           "equation": "公式", "reference": "参考文献", "front_matter": "作者/单位", "missing_page": "缺失页", "list_item": "列表项",
           "image_region": "扫描页区域"}
KIND_GROUP = {"paragraph": "body", "footnote": "body", "list_item": "body", "title": "headings", "heading": "headings",
              "caption": "captions", "table": "tables"}


def loc_str(s):
    parts = []
    if s.get("pages"):
        pg = s["pages"]
        parts.append("p." + (str(pg[0]) if len(pg) == 1 else f"{pg[0]}–{pg[-1]}"))
    elif s.get("line"):
        parts.append(f"line {s['line']}")
    if s.get("column") not in (None, "span"):
        parts.append(f"col.{int(s['column']) + 1}" if isinstance(s.get("column"), int) else f"col.{s['column']}")
    if s.get("blocks"):
        parts.append("+".join(b for b in s["blocks"] if b))
    if s.get("section"):
        parts.append("§ " + s["section"].split(" > ")[-1][:60])
    return " · ".join(parts)


def seg_status(s, trans):
    if s["kind"] == "missing_page":
        return "missing_source"
    if not s.get("in_scope", True):
        return "out_of_scope"
    if s["kind"] == "image_region":
        tr = s.get("transcription")
        if tr == "unreadable":
            return "unreadable"
        if tr in ("nontext", "split", "covered"):
            return "kept_original"
        if s.get("needs_transcription"):
            return "needs_transcription"
    if not s.get("translate"):
        return "kept_original"
    return "translated" if trans.get(s["id"], {}).get("tgt") else "untranslated"


_BYID = {}


def segs_by_id(segs):
    if _BYID.get("n") != id(segs):
        _BYID.clear()
        _BYID.update({"n": id(segs), "m": {s["id"]: s for s in segs}})
    return _BYID["m"]


def compute(segs, trans, gl, src_lang, tgt_lang):
    """Run checks on every translated segment; return (issues_by_id, metrics, term_stats)."""
    issues = {}
    n_num = n_num_bad = n_ref = n_ref_bad = n_ph_seg = n_ph_bad = 0
    for s in segs:
        t = trans.get(s["id"], {}).get("tgt")
        if not t or not s.get("translate"):
            continue
        iss = C.check_segment(s, t, gl, src_lang, tgt_lang)
        issues[s["id"]] = iss
        n_num += len(C.numbers(s["prot"]))
        n_num_bad += sum(1 for i in iss if i["code"] == "number_missing")
        refs = len(C.REF_EN.findall(C.strip_ph(s["prot"]))) if src_lang == "en" else len(C.REF_ZH.findall(C.strip_ph(s["prot"])))
        n_ref += refs
        n_ref_bad += sum(1 for i in iss if i["code"] == "crossref_mismatch")
        if s["ph"]:
            n_ph_seg += 1
            n_ph_bad += any(i["code"].startswith("placeholder") for i in iss)
    # document-level: the same translation used for two different sources (copy/paste or misalignment)
    seen = {}
    for s in segs:
        t = trans.get(s["id"], {}).get("tgt")
        if not t or not s.get("translate"):
            continue
        key = C.norm_ws(C.strip_ph(t))
        if len(key) >= 15:
            if key in seen and C.norm_ws(C.strip_ph(segs_by_id(segs)[seen[key]]["prot"])) != C.norm_ws(C.strip_ph(s["prot"])):
                issues.setdefault(s["id"], []).append({"code": "duplicate_translation", "severity": "error",
                                                       "detail": f"identical to the translation of {seen[key]} although the sources differ (misaligned or repeated)"})
            else:
                seen.setdefault(key, s["id"])
    st = C.term_stats(segs, trans, gl)
    tw = sum(v["segments_with_term"] for v in st.values() if v["origin"] != "base")
    tc = sum(v["segments_consistent"] for v in st.values() if v["origin"] != "base")
    bw = sum(v["segments_with_term"] for v in st.values() if v["origin"] == "base")
    bc = sum(v["segments_consistent"] for v in st.values() if v["origin"] == "base")
    counts = Counter(seg_status(s, trans) for s in segs)
    in_scope = counts["translated"] + counts["untranslated"]  # text segments only; scanned regions are reported in metrics["scan"]
    parents = [s for s in segs if s["kind"] == "image_region" and s.get("in_scope", True)]
    scan = None
    if parents:
        pages = {}
        for s in parents:
            stt = seg_status(s, trans)
            d = pages.setdefault(s["page"], {"regions": 0, "pending": 0, "unreadable": 0, "boxes_transcribed": 0})
            d["regions"] += 1
            d["pending"] += stt == "needs_transcription"
            d["unreadable"] += stt == "unreadable"
            d["boxes_transcribed"] += len(s.get("transcribed_boxes") or [])
        kids = [s for s in segs if "transcribed" in s.get("flags", []) and s.get("in_scope", True)]
        kids_tr = [s for s in kids if s.get("translate")]
        scan = {"pages_in_scope": len(pages),
                "pages_transcribed": sorted(p for p, d in pages.items() if d["pending"] == 0),
                "pages_partially_transcribed": sorted(p for p, d in pages.items() if d["pending"] and (d["pending"] < d["regions"] or d["boxes_transcribed"])),
                "pages_not_started": sorted(p for p, d in pages.items() if d["pending"] == d["regions"] and not d["boxes_transcribed"]),
                "regions_total": len(parents), "regions_pending": sum(d["pending"] for d in pages.values()),
                "regions_unreadable": sum(d["unreadable"] for d in pages.values()),
                "region_granularity": sorted({s.get("granularity", "region") for s in parents}),
                "transcribed_segments": len(kids), "transcribed_segments_to_translate": len(kids_tr),
                "transcribed_segments_translated": sum(1 for s in kids_tr if trans.get(s["id"], {}).get("tgt")),
                "degraded": sorted({s["degraded"] for s in parents if s.get("degraded")})}
    by_kind = {}
    for s in segs:
        g = KIND_GROUP.get(s["kind"])
        stt = seg_status(s, trans)
        if not g or stt not in ("translated", "untranslated"):
            continue
        d = by_kind.setdefault(g, {"segments_done": 0, "segments_total": 0, "units_done": 0, "units_total": 0})
        d["segments_total"] += 1
        d["units_total"] += s.get("units", 0)
        if stt == "translated":
            d["segments_done"] += 1
            d["units_done"] += s.get("units", 0)
    for d in by_kind.values():
        d["segment_coverage"] = round(d["segments_done"] / d["segments_total"], 4) if d["segments_total"] else None
        d["unit_coverage"] = round(d["units_done"] / d["units_total"], 4) if d["units_total"] else None
    sev = Counter(i["severity"] for v in issues.values() for i in v)
    metrics = {
        "coverage": round(counts["translated"] / in_scope, 4) if in_scope else None,
        "translated_segments": counts["translated"], "untranslated_segments": counts["untranslated"],
        "kept_original_segments": counts["kept_original"], "out_of_scope_segments": counts["out_of_scope"],
        "missing_source_pages": counts["missing_source"],
        "regions_needing_transcription": counts["needs_transcription"], "regions_unreadable": counts["unreadable"],
        "scan": scan,
        "coverage_by_kind": by_kind,
        "equations_needing_confirmation": sum(1 for s in segs if s["kind"] == "equation" and s.get("eq_status") == "needs_confirmation"
                                              and s.get("in_scope", True)),
        "term_consistency_locked": round(tc / tw, 4) if tw else None, "term_occurrences_locked": tw,
        "term_consistency_base": round(bc / bw, 4) if bw else None, "term_occurrences_base": bw,
        "placeholder_integrity": round(1 - n_ph_bad / n_ph_seg, 4) if n_ph_seg else None, "segments_with_placeholders": n_ph_seg,
        "number_preservation": round(1 - n_num_bad / n_num, 4) if n_num else None, "numbers_checked": n_num,
        "crossref_preservation": round(1 - n_ref_bad / n_ref, 4) if n_ref else None, "crossrefs_checked": n_ref,
        "errors": sev["error"], "warnings": sev["warn"],
        "segments_with_errors": sum(1 for v in issues.values() if any(i["severity"] == "error" for i in v)),
    }
    return issues, metrics, st


def _md_escape_block(t):
    return (t or "").replace("\n", "  \n")


def write_bilingual_md(path, st, segs, trans, issues, metrics):
    L = [f"# 双语对照 / Bilingual alignment — {st.get('title') or os.path.basename(st['source'])}", "",
         f"- 源文件 source: `{os.path.basename(st['source'])}` (sha256 `{st.get('sha256', '')[:16]}…`)",
         f"- 方向 direction: {st['src_lang']} → {st['tgt_lang']} · 段落覆盖 coverage: {metrics['translated_segments']}/"
         f"{metrics['translated_segments'] + metrics['untranslated_segments']} · errors {metrics['errors']} · warnings {metrics['warnings']}",
         f"- 每段格式: `[段号] 位置` → 原文 / 译文 / 核验结果；段号与 alignment.jsonl、result.json 中的 id 一致。", ""]
    for s in segs:
        status = seg_status(s, trans)
        t = trans.get(s["id"], {}).get("tgt")
        head = f"### [{s['id']}] {KIND_ZH.get(s['kind'], s['kind'])} · {loc_str(s)}"
        L.append(head)
        if s.get("image"):
            L.append(f"![{s['id']}]({s['image']})" + ("  *(待确认 needs confirmation)*" if s.get("eq_status") == "needs_confirmation" else ""))
        if "transcribed" in s.get("flags", []):
            L.append(f"*原文由页图转录（{s.get('source_origin', 'transcription')}），需对照 `{s.get('image', '')}` 复核*")
        src = C.restore(s.get("prot", ""), s.get("ph", {}))
        if s["kind"] == "table" and s.get("table_body_kept"):
            src += "\n" + "\n".join(s["table_body_kept"])
        L.append("> **原文 / Source**  \n> " + _md_escape_block(src).replace("\n", "\n> "))
        if status == "translated":
            tt = C.restore(t, s.get("ph", {}))
            L.append("")
            L.append("**译文 / Translation**  \n" + _md_escape_block(tt))
            iss = issues.get(s["id"], [])
            if iss:
                L.append("")
                L.append("⚠ 核验: " + "; ".join(f"[{i['severity']}] {i['code']}: {i['detail']}" for i in iss))
            else:
                L.append("")
                L.append("✓ 核验通过（数字/引用/公式占位符/图表编号/术语/否定与限定词）")
        elif status == "kept_original":
            L.append("")
            L.append(f"*保留原文（不译）: {s.get('reason')}*")
        elif status == "out_of_scope":
            L.append("")
            L.append(f"*不在本次翻译范围内: {s.get('scope_reason', '')}*")
        elif status == "missing_source":
            L.append("")
            L.append(f"*【原文缺失】{s.get('reason')}*")
        elif status == "needs_transcription":
            L.append("")
            L.append(f"**【待转录 NEEDS TRANSCRIPTION】** 扫描页无文字层：见 `{s.get('image', '')}`（p.{s.get('page')} bbox {s.get('bbox')}）")
        elif status == "unreadable":
            L.append("")
            L.append(f"**【无法辨认 UNREADABLE】** {s.get('reason')}（`{s.get('image', '')}`）")
        else:
            L.append("")
            L.append("**【未译 UNTRANSLATED】** 本段尚未翻译（时间/预算不足或处理失败），原文保留如上，可人工接管。")
        L.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


HTML_HEAD = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bilingual alignment</title><style>
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6b6b70;--line:#e3e3e6;--warn:#b45309;--err:#b91c1c;--ok:#15803d;--hl:#f6f6f8}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececef;--mut:#9a9aa2;--line:#2c2c31;--hl:#1d1d21}}
body{background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;margin:0;padding:16px}
h1{font-size:20px;margin:0 0 6px}.meta{color:var(--mut);font-size:13px;margin-bottom:12px}
table{border-collapse:collapse;width:100%;table-layout:fixed}td,th{border-top:1px solid var(--line);padding:8px;vertical-align:top;word-wrap:break-word}
th{position:sticky;top:0;background:var(--bg);text-align:left;font-size:13px;color:var(--mut)}
td.id{width:13%;font-size:12px;color:var(--mut)}td.id b{color:var(--fg);font-size:13px}tr.heading td,tr.title td{background:var(--hl);font-weight:600}
.math{font-family:"Cambria Math","STIX Two Math",serif;background:rgba(127,127,127,.12);padding:0 2px;border-radius:3px}.cite{color:#2563eb}
.iss{font-size:12px;margin-top:6px}.error{color:var(--err)}.warn{color:var(--warn)}.ok{color:var(--ok);font-size:12px}
.kept{color:var(--mut);font-style:italic}.untr{color:var(--err);font-weight:600}pre{white-space:pre-wrap;margin:0;font:inherit}
@media (max-width:700px){td.id{width:22%}body{padding:10px}}
</style></head><body>"""


def write_bilingual_html(path, st, segs, trans, issues, metrics):
    rows = []
    for s in segs:
        status = seg_status(s, trans)
        t = trans.get(s["id"], {}).get("tgt")
        src = C.restore(s.get("prot", ""), s.get("ph", {}), fmt="html")
        if s["kind"] == "table" and s.get("table_body_kept"):
            src += "\n" + html.escape("\n".join(s["table_body_kept"]))
        if s["kind"] == "table":
            src = f"<pre>{src}</pre>"
        if s.get("image"):
            src += f"<div><img src='{html.escape(s['image'])}' style='max-width:100%;border:1px solid var(--line)' alt='{s['id']}'></div>"
            if s.get("eq_status") == "needs_confirmation":
                src += "<div class='warn'>待确认：文字层无法可靠还原，请以图片为准</div>"
        if status == "translated":
            tt = C.restore(t, s.get("ph", {}), fmt="html")
            if s["kind"] == "table":
                tt = f"<pre>{tt}</pre>"
            iss = issues.get(s["id"], [])
            tt += ("<div class='iss'>" + "<br>".join(f"<span class='{i['severity']}'>[{i['severity']}] {html.escape(i['code'])}: "
                                                    f"{html.escape(i['detail'])}</span>" for i in iss) + "</div>") if iss else "<div class='ok'>✓ checks passed</div>"
        elif status == "kept_original":
            tt = f"<span class='kept'>保留原文：{html.escape(s.get('reason') or '')}</span>"
        elif status == "out_of_scope":
            tt = "<span class='kept'>不在本次翻译范围</span>"
        elif status == "missing_source":
            tt = f"<span class='untr'>原文缺失：{html.escape(s.get('reason') or '')}</span>"
        elif status == "needs_transcription":
            tt = "<span class='untr'>【待转录】扫描页区域，见左侧图片</span>"
        elif status == "unreadable":
            tt = f"<span class='untr'>【无法辨认】{html.escape(s.get('reason') or '')}</span>"
        else:
            tt = "<span class='untr'>【未译】</span>"
        rows.append(f"<tr id='{s['id']}' class='{s['kind']}'><td class='id'><b>{s['id']}</b><br>{html.escape(KIND_ZH.get(s['kind'], s['kind']))}"
                    f"<br>{html.escape(loc_str(s))}</td><td>{src}</td><td>{tt}</td></tr>")
    body = (f"<h1>{html.escape(st.get('title') or os.path.basename(st['source']))}</h1><div class='meta'>{html.escape(os.path.basename(st['source']))} · "
            f"{st['src_lang']} → {st['tgt_lang']} · coverage {metrics['translated_segments']}/{metrics['translated_segments'] + metrics['untranslated_segments']}"
            f" · term consistency {metrics['term_consistency_locked']} · errors {metrics['errors']} · warnings {metrics['warnings']}</div>"
            "<table><thead><tr><th>段号 / 位置</th><th>原文 Source</th><th>译文 Translation</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></body></html>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(HTML_HEAD + body)


def write_target_md(path, st, segs, trans):
    L = [f"<!-- translation generated by literature-translator {C.VERSION}; segment ids in comments map back to the source -->"]
    for s in segs:
        status = seg_status(s, trans)
        t = trans.get(s["id"], {}).get("tgt")
        text = C.restore(t, s["ph"]) if status == "translated" else C.restore(s.get("prot", ""), s.get("ph", {}))
        tag = f"<!-- {s['id']} {loc_str(s)} [{status}] -->"
        if status == "out_of_scope":
            continue
        if status == "untranslated":
            text = "【未译】" + text
        if s["kind"] == "title":
            L.append(f"{tag}\n# {text}")
        elif s["kind"] == "heading":
            L.append(f"{tag}\n## {text}")
        elif s["kind"] == "caption":
            L.append(f"{tag}\n> {text}")
        elif s["kind"] == "equation":
            L.append(f"{tag}\n$$ {text} $$")
        elif s["kind"] == "table":
            rows = [r for r in text.split("\n") if r.strip()] + (s.get("table_body_kept") or [])
            if rows:
                n = rows[0].count("|") + 1
                md = ["| " + rows[0] + " |", "|" + "---|" * n] + ["| " + r + " |" for r in rows[1:]]
                L.append(tag + "\n" + "\n".join(md))
        elif s["kind"] == "reference":
            L.append(f"{tag}\n- {text}")
        else:
            L.append(f"{tag}\n{text}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n\n".join(L) + "\n")


def write_manual_review(path, segs, trans, issues, dropped, st):
    L = ["# 人工确认清单 / Manual review checklist", "",
         "按优先级排列。每项给出段号（可在 bilingual.html#段号 定位）与原因。自动检查只能发现可机检的问题，不能替代人工审读。", ""]
    order = {"error": 0, "warn": 1}
    items = []
    for s in segs:
        if not s.get("in_scope", True):
            continue
        for i in issues.get(s["id"], []):
            items.append((order.get(i["severity"], 2), s["id"], f"[{i['severity']}] {i['code']}: {i['detail']}"))
        stt = seg_status(s, trans)
        if stt == "needs_transcription":
            items.append((0, s["id"], f"[error] needs_transcription: p.{s.get('page')} region {s.get('bbox')} has no text layer; read {s.get('image')} and run tr.py transcribe"))
        if stt == "unreadable":
            items.append((0, s["id"], f"[error] unreadable_region: p.{s.get('page')} region {s.get('bbox')}: {s.get('reason')} ({s.get('image')})"))
        if "transcribed" in s.get("flags", []) and s.get("translate"):
            items.append((1, s["id"], f"[warn] transcribed_source: source text was transcribed from {s.get('image')} ({s.get('source_origin', 'transcription')}); verify numbers and symbols against the image"))
        if "formula_fragment" in s.get("flags", []):
            items.append((1, s["id"], f"[warn] formula_fragment: short math-only text outside any equation (p.{s.get('page')} {s.get('bbox')}): "
                                      "probably a piece of a display formula the parser could not attach; check the page"))
        if "unmapped_glyph" in s.get("flags", []):
            items.append((1, s["id"], "[warn] unmapped_glyph: the text layer has glyphs without Unicode mapping (shown as ⟨?⟩); check the page"))
        if seg_status(s, trans) == "untranslated":
            items.append((0, s["id"], "[error] untranslated: segment in scope but not translated"))
        if "ocr" in s.get("flags", []) and s.get("translate"):
            items.append((1, s["id"], "[warn] ocr_source: source text comes from OCR; verify numbers and terms against the page image"))
        if s["kind"] == "equation" and s.get("eq_status") == "needs_confirmation":
            items.append((1, s["id"], f"[warn] equation_needs_confirmation: ({s.get('eq_number') or '-'}) p.{s.get('page')} bbox {s.get('bbox')} — "
                                      f"{'; '.join(s.get('eq_notes') or [])}; compare with {s.get('image')}"))
        elif s["kind"] == "equation" and "transcribed" in s.get("flags", []):
            items.append((1, s["id"], f"[warn] equation_transcribed: equation transcribed from {s.get('image')}; verify every symbol against the image"))
        elif s["kind"] == "equation":
            items.append((2, s["id"], f"[info] equation_rebuilt: ({s.get('eq_number') or '-'}) rebuilt from the text layer; spot-check against {s.get('image')}"))
        if "unclear" in s.get("flags", []) or (trans.get(s["id"], {}).get("uncertain")):
            items.append((1, s["id"], "[warn] marked_unclear: ⟪?⟫ in the transcription or translation; a human must resolve it"))
        if s["kind"] == "missing_page":
            items.append((0, s["id"], f"[error] missing_source: {s.get('reason')}"))
    items.sort()
    L.append(f"## 需要处理的问题 ({len(items)})")
    for _, sid, msg in items[:400]:
        L.append(f"- [ ] **{sid}** {msg}")
    if len(items) > 400:
        L.append(f"- … {len(items) - 400} more in qa_report.json")
    L.append("")
    L.append(f"## 未进入正文的版面元素 ({len(dropped)}) — 确认没有漏掉正文")
    for d in dropped[:120]:
        L.append(f"- p.{d.get('page')} `{d.get('block')}` {d.get('role')}: {d.get('reason')} — “{d.get('text', '')[:80]}”")
    if len(dropped) > 120:
        L.append(f"- … {len(dropped) - 120} more in dropped.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    return items


CONTRACT_HINTS = [
    (r"note|备注|说明", "verification_notes"),
    (r"bilingual|aligned|alignment|pairs|对照|parallel", "pairs"),
    (r"glossary|term|术语|vocab", "glossary"),
    (r"manual|review|human|人工|待确认", "manual_review"),
    (r"uncertain|不确定|limitation|局限", "uncertainty"),
    (r"untranslated|未译|missing", "untranslated"),
    (r"qa|check|verif|issue|核验|检查|quality", "qa"),
    (r"metric|score|指标", "metrics"),
    (r"coverage|complete|完整", "coverage"),
    (r"change|revision|修改|变更", "changes"),
    (r"evidence|source|provenance|location|来源|证据|定位|citation", "sources"),
    (r"status|状态", "status"),
    (r"segment|paragraph|段", "segments"),
    (r"translation|translated|译文|target", "translation_text"),
    (r"case", "case_id"),
]


def _want_type(spec):
    if isinstance(spec, dict) and isinstance(spec.get("type"), str):
        spec = spec["type"]
    s = json.dumps(spec, ensure_ascii=False).lower() if not isinstance(spec, str) else spec.lower()
    if isinstance(spec, list) or re.search(r"\blist\b|array|\[\]|列表|数组", s):
        return "list"
    if isinstance(spec, dict) or re.search(r"\bobject\b|\bdict\b|\bmap\b|对象|字典", s):
        return "object"
    if re.search(r"\bstring\b|\bstr\b|\btext\b|字符串|文本", s):
        return "string"
    if re.search(r"number|float|\bint|integer|数值|数字", s):
        return "number"
    if re.search(r"bool", s):
        return "boolean"
    return None


def contract_schema(contract):
    """Normalize flat contracts, JSON Schema objects (properties/required), and deliverables dicts into {field: spec}."""
    if not isinstance(contract, dict):
        return contract if isinstance(contract, list) else {}
    src = contract.get("deliverables") if isinstance(contract.get("deliverables"), dict) and len(contract) == 1 else contract
    if "properties" in src or ("required" in src and isinstance(src.get("required"), list)):
        fields = {}
        props = src.get("properties")
        if isinstance(props, dict):
            fields.update(props)
        req = src.get("required")
        if isinstance(req, list):
            for name in req:
                if isinstance(name, str) and name not in fields:
                    fields[name] = None
        if isinstance(src.get("deliverables"), dict):
            for name, spec in src["deliverables"].items():
                if isinstance(name, str) and name not in fields:
                    fields[name] = spec
        return fields
    return src


def coerce(value, want):
    """Make a mapped value match the contract type; returns (value, changed)."""
    if want is None or value is None:
        return value, False
    if want == "list":
        if isinstance(value, list):
            return value, False
        if isinstance(value, dict):
            lists = [x for v in value.values() if isinstance(v, list) for x in v]
            return (lists if lists else [{"field": k, "value": v} for k, v in value.items()]), True
        return [value], True
    if want == "object":
        if isinstance(value, dict):
            return value, False
        return ({"items": value} if isinstance(value, list) else {"value": value}), True
    if want == "string":
        if isinstance(value, str):
            return value, False
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            return "\n".join(value), True
        return json.dumps(value, ensure_ascii=False), True
    if want == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value, False
        if isinstance(value, str) and re.fullmatch(r"\s*[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\s*%?\s*", value):
            v = float(value.strip().rstrip("%"))
            return (v / 100 if value.strip().endswith("%") else v), True
        return value, False  # e.g. "unavailable": cannot become a number
    if want == "boolean":
        if isinstance(value, bool):
            return value, False
        if isinstance(value, str) and value.strip().lower() in ("true", "yes", "y", "1", "是", "真"):
            return True, True
        if isinstance(value, str) and value.strip().lower() in ("false", "no", "n", "0", "否", "假"):
            return False, True
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value), True
        return value, False
    return value, False


def fill_contract(contract, core):
    """Map output_contract field names to core result fields. Returns (filled, unfilled_names)."""
    filled, unfilled = {}, []
    schema = contract_schema(contract)
    names = list(schema.keys()) if isinstance(schema, dict) else (schema if isinstance(schema, list) else [])
    for name in names:
        if not isinstance(name, str):
            continue
        if name in core:
            filled[name] = core[name]
            continue
        low = name.lower()
        hit = next((k for rx, k in CONTRACT_HINTS if re.search(rx, low)), None)
        if hit:
            filled[name] = core.get(hit)
        else:
            unfilled.append(name)
    return filled, unfilled


def apply_contract_types(contract, res):
    """Coerce every contract field to its declared type. Returns (coercions, gaps): a value that cannot be converted
    (e.g. confidence_level: number given "unavailable") is replaced by null and reported as a gap with the reason."""
    done, gaps = [], []
    schema = contract_schema(contract)
    if not isinstance(schema, dict):
        return done, gaps
    for name, spec in schema.items():
        want = _want_type(spec)
        if name not in res or res[name] is None:
            gaps.append({"field": name, "expected": want or spec, "got": None,
                         "reason": "no value: the Skill could not map this field and the agent did not set it (tr.py finalize --set)"})
            res.setdefault(name, None)
            continue
        val, changed = coerce(res[name], want)
        ok = want is None or (want == "list" and isinstance(val, list)) or (want == "object" and isinstance(val, dict)) or \
            (want == "string" and isinstance(val, str)) or (want == "number" and isinstance(val, (int, float)) and not isinstance(val, bool)) or \
            (want == "boolean" and isinstance(val, bool))
        if not ok:
            gaps.append({"field": name, "expected": want, "got": res[name] if isinstance(res[name], (str, int, float, bool)) else type(res[name]).__name__,
                         "reason": f"value of type {type(res[name]).__name__} cannot be converted to {want}; field set to null"})
            res[name] = None
            continue
        if changed:
            done.append({"field": name, "declared": spec, "from": type(res[name]).__name__, "to": type(val).__name__})
            res[name] = val
    return done, gaps


def build_result(st, segs, trans, gl, issues, metrics, tstats, dropped, changes, review_items, extra_fields):
    in_scope = metrics["translated_segments"] + metrics["untranslated_segments"] + metrics["regions_needing_transcription"] + \
        metrics["regions_unreadable"]  # for the status decision only; metrics["coverage"] counts text segments
    pending = metrics["regions_needing_transcription"] + metrics["regions_unreadable"]
    if st.get("fatal"):
        status, reason = "failed", st["fatal"]
    elif metrics["translated_segments"] == 0:
        if in_scope == 0 and metrics["kept_original_segments"] > 0 and not metrics["missing_source_pages"] and pending == 0:
            status = "success"
            reason = f"all {metrics['kept_original_segments']} in-scope segment(s) intentionally kept original (no translatable prose)"
        else:
            status = "failed" if in_scope == 0 else "partial"
            reason = ("no translatable text was found" + (f" ({metrics['missing_source_pages']} page(s) have no usable text layer and OCR was unavailable)" if metrics["missing_source_pages"] else "")) if in_scope == 0 else \
                ("no segment translated yet" + (f"; {metrics['regions_needing_transcription']} scanned region(s) await transcription (no text layer, no OCR)"
                                                if metrics["regions_needing_transcription"] else ""))
    elif metrics["untranslated_segments"] or metrics["missing_source_pages"] or pending:
        status = "partial"
        reason = f"{metrics['untranslated_segments']} of {in_scope} in-scope segments untranslated" + \
                 (f"; {metrics['missing_source_pages']} page(s) without usable text" if metrics["missing_source_pages"] else "") + \
                 (f"; {metrics['regions_needing_transcription']} scanned region(s) not transcribed" if metrics["regions_needing_transcription"] else "") + \
                 (f"; {metrics['regions_unreadable']} scanned region(s) unreadable" if metrics["regions_unreadable"] else "") +                  (f"; scanned pages fully transcribed {len(metrics['scan']['pages_transcribed'])}/{metrics['scan']['pages_in_scope']}" if metrics.get("scan") else "")
    elif metrics["segments_with_errors"]:
        status, reason = "partial", f"all segments translated but {metrics['segments_with_errors']} segment(s) still fail hard checks (see manual_review)"
    else:
        status, reason = "success", "all in-scope segments translated; no hard-check errors (warnings may remain)"
    seg_out, pairs, untr = [], [], []
    for s in segs:
        stt = seg_status(s, trans)
        t = trans.get(s["id"], {}).get("tgt")
        boxes = [{"block": b, "page": bp, "bbox": bb} for b, (bp, bb) in zip(s.get("blocks") or [], s.get("boxes") or [])]
        pg = s.get("pages") or []
        rec = {"id": s["id"], "kind": s["kind"], "location": {"pages": s.get("pages"), "bbox": s.get("bbox"), "column": s.get("column"),
                                                            "blocks": s.get("blocks"), "boxes": boxes or None, "section": s.get("section"),
                                                            "line": s.get("line"), "joined_across_blocks": bool(s.get("joined")),
                                                            "page_breaks": [f"p{a}->p{b}" for a, b in zip(pg, pg[1:])] or None},
               "image": s.get("image"), "eq_number": s.get("eq_number"), "eq_status": s.get("eq_status"),
               "source_origin": s.get("source_origin", "text_layer" if s["kind"] != "image_region" else None),
               "status": stt, "source": C.restore(s.get("prot", ""), s.get("ph", {})),
               "translation": C.restore(t, s["ph"]) if stt == "translated" else None,
               "protected_tokens": s.get("ph") or None, "reason": s.get("reason") if stt != "translated" else None,
               "issues": issues.get(s["id"], []) or None, "revisions": trans.get(s["id"], {}).get("rev")}
        seg_out.append(rec)
        if stt == "translated":
            pairs.append({"id": s["id"], "page": (s.get("pages") or [None])[0], "kind": s["kind"], "source": rec["source"], "translation": rec["translation"]})
        if stt == "untranslated":
            untr.append({"id": s["id"], "location": loc_str(s), "source": rec["source"][:300]})
    gl_out = [{k: v[k] for k in ("source", "target", "origin", "keep_original", "variants", "segments_with_term", "segments_consistent", "violations", "note")}
              for v in tstats.values() if v["segments_with_term"] or v["origin"] != "base"]
    unc = list(st.get("uncertainty", []))
    if any("ocr" in s.get("flags", []) for s in segs):
        unc.append("some source text comes from OCR; numbers/terms on those pages need checking against page images")
    if any(s["kind"] == "equation" for s in segs):
        n_eq = sum(1 for s in segs if s["kind"] == "equation" and s.get("in_scope", True))
        unc.append(f"{metrics['equations_needing_confirmation']} of {n_eq} display equations could not be reliably rebuilt from the text layer "
                   "(eq_status=needs_confirmation, crop image in crops/); the others were rebuilt from spans and fraction bars and should still be spot-checked")
    if metrics.get("scan") and metrics["scan"].get("degraded"):
        unc.append("DEGRADED scanned-page layout: " + metrics["scan"]["degraded"][0] + "; transcribed parts keep the boxes given in "
                   "`@@ pN [x0,y0,x1,y1]` locators, other parts are located only to the page")
    if any("transcribed" in s.get("flags", []) for s in segs):
        unc.append("some source text was transcribed from page images (no text layer); transcription errors cannot be detected by the checks")
    unc.append("automatic checks verify numbers, units, placeholders (formulas/citations/URLs), cross-references, glossary terms, negation and qualifier cues; "
               "they do not prove semantic adequacy — warnings are listed in manual_review.md")
    core = {
        "case_id": st.get("case_id"), "status": status, "status_reason": reason,
        "skill": {"name": "literature-translator", "version": C.VERSION},
        "source": {"file": st["source"], "sha256": st.get("sha256"), "format": st.get("format"), "pages": st.get("page_count"),
                   "title": st.get("title")},
        "direction": {"source_lang": st["src_lang"], "target_lang": st["tgt_lang"]},
        "scope": st.get("scope"),
        "outputs": {k: v for k, v in st.get("outputs", {}).items()},
        "metrics": metrics,
        "coverage": metrics["coverage"],
        "segments": seg_out,
        "pairs": pairs,
        "translation_text": "\n\n".join(p["translation"] for p in pairs),
        "untranslated": untr,
        "glossary": gl_out,
        "qa": {"metrics": metrics, "issues": [{"id": k, **i} for k, v in issues.items() for i in v]},
        "manual_review": [{"id": sid, "item": msg} for _, sid, msg in review_items[:300]],
        "uncertainty": unc,
        "verification_notes": [f"{k} [{i['severity']}] {i['code']}: {i['detail']}" for k, v in issues.items() for i in v] +
                              [f"{sid} {msg}" for _, sid, msg in review_items if "[error]" not in msg and "[warn]" not in msg][:100] +
                              ([f"{metrics['regions_needing_transcription']} scanned region(s) not transcribed"] if metrics["regions_needing_transcription"] else []),
        "resume": {"resumable": bool(untr) or bool(metrics["regions_needing_transcription"]), "untranslated_count": len(untr),
                   "next_ids": [u["id"] for u in untr[:30]], "state_dir": "resume_state/",
                   "command": "python tr.py --work <output>/resume_state session --budget 900 && python tr.py --work <output>/resume_state batch"},
        "changes": changes[-300:],
        "dropped_non_body": {"count": len(dropped), "file": "dropped.jsonl",
                             "by_role": dict(Counter(d.get("role") for d in dropped))},
        "sources": [{"file": st["source"], "sha256": st.get("sha256"), "evidence_rule": "every segment carries page/bbox/block ids (PDF) or line numbers (text)"}],
        "run": {"started_at": st.get("started_iso"), "updated_at": C.now_iso(), "elapsed_s": st.get("elapsed_s"),
                "batches_put": st.get("n_puts", 0), "parser": st.get("parser")},
    }
    filled, unfilled = fill_contract(st.get("contract") or {}, core)
    res = dict(core)
    for k, v in filled.items():
        res[k] = v
    for k, v in (extra_fields or {}).items():
        res[k] = v
        if k in unfilled:
            unfilled.remove(k)
    res["contract_fields_unfilled"] = unfilled
    res["contract_type_coercions"], res["contract_gaps"] = apply_contract_types(st.get("contract") or {}, res)
    for g in res["contract_gaps"]:
        if g["field"] not in res["contract_fields_unfilled"]:
            res["contract_fields_unfilled"].append(g["field"])
    if res["contract_gaps"]:
        res["status_reason"] += f"; output_contract not fully satisfied: {', '.join(g['field'] for g in res['contract_gaps'])} (see contract_gaps)"
    return res


def render_crops(pdf_path, segs, out_dir, password=None, kinds=("equation", "table", "image_region"), dpi=170):
    """Crop image per segment (equations, tables, scanned regions): crops/Sxxxx.png; sets seg['image']."""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    os.makedirs(os.path.join(out_dir, "crops"), exist_ok=True)
    doc = fitz.open(pdf_path)
    if doc.needs_pass:
        doc.authenticate(password or "")
    n = 0
    for s in segs:
        if s["kind"] not in kinds or not s.get("in_scope", True) or not s.get("bbox") or not s.get("page"):
            continue
        page = doc[s["page"] - 1]
        r = (fitz.Rect(s["bbox"]) + (-4, -4, 4, 4)) & page.rect
        if r.is_empty or r.width < 2 or r.height < 2:
            continue
        rel = f"crops/{s['id']}.png"
        page.get_pixmap(dpi=dpi if s["kind"] != "image_region" else 200, clip=r).save(os.path.join(out_dir, rel))
        s["image"] = rel
        n += 1
    doc.close()
    return n


def render_previews(pdf_path, segs, out_dir, pages, password=None, dpi=70, max_pages=40):
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    if doc.needs_pass:
        doc.authenticate(password or "")
    col = {"paragraph": (0, 0.3, 0.9), "heading": (0.9, 0.4, 0), "title": (0.8, 0, 0), "caption": (0.6, 0, 0.6), "table": (0.5, 0.3, 0),
           "footnote": (0.3, 0.3, 0.3), "equation": (0, 0.6, 0), "reference": (0.5, 0.5, 0.5), "front_matter": (0.5, 0.5, 0.5)}
    written = []
    for p in pages[:max_pages]:
        if p < 1 or p > doc.page_count:
            continue
        page = doc[p - 1]
        for s in segs:
            for bp, bb in s.get("boxes") or ([[s["page"], s["bbox"]]] if s.get("bbox") else []):
                if bp != p or not bb:
                    continue
                c = col.get(s["kind"], (0, 0, 0)) if s.get("translate") else (0.6, 0.6, 0.6)
                r = fitz.Rect(bb)
                page.draw_rect(r, color=c, width=0.7)
                page.insert_text((r.x0 + 1, r.y0 + 6), s["id"], fontsize=6, color=c)
        fn = os.path.join(out_dir, f"page_{p:03d}.png")
        page.get_pixmap(dpi=dpi).save(fn)
        written.append(fn)
    doc.close()
    return written
