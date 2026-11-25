"""literature-translator core: segmentation, placeholder protection, glossary locking, checks.

Pure Python except the PDF path, which uses the vendored layout parser (pdf_layout.py, PyMuPDF).
All functions are deterministic: the same input gives the same segment ids on every run.
"""
import hashlib
import html
import json
import os
import re
import time
import zipfile
from collections import Counter, defaultdict

VERSION = "0.3.0"
TAG_OPEN, TAG_CLOSE = "\ue000", "\ue001"
PH_RE = re.compile(r"\{\{([MCU]\d+)\}\}")
PH_LOOSE_RE = re.compile(r"[\{｛]\s*[\{｛]\s*([MCUmcu]\s*\d+)\s*[\}｝]\s*[\}｝]")
HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.normpath(os.path.join(HERE, "..", "resources"))

TRANSLATE_KINDS = {"title", "heading", "paragraph", "caption", "footnote", "table", "list_item"}


# ---------------------------------------------------------------- small helpers
def norm_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def han_count(s):
    return sum(1 for c in s or "" if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf")


def latin_words(s):
    return re.findall(r"[A-Za-z][A-Za-z'’\-]*", s or "")


def detect_lang(text):
    h, w = han_count(text), len(latin_words(text))
    if h == 0 and w == 0:
        return "und"
    return "zh" if h >= w else "en"


def strip_ph(s):
    return PH_RE.sub(" ", s or "")


def size_units(text, lang):
    """Translation workload unit: English words or Chinese characters / 1.6."""
    t = strip_ph(text)
    return len(latin_words(t)) if lang == "en" else int(han_count(t) / 1.6 + len(latin_words(t)))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path, obj, indent=1):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- protection
OPS_GAP = re.compile(r"^[ \t0-9=+\-−–()\[\]{}|/<>!'′*·×,:^_~≈≤≥]{0,8}$")
ADJ_RIGHT = re.compile(r"^(?:\^\{[^{}]*\}|_\{[^{}]*\}|[0-9()\[\]'′*+\-−=/|^_.,])+")
ADJ_LEFT = re.compile(r"(?:[0-9(\[{|−\-]|\^\{[^{}]*\}|_\{[^{}]*\})+$")
RE_SUP_CITE = re.compile(r"(?<=[A-Za-z\u4e00-\u9fff\.\,;:\)\]”’\"%])\^\{\s*((?:\d{1,4}(?:\s*[,–\-—]\s*\d{1,4})*)|[\*∗†‡§¶#]+)\s*\}")
RE_SCRIPT_TOKEN = re.compile(r"[^\s{}]*?(?:(?:\^|_)\{(?:[^{}]|\{[^{}]*\})*\}[^\s{}]*?)+(?=[\s,.;:)\]]|$)")
_KEY = r"[A-Z][A-Za-z\-+]*(?:\^\{\+\})?\d{2,4}[a-z]?"
RE_BRACKET_CITE = re.compile(r"\[\s*\d{1,4}(?:\s*[,–\-—]\s*\d{1,4})*\s*\]|\[" + _KEY + r"(?:\s*[,;]\s*" + _KEY + r")*\]")
RE_AUTHOR_YEAR = re.compile(
    r"\((?:e\.g\.,?\s*|see\s+(?:also\s+)?|cf\.\s*)?"
    r"(?:[A-Z][A-Za-z'’\-]+(?:\s+(?:et\s+al\.?|and|&)\s*(?:[A-Z][A-Za-z'’\-]+)?)?,?\s*(?:19|20)\d{2}[a-z]?(?:\s*[;,]\s*)?)+\)")
RE_URL = re.compile(r"https?://[^\s<>\"]+[^\s<>\".,;:)\]]|www\.[^\s<>\"]+[^\s<>\".,;:)\]]|\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b|\b10\.\d{4,9}/[^\s\"<>]+[^\s\"<>.,;)\]]")


TAG_WOPEN, TAG_WCLOSE = "", ""
PIECE_RE = re.compile("(.*?|.*?)", re.S)
CLOSE_OF = {"(": ")", "[": "]", "{": "}"}


def _math_pieces(s):
    """Split a tagged string into pieces typed S (math font), w (short weak-math span), g (operator gap), T (text)."""
    out, pos = [], 0
    for m in PIECE_RE.finditer(s):
        if m.start() > pos:
            out.append(["P", s[pos:m.start()]])
        out.append(["S" if m.group(0)[0] == TAG_OPEN else "W", m.group(0)[1:-1]])
        pos = m.end()
    if pos < len(s):
        out.append(["P", s[pos:]])
    for p in out:
        core = p[1].strip()
        if p[0] == "W":
            p[0] = "w" if core and len(core) <= 8 and not re.search(r"[A-Za-z]{2,}", core) and " " not in core else "P"
        if p[0] == "P":
            p[0] = "g" if OPS_GAP.match(p[1]) and not re.search(r"[A-Za-z]", p[1]) else "T"
    return out


def _protect_math(s, new):
    pieces = _math_pieces(s)
    out, i, n = [], 0, len(pieces)
    while i < n:
        if pieces[i][0] != "S":
            out.append(pieces[i])
            i += 1
            continue
        a = i
        while a - 1 >= 0 and pieces[a - 1][0] == "w" and (not out or out[-1] is pieces[a - 1]):
            a -= 1
            out.pop()
        b = i
        while b + 1 < n and pieces[b + 1][0] in "Swg":
            b += 1
        while pieces[b][0] == "g":
            b -= 1
        out.append(["M", "".join(p[1] for p in pieces[a:b + 1])])
        i = b + 1
    # balance brackets against neighbouring plain text, absorb glued sub/superscripts, strip end punctuation
    res = []
    for k, (typ, txt) in enumerate(out):
        if typ != "M":
            res.append(txt)
            continue
        body = txt
        nxt = out[k + 1] if k + 1 < len(out) else None
        for o, c in CLOSE_OF.items():
            while body.count(o) > body.count(c) and nxt and nxt[0] != "M":
                m = re.match(r"^[^A-Za-z]{0,24}?" + re.escape(c), nxt[1])
                if not m:
                    break
                body += m.group(0)
                nxt[1] = nxt[1][m.end():]
            if body.count(c) > body.count(o) and res and res[-1].rstrip().endswith(o):
                prev = res[-1]
                cut = prev.rstrip()
                res[-1] = cut[:-1]
                body = o + prev[len(cut):] + body
        if nxt and nxt[0] != "M":
            m = ADJ_SCRIPT.match(nxt[1])
            if m:
                body += m.group(0)
                nxt[1] = nxt[1][m.end():]
        tail = re.search(r"[\s.,;:]*$", body).group(0)
        # an unmatched closing bracket at the end belongs to the sentence, not the formula
        core = body[: len(body) - len(tail)]
        while core and core[-1] in ")]}" and core.count(core[-1]) > core.count({")": "(", "]": "[", "}": "{"}[core[-1]]):
            tail = core[-1] + tail
            core = core[:-1]
        lead = core[: len(core) - len(core.lstrip())]
        core = core.strip()
        if core and re.search(r"[A-Za-z0-9Ͱ-Ͽ∀-⋿]", core):
            res.append(lead + new("M", core) + tail)
        else:
            res.append(body)
    return "".join(res)


ADJ_SCRIPT = re.compile(r"^(?:\^\{[^{}]*\}|_\{[^{}]*\}|['′])+")


def _merge_scripts(s):
    """h_{t}_{−}_{1} -> h_{t−1} (the PDF splits sub/superscripts into one span per glyph run)."""
    for _ in range(8):
        s2 = re.sub(r"([_^])\{([^{}]*)\}\1\{", r"\1{\2", s)
        if s2 == s:
            break
        s = s2
    return s


def protect(raw, tagged=None):
    """Replace inline math, citation markers and URLs by {{M1}}/{{C1}}/{{U1}} placeholders.

    Returns (protected_text, placeholder_map, display_text)."""
    s = norm_ws(tagged if tagged else raw)
    s = _merge_scripts(s)
    ph = {}
    counters = Counter()

    def new(kind, value):
        counters[kind] += 1
        name = f"{kind}{counters[kind]}"
        ph[name] = _merge_scripts(re.sub("[-]", "", value))
        return "{{" + name + "}}"

    s = re.sub(r"\[[^\[\]]{2,120}\]", lambda m: re.sub("[-]", "", m.group(0))
               if RE_BRACKET_CITE.fullmatch(_merge_scripts(re.sub("[-]", "", m.group(0)))) else m.group(0), s)
    # 1. inline math from math-font spans
    if TAG_OPEN in s:
        s = _protect_math(s, new)
    s = re.sub("[\ue000-\ue003]", "", s)
    s = re.sub(r"(\d)- (?=[a-z])", r"\1-", s)  # "152- layer" left by line-end hyphenation
    # 2. URLs first (they may follow a footnote marker), then footnote / citation superscripts
    s = RE_URL.sub(lambda m: new("U", m.group(0)), s)
    s = re.sub(r"(?:(?<=\s)|^)\^\{\s*(\d{1,3}|[\*\u2217\u2020\u2021\u00a7\u00b6#]+)\s*\}(?=[A-Z{]|\s*$)", lambda m: new("C", "^{" + m.group(1) + "}"), s)
    s = RE_SUP_CITE.sub(lambda m: new("C", "^{" + m.group(1).strip() + "}"), s)
    # 3. remaining sub/superscript tokens (x_{i}, 10^{-3}) are math
    s = RE_BRACKET_CITE.sub(lambda m: new("C", m.group(0)), s)
    s = RE_SCRIPT_TOKEN.sub(lambda m: new("M", m.group(0)) if m.group(0).strip() else m.group(0), s)
    # 4. bracket citations, author-year citations
    s = RE_AUTHOR_YEAR.sub(lambda m: new("C", m.group(0)), s)
    if "�" in s:
        s = s.replace("�", "⟨?⟩")
    for k in ph:
        ph[k] = ph[k].replace("�", "⟨?⟩")
    display = restore(s, ph)
    return s, ph, display


def restore(text, ph, fmt="plain"):
    def rep(m):
        name = m.group(1)
        v = ph.get(name)
        if v is None:
            return m.group(0)
        if fmt == "html":
            e = html.escape(v)
            e = re.sub(r"\^\{([^{}]*)\}", r"<sup>\1</sup>", e)
            e = re.sub(r"_\{([^{}]*)\}", r"<sub>\1</sub>", e)
            cls = {"M": "math", "C": "cite", "U": "url"}[name[0]]
            return f'<span class="{cls}" title="{name}">{e}</span>'
        return v
    if fmt == "html":
        parts = re.split(r"(\{\{[MCU]\d+\}\})", text or "")
        return "".join(rep(PH_RE.match(p)) if PH_RE.fullmatch(p) else html.escape(p) for p in parts)
    return PH_RE.sub(rep, text or "")


def normalize_placeholders(text):
    return PH_LOOSE_RE.sub(lambda m: "{{" + re.sub(r"\s+", "", m.group(1)).upper() + "}}", text or "")


# ---------------------------------------------------------------- sentences
ABBR = re.compile(r"(?:\b(?:e\.g|i\.e|et al|Fig|Figs|Eq|Eqs|Eqn|Eqns|Sec|Secs|Tab|Ref|Refs|vs|cf|approx|No|Nos|Dr|Mr|Ms|Prof|resp|Vol|pp|ca|Inc|Ltd|Co|St|Jr|Sr|al|Suppl|Ch|Def|Thm|Prop|Lem|Cor|etc|Alg|Sect|Exp|Eqn)\.|\b[A-Z]\.)$")


def split_sentences(text, lang):
    t = norm_ws(text)
    if not t:
        return []
    if lang == "zh":
        parts = re.split(r"(?<=[。！？；])", t)
        return [p.strip() for p in parts if p.strip()]
    out, start = [], 0
    for m in re.finditer(r"[.!?][\"”’)\]]*\s+(?=[A-Z0-9\[(“\"{])", t):
        head = t[start:m.start() + 1]
        if ABBR.search(head):
            continue
        out.append(t[start:m.end()].strip())
        start = m.end()
    if start < len(t):
        out.append(t[start:].strip())
    return [o for o in out if o]


# ---------------------------------------------------------------- segmentation
SKIPPABLE_BETWEEN = {"footnote", "caption", "table", "figure_text", "header", "footer", "margin_note"}
SENT_END = re.compile(r"[\.\?\!。！？:：;；]\s*[\"'”’)\]]?\s*$")


def _seg(kind, src_raw, tagged=None, **kw):
    prot, ph, display = protect(src_raw, tagged)
    d = {"kind": kind, "prot": prot, "ph": ph, "src": display}
    d.update(kw)
    return d


def segments_from_pdftotext(path, password=None):
    """Fallback when PyMuPDF is missing: poppler pdftotext, pages split on form feeds, no bboxes."""
    import shutil
    import subprocess
    exe = shutil.which("pdftotext")
    if not exe:
        return [], [], {"status": "failed", "error": "no_pdf_backend", "message": "neither PyMuPDF nor pdftotext is available",
                        "meta": {"source_file": os.path.abspath(path)}}
    cmd = [exe, "-enc", "UTF-8"] + (["-upw", password] if password else []) + [path, "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace")[:300]
        return [], [], {"status": "failed", "error": "encrypted" if "password" in msg.lower() else "open_failed", "message": msg,
                        "meta": {"source_file": os.path.abspath(path)}}
    pages_txt = r.stdout.decode("utf-8", "replace").split("\f")
    segs, dropped, in_refs = [], [], False
    rep = Counter(norm_ws(re.sub(r"\d+", "#", l)) for p in pages_txt for l in p.splitlines()[:2] + p.splitlines()[-2:] if l.strip())
    for pno, ptxt in enumerate(pages_txt, 1):
        for chunk in re.split(r"\n\s*\n", ptxt):
            lines = [l for l in chunk.splitlines() if l.strip()]
            if not lines:
                continue
            t = norm_ws(re.sub(r"([A-Za-z]{2,})-\n([a-z])", r"\1\2", "\n".join(lines)))
            key = norm_ws(re.sub(r"\d+", "#", t))
            if len(lines) <= 2 and (re.fullmatch(r"[#\s\-–ivxlc]+", key) or rep[key] >= 3):
                dropped.append({"block": f"p{pno}", "page": pno, "role": "header_footer", "text": t[:200], "reason": "repeated running header/footer or page number"})
                continue
            loc = {"page": pno, "pages": [pno], "bbox": None, "column": None, "blocks": [f"p{pno}"], "section": "", "flags": ["no_layout_backend"]}
            if re.match(r"^\s*((\d+|[IVX]+)\.?\s*)?(references|bibliography|参考文献)\s*$", t, re.I):
                in_refs = True
                segs.append({"kind": "heading", "_raw": t, "_tag": t, "translate": True, "reason": None, **loc})
                continue
            if in_refs:
                segs.append({"kind": "reference", "src": t, "prot": t, "ph": {}, "translate": False, "reason": "bibliographic entry: kept in original", **loc})
                continue
            kind = "caption" if re.match(r"^(Fig\.|Figure|Table|图|表)\s*S?\d+", t) else \
                ("heading" if len(t) < 90 and not SENT_END.search(t) and re.match(r"^(\d+(\.\d+)*\.?|[IVX]+\.)\s+[A-Z]", t) else "paragraph")
            if kind == "paragraph" and segs and segs[-1]["kind"] == "paragraph" and "_raw" in segs[-1] and not SENT_END.search(segs[-1]["_raw"]) \
                    and re.match(r"^[a-z(\[,;]", t):
                segs[-1]["_raw"] += " " + t
                segs[-1]["_tag"] = segs[-1]["_raw"]
                segs[-1]["pages"] = sorted(set(segs[-1]["pages"] + [pno]))
                segs[-1]["joined"] = True
                continue
            segs.append({"kind": kind, "_raw": t, "_tag": t, "translate": True, "reason": None, **loc})
    for s in segs:
        if "_raw" in s:
            prot, ph, display = protect(s.pop("_raw"))
            s.pop("_tag")
            s.update(prot=prot, ph=ph, src=display)
    return segs, dropped, {"status": "success" if segs else "failed", "meta": {"source_file": os.path.abspath(path), "parser": "pdftotext fallback",
                                                                             "page_count": len(pages_txt)},
                           "warnings": [{"code": "fallback_extractor", "detail": "PyMuPDF unavailable: no bboxes, no math-font protection, simpler layout rules"}]}


def segments_from_pdf(path, workdir, password=None, pages=None, ocr="auto"):
    try:
        if os.environ.get("LT_NO_PYMUPDF"):
            raise ImportError("forced fallback")
        import pdf_layout  # vendored parser (PyMuPDF)
    except ImportError:
        return segments_from_pdftotext(path, password)
    pdir = os.path.join(workdir, "parsed")
    r = pdf_layout.parse(path, pdir, password=password, pages=pages, ocr_mode=ocr, preview=False)
    if r.get("status") == "failed":
        return [], [], r
    blocks = r["blocks"]
    tables = r.get("tables", [])
    refs = r.get("references", [])
    table_of_block = {}
    for ti, t in enumerate(tables):
        for b in blocks:
            if b["page"] == t["page"] and b["role"] == "table" and pdf_layout.bbox_center_in(b["bbox"], t["bbox"]):
                table_of_block.setdefault(b["id"], ti)
    ocr_pages = set(r.get("ocr_pages") or [])
    body_size = (r.get("meta") or {}).get("body_font_size") or 10
    figs = [f for f in r.get("figures", []) if f.get("bbox")]

    def in_figure(b):
        cx, cy = (b["bbox"][0] + b["bbox"][2]) / 2, (b["bbox"][1] + b["bbox"][3]) / 2
        return any(f["page"] == b["page"] and f["bbox"][0] - 2 <= cx <= f["bbox"][2] + 2 and f["bbox"][1] - 2 <= cy <= f["bbox"][3] + 2
                   for f in figs)

    def raw_tab(t):
        return "\n".join(" | ".join(norm_ws(c or "") for c in row) for row in t["rows"])

    def good_table(t):
        cells = [c for row in t["rows"] for c in row]
        filled = [c for c in cells if c and str(c).strip()]
        return bool(t.get("caption")) or (cells and len(filled) / len(cells) >= 0.5 and len(t["rows"]) >= 2)
    segs, dropped = [], []
    emitted_tables, refs_emitted = set(), False
    last_para = None  # index into segs of the last paragraph, for continuation joins
    between_roles = []
    for b in blocks:
        role = b["role"]
        text = b.get("marked") or b["text"]
        loc = {"page": b["page"], "pages": [b["page"]], "bbox": b["bbox"], "column": b.get("column"),
               "blocks": [b["id"]], "section": " > ".join(b.get("section_path") or []), "boxes": [[b["page"], b["bbox"]]]}
        flags = ["ocr"] if b["page"] in ocr_pages or b.get("source") == "ocr" else []
        if role == "table":
            ti = table_of_block.get(b["id"])
            if ti is None or ti in emitted_tables:
                if ti is None:
                    dropped.append({"block": b["id"], "page": b["page"], "role": "table_fragment", "text": norm_ws(b["text"])[:300],
                                    "reason": "text inside a table region that could not be rebuilt as a grid; kept in parsed/doc.json"})
                continue
            emitted_tables.add(ti)
            t = tables[ti]
            if not t.get("caption") and ("@" in raw_tab(t) or (b["page"] == blocks[0]["page"] and not any(
                    x["role"] in ("heading", "abstract") and x["page"] == b["page"] and x["bbox"][1] < t["bbox"][1] for x in blocks))):
                segs.append({"kind": "front_matter", "src": raw_tab(t), "prot": raw_tab(t), "ph": {}, "translate": False,
                             "reason": "author / affiliation grid kept in original", **loc, "bbox": t["bbox"], "flags": flags})
                continue
            if not good_table(t):
                dropped.append({"block": b["id"], "page": b["page"], "role": "diagram_grid", "text": norm_ws(b["text"])[:300],
                                "reason": "grid detected inside a diagram (no table caption, mostly empty cells); not a data table"})
                continue
            rows = [[norm_ws(c or "") for c in row] for row in t["rows"]]
            head, body = (rows[:2], rows[2:]) if len(rows) > 25 else (rows, [])
            raw = "\n".join(" | ".join(r_) for r_ in head)
            prot_lines, ph_all, disp_lines = [], {}, []
            alpha = len(re.findall(r"[A-Za-z\u4e00-\u9fff]{2,}", raw))
            seg = {"kind": "table", "src": raw, "prot": raw, "ph": {}, "table_id": t.get("id"), "table_caption": t.get("caption"),
                   "table_body_kept": [" | ".join(r_) for r_ in body], "n_rows": len(head),
                   "translate": alpha >= 2, "reason": None if alpha >= 2 else "numeric table: kept as in source", **loc,
                   "bbox": t["bbox"], "boxes": [[t["page"], t["bbox"]]], "flags": flags}
            segs.append(seg)
            between_roles.append("table")
            continue
        if role == "reference":
            if not refs_emitted:
                refs_emitted = True
                for k, rf in enumerate(refs):
                    segs.append({"kind": "reference", "src": rf["text"], "prot": rf["text"], "ph": {}, "translate": False,
                                 "reason": "bibliographic entry: kept in original language (convention; keeps citation keys searchable)",
                                 "ref_label": rf.get("label"), "page": rf.get("page"), "pages": [rf.get("page")], "bbox": None,
                                 "column": None, "blocks": [rf.get("block_id")], "section": loc["section"], "flags": []})
            continue
        if role in ("body", "abstract"):
            t = norm_ws(text)
            nw = len(t.split())
            if role == "body" and ((in_figure(b) and nw < 30) or (nw <= 4 and not SENT_END.search(t) and b.get("size", body_size) < body_size * 0.95)):
                dropped.append({"block": b["id"], "page": b["page"], "role": "figure_text", "text": t[:300],
                                "reason": "short label inside/near a figure graphic (axis, legend, diagram box); not running text"})
                continue
            if last_para is not None:
                p = segs[last_para]
                skippable = all(x in SKIPPABLE_BETWEEN for x in between_roles)
                cont = re.match(r"^(?:[a-z(\[,;]|[_^]\{|\{\{)", t) or (p["lang_hint"] == "zh" and not SENT_END.search(p["_raw"])) or \
                    re.search(r"(\b(in|of|the|a|an|and|or|to|for|with|by|on|from|as|at|that|than|is|are|was|were|be|into|via|for|where|and)|[,(\[–\-=+×−∑∏∫]|\{\{[MC]\d+\}\}|[_^]\{[^{}]*\})$", p["_raw"])
                if skippable and not SENT_END.search(p["_raw"]) and cont and p["section"] == loc["section"]:
                    hy = p["_raw"].endswith("-") and re.match(r"^[a-z]", t)
                    p["_raw"] = (p["_raw"][:-1] if hy else p["_raw"] + " ") + t
                    p["_tag"] = ((p["_tag"][:-1] if hy else p["_tag"] + " ") + norm_ws(b.get("tagged") or text))
                    p["pages"] = sorted(set(p["pages"] + [b["page"]]))
                    p["blocks"].append(b["id"])
                    p["boxes"].append([b["page"], b["bbox"]])
                    p["joined"] = True
                    p.setdefault("joined_over", []).extend(between_roles)
                    between_roles = []
                    continue
            segs.append({"kind": "paragraph", "_raw": t, "_tag": norm_ws(b.get("tagged") or text), "lang_hint": detect_lang(t),
                         "translate": True, "reason": None, **loc, "flags": flags,
                         "subkind": "abstract" if role == "abstract" else None})
            last_para = len(segs) - 1
            between_roles = []
            continue
        if role == "equation" and last_para is not None and not b.get("equation_number") and "⏎" not in text \
                and b.get("eq_confident", True) and len(text) <= 80 and not SENT_END.search(segs[last_para]["_raw"]) \
                and all(x in SKIPPABLE_BETWEEN for x in between_roles):
            # unnumbered one-line display formula that completes the sentence ("we use a rate of  P_drop = 0.1.")
            p = segs[last_para]
            p["_raw"] += " " + norm_ws(text)
            p["_tag"] += " " + TAG_OPEN + norm_ws(text).rstrip(".,;") + TAG_CLOSE + (norm_ws(text)[-1] if norm_ws(text)[-1] in ".,;" else "")
            p["blocks"].append(b["id"])
            p["boxes"].append([b["page"], b["bbox"]])
            p["flags"] = p.get("flags", []) + ["inline_display_formula_merged"]
            between_roles = []
            continue
        between_roles.append(role)
        if role in ("caption", "footnote"):
            segs.append({"kind": role, "_raw": norm_ws(text), "_tag": norm_ws(b.get("tagged") or text), "translate": True,
                         "reason": None, **loc, "flags": flags})
            continue
        last_para = None if role in ("heading", "title", "equation") else last_para
        if role == "title" and segs and segs[-1]["kind"] == "title" and "_raw" in segs[-1] and segs[-1]["page"] == b["page"]:
            p = segs[-1]  # a title wrapped over several lines/blocks is one segment
            p["_raw"] += " " + norm_ws(text)
            p["_tag"] += " " + norm_ws(b.get("tagged") or text)
            p["blocks"].append(b["id"])
            p["boxes"].append([b["page"], b["bbox"]])
            continue
        if role in ("title", "heading"):
            segs.append({"kind": role, "_raw": norm_ws(text), "_tag": norm_ws(b.get("tagged") or text), "translate": True,
                         "reason": None, **loc, "flags": flags})
        elif role == "equation":
            ok = bool(b.get("eq_confident"))
            notes = b.get("eq_notes") or []
            segs.append({"kind": "equation", "src": norm_ws(text), "prot": norm_ws(text), "ph": {}, "translate": False,
                         "reason": ("display equation rebuilt from the text layer (fraction bars and sub/superscripts recovered); kept untranslated"
                                    if ok else "display equation NOT reliably recoverable from the text layer (" + "; ".join(notes) +
                                    ") — 待确认: compare with the crop image"),
                         "eq_number": b.get("equation_number"), "eq_status": "ok" if ok else "needs_confirmation", "eq_notes": notes,
                         **loc, "flags": flags + ["math_from_text_layer"] + ([] if ok else ["needs_confirmation"])})
        elif role == "author_affiliation":
            segs.append({"kind": "front_matter", "src": norm_ws(text), "prot": norm_ws(text), "ph": {}, "translate": False,
                         "reason": "author names / affiliations / e-mails kept in original", **loc, "flags": flags})
        else:
            dropped.append({"block": b["id"], "page": b["page"], "role": role, "text": norm_ws(b["text"])[:300],
                            "reason": f"role '{role}' is not part of the running text"})
    for hf in r.get("headers_footers", []):
        why = {"figure_text": "text drawn inside a figure (axis labels, legends); not translated",
               "margin_note": "rotated/margin text (e.g. arXiv stamp)"}.get(hf["role"], "running header/footer or page number")
        dropped.append({"block": hf["id"], "page": hf["page"], "role": hf["role"], "text": norm_ws(hf["text"])[:200], "reason": why})
    for s in segs:
        if "_raw" in s:
            prot, ph, display = protect(s["_raw"], s["_tag"] if TAG_OPEN in s["_tag"] else None)
            s.update(prot=prot, ph=ph, src=display)
            s.pop("_raw"), s.pop("_tag")
            s.pop("lang_hint", None)
    # pages without a usable text layer (and no OCR): one segment per layout region, to be transcribed from its crop image
    page_info = {p["page"]: p for p in r.get("pages", [])}
    for p in r.get("unresolved_pages") or []:
        info = page_info.get(p, {})
        regs = info.get("image_regions") or []
        if not regs:
            regs = [{"id": f"p{p}-page", "bbox": [0, 0, info.get("width", 0), info.get("height", 0)]}]
        new = []
        for rg in regs:
            bb = [round(float(v), 1) for v in rg["bbox"]]
            if (bb[2] - bb[0]) < 6 or (bb[3] - bb[1]) < 3:
                continue
            gran = rg.get("granularity", "region")
            new.append({"kind": "image_region", "src": "", "prot": "", "ph": {}, "translate": True, "needs_transcription": True,
                        "page": p, "pages": [p], "bbox": bb, "boxes": [[p, bb]], "column": None, "blocks": [rg["id"]], "section": "",
                        "flags": ["no_text_layer"] + (["page_level_region"] if gran == "page" else []),
                        "granularity": gran, "degraded": rg.get("degraded"),
                        "reason": "page has no text layer and OCR was unavailable: transcribe this region from its crop image "
                                  "(tr.py regions / tr.py transcribe), or mark it UNREADABLE / NONTEXT"
                                  + (" — whole-page region because " + rg["degraded"] if rg.get("degraded") else "")})
        at = next((i for i, s in enumerate(segs) if (s.get("page") or 0) > p), len(segs))
        segs[at:at] = new
    return segs, dropped, r


def seg_num(sid):
    """'S0012' -> (12, 0); 'S0012.3' -> (12, 3) — for ordering and id ranges."""
    m = re.match(r"S(\d+)(?:\.(\d+))?$", sid or "")
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


TRANSCRIBE_KINDS = {"paragraph", "heading", "title", "caption", "table", "equation", "footnote", "reference", "list_item"}


def transcription_parts(text):
    """Split a transcription into (kind, text) parts: blank-line separated; '# ' heading, ' | ' rows -> table,
    '$$..$$' -> equation, 'Table/Figure N' -> caption, else paragraph. An explicit '[kind]' first line wins."""
    parts = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        lines = [l.rstrip() for l in chunk.splitlines() if l.strip()]
        if not lines:
            continue
        kind = None
        m = re.match(r"^\[(\w+)\]\s*$", lines[0].strip())
        if m and m.group(1) in TRANSCRIBE_KINDS:
            kind, lines = m.group(1), lines[1:]
        body = "\n".join(lines)
        if not body.strip():
            continue
        if kind is None:
            if re.match(r"^#{1,6}\s+", lines[0]) and len(lines) == 1:
                kind, body = "heading", re.sub(r"^#{1,6}\s+", "", lines[0])
            elif sum(1 for l in lines if " | " in l) >= 2:
                kind = "table"
            elif body.strip().startswith("$$"):
                kind = "equation"
            elif re.match(r"^\s*(Fig\.|Figure|FIGURE|Table|TABLE|图|表)\s*S?\d+[a-z]?\s*[:.：．]", body):
                kind = "caption"
            else:
                kind = "paragraph"
        if kind != "table":
            body = norm_ws(body)
        parts.append((kind, body))
    return parts


def _text_blocks_md(text):
    """Split markdown / plain text into (kind, text, line_no) blocks."""
    out, buf, start = [], [], 1
    in_eq = False
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        st = line.strip()
        if in_eq:
            buf.append(line)
            if re.search(r"\$\$\s*$|\\end\{(equation|align|eqnarray|gather|multline)\*?\}", st):
                out.append(("equation", "\n".join(buf), start))
                buf, in_eq = [], False
            continue
        if re.match(r"^(\$\$|\\begin\{(equation|align|eqnarray|gather|multline)\*?\})", st):
            if buf:
                out.append(("paragraph", " ".join(buf), start))
                buf = []
            start = i
            buf = [line]
            if st.count("$$") >= 2 or re.search(r"\\end\{", st):
                out.append(("equation", line, i))
                buf = []
            else:
                in_eq = True
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", st) or re.match(r"^\\(?:sub)*section\*?\{(.*)\}\s*$", st)
        if m:
            if buf:
                out.append(("paragraph", " ".join(buf), start))
                buf = []
            out.append(("heading", m.group(m.lastindex), i))
            continue
        if not st:
            if buf:
                out.append(("paragraph", " ".join(buf), start))
                buf = []
            continue
        if not buf:
            start = i
        buf.append(st)
    if buf:
        out.append(("equation" if in_eq else "paragraph", " ".join(buf), start))
    return out


def _latex_inline_tag(s):
    """Mark $...$, \\( \\) and \\cite{...}/\\ref{...} in text sources so protect() keeps them."""
    s = re.sub(r"(?<!\\)\$(?!\$)(.+?)(?<!\\)\$", lambda m: TAG_OPEN + "$" + m.group(1) + "$" + TAG_CLOSE, s)
    s = re.sub(r"\\\((.+?)\\\)", lambda m: TAG_OPEN + "\\(" + m.group(1) + "\\)" + TAG_CLOSE, s)
    s = re.sub(r"\\(?:cite[pt]?|ref|eqref|autoref|cref|Cref|label|url)\{[^{}]*\}", lambda m: TAG_OPEN + m.group(0) + TAG_CLOSE, s)
    return s


def docx_text(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    paras = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        style = re.search(r'<w:pStyle w:val="([^"]+)"', p)
        texts = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))
        texts = html.unescape(texts)
        if not texts.strip():
            paras.append("")
            continue
        if style and re.match(r"(Heading|heading|Title|标题)", style.group(1)):
            paras.append("# " + texts)
        else:
            paras.append(texts)
        paras.append("")
    return "\n".join(paras)


def segments_from_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        text = docx_text(path)
    else:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
        if ext in (".html", ".htm"):
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
            text = re.sub(r"(?i)<h[1-6][^>]*>", "\n\n# ", text)
            text = re.sub(r"(?i)</(p|div|h[1-6]|li|tr)>", "\n\n", text)
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    segs = []
    in_refs = False
    for kind, t, line in _text_blocks_md(text):
        loc = {"page": None, "pages": [], "bbox": None, "column": None, "blocks": [f"L{line}"], "section": "", "line": line, "flags": []}
        if kind == "heading":
            in_refs = bool(re.match(r"^\s*(\d+\.?\s*)?(references|bibliography|参考文献)\s*$", t, re.I))
        if kind == "equation":
            segs.append({"kind": "equation", "src": t, "prot": t, "ph": {}, "translate": False,
                         "reason": "display equation: copied verbatim", **loc})
            continue
        if in_refs and kind == "paragraph":
            segs.append({"kind": "reference", "src": t, "prot": t, "ph": {}, "translate": False,
                         "reason": "bibliographic entry: kept in original language", **loc})
            continue
        if kind == "paragraph" and re.match(r"^\s*(Fig\.|Figure|Table|图|表)\s*S?\d+", t):
            kind = "caption"
        tagged = _latex_inline_tag(t)
        prot, ph, display = protect(t, tagged if TAG_OPEN in tagged else None)
        segs.append({"kind": kind, "src": display, "prot": prot, "ph": ph, "translate": True, "reason": None, **loc})
    # section paths
    sec = ""
    for s in segs:
        if s["kind"] == "heading":
            sec = s["src"]
        s["section"] = sec
    return segs, [], {"status": "success", "meta": {"source_file": os.path.abspath(path)}}


def foreign_sentences(text, tgt_lang):
    """Sentences written in the other language inside a segment (e.g. an English sentence in a Chinese paragraph)."""
    t = strip_ph(text)
    if tgt_lang == "zh":
        cands = re.findall(r"[A-Za-z][A-Za-z0-9 ,;:'’\"()\-]{20,}[\.!?]", t)
        return [c for c in cands if len(latin_words(c)) >= 5 and han_count(c) == 0]
    return [c for c in re.findall(r"[一-鿿][^。！？]{6,}[。！？]", t) if han_count(c) >= 6]


def finish_segments(segs, tgt_lang):
    """Assign stable ids, language, sentences, size; mark segments already in the target language."""
    out = []
    for i, s in enumerate(segs, 1):
        s["id"] = f"S{i:04d}"
        lang = detect_lang(strip_ph(s.get("prot", "")))
        s["lang"] = lang
        if s["kind"] == "image_region":
            s.update(sentences=[], units=0)
            s.setdefault("flags", [])
            s.setdefault("in_scope", True)
            out.append(s)
            continue
        mixed = foreign_sentences(s.get("prot", ""), tgt_lang) if s.get("translate") and lang == tgt_lang else []
        if mixed:
            s["flags"] = s.get("flags", []) + ["mixed_language"]
            s["mixed_note"] = f"{len(mixed)} sentence(s) in the source language inside a {tgt_lang} segment: translate those, keep the rest"
        if s.get("translate") and lang == tgt_lang and s["kind"] != "table" and not mixed:
            s["translate"] = False
            s["reason"] = f"already in the target language ({tgt_lang}); kept as is"
        if s.get("translate") and lang == "und" and s["kind"] != "table":
            s["translate"] = False
            s["reason"] = "no translatable words (symbols/numbers only); kept as is"
        s["sentences"] = split_sentences(s.get("src", ""), lang if lang != "und" else "en")
        s["units"] = size_units(s.get("prot", ""), lang if lang != "und" else "en")
        s.setdefault("flags", [])
        if "⟨?⟩" in s.get("prot", "") or any("⟨?⟩" in v for v in (s.get("ph") or {}).values()):
            s["flags"] = s["flags"] + ["unmapped_glyph"]
        if s["kind"] in ("paragraph", "heading") and s.get("translate", True) and len(latin_words(strip_ph(s.get("prot", "")))) <= 2                 and (any(k.startswith("M") for k in (s.get("ph") or {})) or re.search(r"[=∑∫^_]", s.get("src", ""))):
            s["flags"] = s["flags"] + ["formula_fragment"]
        s.setdefault("in_scope", True)
        out.append(s)
    return out


# ---------------------------------------------------------------- glossary
STOP = set("""a an the of in on at to for from by with and or but as is are was were be been being this that these those
it its we our us they their them he she his her i you your which who whom whose what when where why how than then there here
can could may might must shall should will would do does did done not no nor so such also only both each every all any some
more most less least very much many few one two three four five six seven eight nine ten into over under between through
about above below after before during while if else however thus therefore hence since because via per et al use used using
based show shows shown given give gives new other same different first second third further e.g i.e fig figure table eq section""".split())
GENERIC_PHRASES = {"et al", "in this paper", "for example", "in addition", "as shown", "in order", "state of the art"}


def _term_regex(term):
    words = re.split(r"[\s\-]+", term.strip())
    parts = [re.escape(w) for w in words[:-1]]
    last = words[-1]
    if re.fullmatch(r"[a-z]+", last):
        if last.endswith("y") and len(last) > 2:
            lastp = re.escape(last[:-1]) + r"(?:y|ies)"
        elif last.endswith(("s", "x", "ch", "sh")):
            lastp = re.escape(last) + r"(?:es|s)?"
        else:
            lastp = re.escape(last) + r"(?:s|es)?"
    else:
        lastp = re.escape(last) + r"(?:s)?"
    body = r"[\s\-]+".join(parts + [lastp])
    ci = term == term.lower() or (term[0].isupper() and term[1:] == term[1:].lower() and " " in term)
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.I if ci else 0)


def parse_glossary_lines(lines, origin):
    """Lines like `source => target ! forbidden1|forbidden2  # note`; target `=` keeps the source form.
    Also accepts TSV (`source<TAB>target[<TAB>variants]`) and CSV with 2+ columns."""
    entries = []
    for raw in lines:
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        note = ""
        if " #" in line:
            line, note = line.split(" #", 1)
        variants = []
        if "=>" in line:
            src, rest = line.split("=>", 1)
            if "!" in rest:
                rest, v = rest.split("!", 1)
                variants = [x.strip() for x in re.split(r"[|｜]", v) if x.strip()]
            tgt = rest.strip()
        elif "\t" in line:
            cols = line.split("\t")
            src, tgt = cols[0], cols[1] if len(cols) > 1 else ""
            if len(cols) > 2:
                variants = [x.strip() for x in re.split(r"[|｜]", cols[2]) if x.strip()]
            if len(cols) > 3:
                note = cols[3]
        elif "," in line and line.count(",") >= 1:
            cols = [c.strip().strip('"') for c in line.split(",")]
            src, tgt = cols[0], cols[1]
        else:
            continue
        src, tgt = src.strip(), tgt.strip()
        if not src or not tgt or src.lower() in ("source", "term", "英文", "原文", "en") and tgt.lower() in ("target", "translation", "中文", "译文", "zh"):
            continue
        keep = tgt == "="
        entries.append({"source": src, "target": src if keep else tgt, "keep_original": keep,
                        "variants": [v for v in variants if v != tgt], "origin": origin, "note": note.strip()})
    return entries


def load_glossary_file(path, origin):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        data = read_json(path, [])
        if isinstance(data, dict):
            data = [{"source": k, "target": v} for k, v in data.items() if isinstance(v, str)] or data.get("terms", [])
        out = []
        for d in data:
            if isinstance(d, dict):
                src = d.get("source") or d.get("term") or d.get("en") or d.get("原文")
                tgt = d.get("target") or d.get("translation") or d.get("zh") or d.get("译文")
                if src and tgt:
                    out.append({"source": src, "target": tgt, "keep_original": tgt == src, "variants": d.get("variants", []),
                                "origin": origin, "note": d.get("note", "")})
        return out
    if ext == ".xlsx":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True)
            lines = []
            for row in wb.active.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if len(cells) >= 2:
                    lines.append("\t".join(cells))
            return parse_glossary_lines(lines, origin)
        except Exception:
            return []
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    lines = text.splitlines()
    if ext == ".md":  # markdown table | a | b |
        lines = ["\t".join(c.strip() for c in l.strip().strip("|").split("|")) for l in lines
                 if l.strip().startswith("|") and not re.match(r"^\|?\s*:?-{2,}", l.strip())]
    return parse_glossary_lines(lines, origin)


def merge_glossaries(*lists):
    """Later lists do NOT override earlier ones (priority order: case > agent > base)."""
    seen, out = {}, []
    for lst in lists:
        for e in lst:
            key = e["source"].lower()
            if key in seen:
                prev = seen[key]
                if prev["target"] != e["target"] and e["target"] not in prev["variants"] and prev["origin"] != "base":
                    prev.setdefault("conflicts", []).append({"origin": e["origin"], "target": e["target"]})
                elif prev["origin"] == "base" and e["origin"] != "base":
                    pass
                continue
            e = dict(e)
            e["rx"] = None
            seen[key] = e
            out.append(e)
    out.sort(key=lambda e: -len(e["source"]))
    return out


def compile_glossary(gl):
    gl.sort(key=lambda e: -len(e["source"]))
    for e in gl:
        e["_rx"] = _term_regex(e["source"])
    return gl


def find_terms(text, gl):
    """Longest-match-first occurrences of glossary terms in a source text (placeholders removed)."""
    t = strip_ph(text)
    taken = [False] * len(t)
    hits = Counter()
    for e in gl:
        for m in e["_rx"].finditer(t):
            if any(taken[m.start():m.end()]):
                continue
            for k in range(m.start(), m.end()):
                taken[k] = True
            hits[e["source"]] += 1
    return hits


def load_base_glossary(src_lang, tgt_lang):
    path = os.path.join(RES, "glossary_base.txt")  # tab-separated; .txt because Skill resources must be plain text types
    if not os.path.exists(path):
        path = os.path.join(RES, "glossary_base.tsv")  # name used up to v0.3.0
    if not os.path.exists(path):
        return []
    ents = load_glossary_file(path, "base")
    if src_lang == "zh" and tgt_lang == "en":
        rev = []
        for e in ents:
            if not e["keep_original"]:
                rev.append({"source": e["target"], "target": e["source"], "keep_original": False, "variants": [],
                            "origin": "base", "note": ""})
        return rev
    return ents


def extract_term_candidates(segs, src_lang, limit=60):
    """Candidate terms: acronym definitions, repeated noun phrases, named entities (CamelCase / with digits)."""
    texts = [strip_ph(s.get("prot", "")) for s in segs if s.get("translate") and s.get("in_scope", True) and s["kind"] != "table"]
    first_seen = {}
    if src_lang == "zh":
        cnt = Counter()
        for i, t in enumerate(texts):
            for m in re.finditer(r"[\u4e00-\u9fff]{2,8}", t):
                w = m.group(0)
                for n in (2, 3, 4, 5, 6):
                    for k in range(0, len(w) - n + 1):
                        cnt[w[k:k + n]] += 1
            for m in re.finditer(r"[A-Za-z][A-Za-z0-9\-]{1,}", t):
                cnt[m.group(0)] += 1
        cands = [(c, w) for w, c in cnt.items() if c >= 3 and not re.fullmatch(r"(的|是|在|和|了|与|中|对|为|及|等)+", w)]
        cands.sort(key=lambda x: (-x[0] * len(x[1]), x[1]))
        out, kept = [], []
        for c, w in cands:
            if any(w in k and cnt[k] >= c * 0.8 for k in kept):
                continue
            kept.append(w)
            out.append({"term": w, "freq": c, "kind": "ngram"})
            if len(out) >= limit:
                break
        return out
    cands = {}
    # acronyms defined as "long form (ACR)"
    for i, t in enumerate(texts):
        for m in re.finditer(r"((?:[A-Za-z][A-Za-z\-]*\s+){1,7}?)\(([A-Z][A-Za-z0-9\-]*[A-Z0-9][a-z]?s?)\)", t):
            words, acr = m.group(1).split(), m.group(2).rstrip("s")
            letters = [c for c in acr if c.isupper()]
            if len(letters) < 2 or len(letters) < 0.6 * sum(c.isalpha() for c in acr):
                continue
            best = None
            for k in range(len(words) - 1, -1, -1):
                cand = words[k:]
                initials = [w[0].upper() for w in cand]
                if len(cand) >= 2 and initials[0] == letters[0] and abs(len(cand) - len(letters)) <= 1:
                    best = cand
                    break
            if best:
                lf = " ".join(best)
                cands.setdefault(lf.lower(), {"term": lf, "freq": 0, "kind": "acronym_def", "acronym": acr, "first": i})
                cands.setdefault(acr, {"term": acr, "freq": 0, "kind": "acronym", "first": i})
    # n-grams
    cnt, nam = Counter(), Counter()
    for i, t in enumerate(texts):
        toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", t)
        low = [w.lower() for w in toks]
        for n in (2, 3, 4):
            for k in range(len(low) - n + 1):
                g = low[k:k + n]
                if g[0] in STOP or g[-1] in STOP or any(w in STOP and w != "of" for w in g[1:-1]):
                    continue
                if any(len(w) < 2 or re.search(r"\d", w) for w in g):
                    continue
                key = " ".join(g)
                cnt[key] += 1
                first_seen.setdefault(key, i)
        for w in toks:
            if re.match(r"^\d", w):
                continue
            if (re.search(r"[a-z][A-Z]", w) or re.search(r"[A-Za-z]-?\d", w) or (w.isupper() and len(w) >= 2)) and w.lower() not in STOP:
                nam[w] += 1
                first_seen.setdefault(w, i)
    min_f = 2 if sum(len(t.split()) for t in texts) < 4000 else 3
    grams = [(c, g) for g, c in cnt.items() if c >= min_f and g not in GENERIC_PHRASES]
    grams.sort(key=lambda x: (-(x[0] * (1 + 0.5 * len(x[1].split()))), x[1]))
    chosen = []
    for c, g in grams:
        if any((g in k or k in g) and abs(cnt[k] - c) <= max(1, 0.3 * c) for k in chosen):
            continue
        chosen.append(g)
    for g in chosen[:limit]:
        cands.setdefault(g, {"term": g, "freq": cnt[g], "kind": "ngram", "first": first_seen.get(g)})
    for w, c in nam.most_common(25):
        if c >= 2:
            cands.setdefault(w, {"term": w, "freq": c, "kind": "name", "first": first_seen.get(w)})
    alltext = "\n".join(texts)
    for k, d in cands.items():
        if d["freq"] == 0:
            d["freq"] = len(_term_regex(d["term"]).findall(alltext))
    out = sorted(cands.values(), key=lambda d: ({"acronym_def": 0, "ngram": 1, "name": 2, "acronym": 3}[d["kind"]], -d["freq"]))
    return out[: limit + 30]


# ---------------------------------------------------------------- checks
NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.(\d+))?")
CN_DIGITS = "零一二三四五六七八九十"
# Chinese words that contain numeral characters without meaning a number
C_NUM_CONTEXT = re.compile(r"一些|一种|一个|一样|一致|一起|一般|一直|一定|一旦|一方面|进一步|统一|唯一|同一|单一|一次性|之一|万一|不一|一切|一系列|一部分|二者|三维|两者")


def cn_numeral(n):
    if n <= 10:
        return CN_DIGITS[n] if n != 2 else "二|两"
    if n < 20:
        return "十" + CN_DIGITS[n - 10]
    if n < 100:
        return CN_DIGITS[n // 10] + "十" + (CN_DIGITS[n % 10] if n % 10 else "")
    return None


def numbers(text):
    out = []
    for m in NUM_RE.finditer(strip_ph(text)):
        v = m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else "")
        out.append(v)
    return out


REF_EN = re.compile(
    r"\b(Fig(?:ure)?s?\.?|FIG\.?|Tables?|TABLE|Tab\.|Eqs?\.|Eqns?\.?|Equations?|Sec(?:tion)?s?\.?|§|Algorithms?|Alg\.|"
    r"Appendix|Theorem|Lemma|Proposition|Corollary|Definition|Chapter)\s*~?\(?\s*([A-Z]?\d+(?:\.\d+)*[a-z]?)\)?", re.I)
REF_LIST_TAIL = re.compile(r"(?:\s*(?:,|and|&|or|–|—|-|to)\s*(?:Fig(?:ure)?s?\.?|Tables?|Sec(?:tion)?s?\.?|Eqs?\.?|§)?\s*\(?[A-Z]?\d+(?:\.\d+)*[a-z]?\)?)+", re.I)
REF_LIST_ZH = r"(?:\s*(?:-|–|—|~|～|至|到|、|,|，|和|与|及|或)\s*第?\s*[A-Z]?\d+(?:\.\d+)*[a-z]?)*"
REF_KIND_EN = [("fig", r"^(Fig|FIG)"), ("table", r"^(Tab|TABLE)"), ("eq", r"^Eq"), ("sec", r"^(Sec|§)"), ("alg", r"^Alg"),
               ("app", r"^Appendix"), ("thm", r"^Theorem"), ("lem", r"^Lemma"), ("prop", r"^Proposition"), ("cor", r"^Corollary"),
               ("def", r"^Definition"), ("chap", r"^Chapter")]
REF_ZH_WORDS = {"fig": "图", "table": "表", "eq": "式|公式|方程|等式", "sec": "节|章|部分|§|小节", "alg": "算法", "app": "附录",
                "thm": "定理", "lem": "引理", "prop": "命题", "cor": "推论", "def": "定义", "chap": "章"}
REF_ZH = re.compile(r"(图|表|公式|式|方程|算法|附录|定理|引理|命题|推论|定义)\s*[\(（]?\s*(S?\d+(?:\.\d+)*[a-z]?)|第\s*(\d+(?:\.\d+)*)\s*([节章])")
REF_EN_WORDS = {"图": r"Fig(?:ure)?s?\.?", "表": r"Tables?|Tab\.", "公式": r"Eq(?:uation|n)?s?\.?|\(", "式": r"Eq(?:uation|n)?s?\.?|\(",
                "方程": r"Eq(?:uation|n)?s?\.?|\(", "算法": r"Algorithms?|Alg\.", "附录": r"Appendix", "定理": "Theorem", "引理": "Lemma",
                "命题": "Proposition", "推论": "Corollary", "定义": "Definition", "节": r"Sec(?:tion)?s?\.?|§", "章": r"Chapter|Sec(?:tion)?\.?"}

NEG_EN = re.compile(r"\b(not|no(?!\.\s*\d)|never|none|nor|neither|without|cannot|can't|won't|don't|doesn't|didn't|isn't|aren't|wasn't|"
                    r"weren't|hasn't|haven't|hadn't|shouldn't|wouldn't|couldn't|unable|lacks?|lacking|lacked|absence|absent|nothing|"
                    r"hardly|barely|rarely|seldom|unlikely|fail(?:s|ed|ing)?\s+to)\b", re.I)
NEG_ZH_NOISE = re.compile(r"不同|不断|非常|无论|不管|不论|不久|不少|无数|并且|困难|艰难|非线性|非平凡|非视觉|非残差|无监督|无参|未来|非对称|无线")
NEG_ZH = re.compile(r"不|没|未|无|非|否|别|勿|莫|缺乏|缺少|缺失|难以|很少|鲜有|极少|免")
NEG_ZH_SRC = re.compile(r"不|没有|没|未|无|非|否|勿|缺乏|缺少|难以|鲜有")
NEG_EN_TGT = NEG_EN

QUALIFIERS = [
    (r"\b(approximately|roughly|nearly|almost|around\s+\d|about\s+\d|~\s*\d|≈)", r"约|大约|大概|左右|近|几乎|差不多|~|≈|∼"),
    (r"\bat least\b", r"至少|不少于|不低于|起码"),
    (r"\b(at most|up to\s+\d)", r"至多|最多|不超过|高达|多达|达"),
    (r"\b(more than|greater than|over\s+\d|exceed(?:s|ed|ing)?)", r"超过|多于|大于|高于|以上|超出|逾"),
    (r"\b(less than|fewer than|below\s+\d|under\s+\d)", r"少于|小于|低于|不足|以下|不到"),
    (r"\b(only|solely|merely|exclusively)\b", r"仅|只|唯一|唯有|单单|单纯|专门|纯粹"),
    (r"\b(may|might)\b", r"可能|或许|也许|可|能|会|允许"),
    (r"\b(likely|probably|presumably)\b", r"可能|或许|大概|也许|很可能"),
    (r"\b(although|though|despite|in spite of|whereas|even if)\b", r"虽然|尽管|虽|即使|即便|不过|但|然而|而|纵然|哪怕"),
    (r"\b(because|due to|owing to|therefore|thus|hence|consequently|as a result)\b", r"因为|由于|因此|所以|故|因而|从而|于是|导致|结果|鉴于|得益于|源于|归因|这样|进而"),
    (r"\b(unless|only if|provided that)\b", r"除非|只有|只要|前提|才|否则|若|如果"),
    (r"\b(significantly|substantially)\b", r"显著|明显|大幅|大大|很大|显然"),
]
QUALIFIERS_ZH = [
    (r"约|大约|左右|近似", r"\b(approximately|about|around|roughly|nearly|almost|~|≈)"),
    (r"至少|不少于|不低于", r"\bat least\b|no (?:less|fewer) than|not less"),
    (r"仅|只有|唯一", r"\b(only|solely|merely|sole|unique)"),
    (r"可能|或许|也许", r"\b(may|might|could|possibl|likely|perhaps)"),
    (r"虽然|尽管|即使", r"\b(although|though|despite|even|while|whereas)"),
    (r"因为|由于|因此|所以", r"\b(because|due to|since|owing|therefore|thus|hence|consequently|as a result|so )"),
]

UNIT_ZH = {"s": "秒|s", "ms": "毫秒|ms", "h": "小时|h", "min": "分钟|分|min", "m": "米|m", "km": "千米|公里|km", "g": "克|g",
           "kg": "千克|公斤|kg", "mg": "毫克|mg", "K": "K|开", "°C": "°C|℃|摄氏度", "%": "%|％|百分之", "d": "天|日|d"}
RE_UNIT = re.compile(r"(\d)\s*(%|‰|°C|℃|K|nm|μm|µm|mm|cm|km|m|mg|kg|μg|ng|g|mL|ml|μL|L|mM|μM|nM|mol|Hz|kHz|MHz|GHz|THz|eV|meV|keV|MeV|GeV|TeV|"
                     r"mV|kV|V|mA|μA|A|mW|kW|MW|W|kJ|J|kPa|MPa|GPa|Pa|N|ms|μs|ns|ps|fs|s|h|min|d|dB|mT|T|Ω|kΩ|μF|pF|F|ppm|ppb|wt%|FLOPs|GFLOPs|TFLOPs)(?![A-Za-z])")

# ---- literature-translator 0.2: quantities, counted nouns, reverse cross-references, quantifiers
SCALE_EN = {"thousand": 1e3, "k": 1e3, "K": 1e3, "million": 1e6, "M": 1e6, "billion": 1e9, "B": 1e9, "trillion": 1e12, "T": 1e12}
SCALE_ZH = {"千": 1e3, "万": 1e4, "十万": 1e5, "百万": 1e6, "千万": 1e7, "亿": 1e8, "十亿": 1e9, "百亿": 1e10, "千亿": 1e11, "万亿": 1e12}
RE_SCALED = re.compile(r"(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?:\s*(thousand|million|billion|trillion)\b|([kKMBT])(?![A-Za-z0-9])"
                       r"|\s*(十万|百万|千万|十亿|百亿|千亿|万亿|千|万|亿))")


def scaled_quantities(text):
    """[(value, raw_number)] for numbers carrying a scale word: '6 billion', '175B', '392k', '60 亿', '1.28 万'."""
    out = []
    for m in RE_SCALED.finditer(text or ""):
        raw = m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else "")
        scale = SCALE_EN.get(m.group(3) or m.group(4) or "") or SCALE_ZH.get(m.group(5) or "")
        if scale:
            out.append((float(raw) * scale, raw))
            lo = RE_RANGE_HEAD.search((text or "")[:m.start()])
            if lo:  # "4-12 billion", "40 至 120 亿": the first end carries the same scale
                out.append((float(lo.group(1)) * scale, lo.group(1)))
    return out


RE_RANGE_HEAD = re.compile(r"(?<![A-Za-z0-9_.,])(\d+(?:\.\d+)?)\s*(?:-|–|—|~|～|至|到|to)\s*$")


COUNT_NOUNS = {
    "token": r"词元|标记|令牌|词符|token", "word": r"词|word", "document": r"文档|文章|篇|document", "sentence": r"句|sentence",
    "parameter": r"参数|parameter", "layer": r"层|layer", "example": r"样本|示例|样例|例子|example", "sample": r"样本|样例|采样|sample",
    "image": r"图像|图片|影像|张|image", "epoch": r"轮|周期|epoch", "step": r"步|step", "iteration": r"迭代|次|iteration",
    "gpu": r"GPU|显卡", "hour": r"小时|h\b|hour", "day": r"天|日|day", "minute": r"分钟|min", "second": r"秒|s\b",
    "galaxy": r"星系|galax", "nebulae": r"星云|nebula", "nebula": r"星云|nebula", "object": r"天体|对象|目标|物体|object", "star": r"恒星|星",
    "patient": r"患者|病人|patient", "participant": r"参与者|被试|受试", "cell": r"细胞|单元", "language": r"语言|language",
    "class": r"类|class", "head": r"头|head", "dimension": r"维|dimension", "user": r"用户|user", "query": r"查询|query",
    "question": r"问题|question", "pair": r"对|pair", "task": r"任务|task", "model": r"模型|model", "group": r"组|群|group",
}
RE_COUNT_NOUN = re.compile(r"(?<![A-Za-z0-9_.])(\d[\d,.]*\s*(?:thousand|million|billion|trillion|[kKMB])?)\s+(?:[A-Za-z\-]+\s+)?"
                           r"(tokens?|words?|documents?|sentences?|parameters?|layers?|examples?|samples?|images?|epochs?|steps?|"
                           r"iterations?|GPUs?|hours?|days?|minutes?|seconds?|galaxies|galaxy|nebulae|nebula|objects?|stars?|patients?|"
                           r"participants?|cells?|languages?|class(?:es)?|heads?|dimensions?|users?|quer(?:y|ies)|questions?|pairs?|tasks?|"
                           r"models?|groups?)\b", re.I)
COUNT_NOUNS.update({"galaxie": COUNT_NOUNS["galaxy"], "querie": COUNT_NOUNS["query"], "quer": COUNT_NOUNS["query"], "classe": COUNT_NOUNS["class"]})

ZH_WORD_KIND = {"图": "fig", "表": "table", "式": "eq", "公式": "eq", "方程": "eq", "等式": "eq", "算法": "alg", "附录": "app",
                "定理": "thm", "引理": "lem", "命题": "prop", "推论": "cor", "定义": "def", "节": "sec", "章": "chap"}
REF_KIND_NAME = {"fig": "Figure", "table": "Table", "eq": "Equation", "sec": "Section", "alg": "Algorithm", "app": "Appendix",
                 "thm": "Theorem", "lem": "Lemma", "prop": "Proposition", "cor": "Corollary", "def": "Definition", "chap": "Chapter"}
# reference words in Chinese text, excluding common compounds (代表 2, 形式 3, 试图 1, 环节 4 ...)
REF_ZH_STRICT = re.compile(r"(?:(?<![代列报外发仪电时课统年])(表)|(?<![形方公模格样正算等仪程版句])(式)|(公式|方程|等式|算法|附录|定理|引理|命题|推论|定义)"
                           r"|(?<![试企意地构视制插附蓝])(图))\s*[\(（]?\s*(S?\d+(?:\.\d+)*[a-z]?)(?![\d.])|第\s*(\d+(?:\.\d+)*)\s*([节章])")
POLARITY = [  # English direction word -> acceptable Chinese renderings (warn only)
    (r"\b(small|smaller|smallest)\b", r"小|少|微|低|短"),
    (r"\b(large|larger|largest)\b", r"大|多|高|广|巨|长"),
    (r"\b(high|higher|highest)\b", r"高|大|多|强"),
    (r"\b(low|lower|lowest)\b", r"低|小|少|弱|下"),
    (r"\b(increas\w*)\b", r"增|升|提高|提升|加|扩大|变大|上涨|越来越|日益|逐渐|愈|渐"),
    (r"\b(decreas\w*|reduc\w*)\b", r"减|降|少|缩|下降|变小|削"),
    (r"\b(faster|fastest)\b", r"快|速"),
    (r"\b(slower|slowest)\b", r"慢|缓"),
    (r"\b(positive\w*)\b", r"正|积极|阳"),
    (r"\b(negative\w*)\b", r"负|消极|阴|否定"),
    (r"\b(overweight\w*|overestimat\w*)\b", r"过高|过重|过大|过度|高估"),
    (r"\b(underweight\w*|underestimat\w*)\b", r"过低|过轻|过小|不足|低估"),
]
UNIV_ZH = re.compile(r"普遍|一律|总是|始终|全部|所有|任何|一切")  # 无论 renders "whether ... or" too: not a quantifier
UNIV_EN = re.compile(r"\b(all|every\w*|always|each|any\w*|universal\w*|general\w*|whole|entire\w*|both|whenever|regardless|throughout|"
                     r"constant\w*|uniform\w*|wide\w*|common\w*|broad\w*|never|none|whatever|irrespective)\b", re.I)

# ---- literature-translator 0.3: units with dimension + scale, partial negation, subject binding, table cells
DATA_UNITS = [  # (english regex, family, scale exponent of 1000) ; Chinese renderings below
    (r"bytes?", "byte", 0), (r"kilobytes?|KB|kB", "byte", 1), (r"megabytes?|MB", "byte", 2),
    (r"gigabytes?|GB", "byte", 3), (r"terabytes?|TB", "byte", 4), (r"petabytes?|PB", "byte", 5),
    (r"bits?", "bit", 0), (r"kilobits?|kbits?|Kb", "bit", 1), (r"megabits?|Mbits?|Mb", "bit", 2), (r"gigabits?|Gbits?|Gb", "bit", 3),
]
ZH_DATA = {("byte", 0): r"字节|B\b|bytes?", ("byte", 1): r"千字节|KB|kB|kilobytes?", ("byte", 2): r"兆字节|百万字节|MB|megabytes?",
           ("byte", 3): r"吉字节|千兆字节|GB|gigabytes?", ("byte", 4): r"太字节|TB|terabytes?", ("byte", 5): r"拍字节|PB|petabytes?",
           ("bit", 0): r"比特|位(?!置|于)|bits?", ("bit", 1): r"千比特|kbit|Kb|kilobits?", ("bit", 2): r"兆比特|百万比特|Mbit|Mb|megabits?",
           ("bit", 3): r"吉比特|千兆比特|Gbit|Gb|gigabits?"}
RE_DATA_SRC = re.compile(r"(\d[\d,.]*)\s*(?:-\s*)?(" + "|".join(u for u, _, _ in DATA_UNITS) + r")\b")

PARTIAL_NEG = [  # partial / scoped negation: the Chinese must keep the scope, not just "a" negator somewhere
    (r"\bnot always\b", r"并非总|不总|未必|不一定|不是总|并不总|并非一直|不尽然|并不一直|不都"),
    (r"\bnot necessarily\b", r"不一定|未必|并不一定|不必然|并非必然"),
    (r"\bnot all\b", r"并非所有|不是所有|并非全部|不是全部|并不都|并非都|未必都|不全|并非全"),
    (r"\bnot every\b", r"并非每|不是每|并不是每"),
    (r"\bnot (?:entirely|completely|fully)\b", r"不完全|并不完全|并非完全|未完全|不全然"),
    (r"\bno longer\b", r"不再|已不|已经不"),
    (r"\bnever\b", r"从不|从未|绝不|永不|决不|从来没|从来不|从没"),
]

SUBJECTS = [  # subject words whose rendering must stay next to "their" number / evaluative word
    (r"\bhumans?\b|\bpeople\b|\bannotators?\b|\bhuman\s+(?:performance|accuracy|baseline)", r"人类|人工|人们|标注者"),
    (r"\blanguage models?\b", r"语言模型"),
    (r"\bbaselines?\b", r"基线"),
    (r"\b(?:our|the proposed) (?:model|method|system)\b|\bours\b", r"我们的|本文"),
]
EVALUATIVE = [  # (english, chinese) evaluative words bound to a subject
    (r"\b(?:difficult|hard)\b", r"难"), (r"\beasy\b", r"易|便于|容易"),
]


def _anchors_en(text):
    """[(pos, key, zh_regex)] subject anchors in an English text: subject words, proper model names, placeholders."""
    out = []
    for rx, zh in SUBJECTS:
        for m in re.finditer(rx, text, re.I):
            out.append((m.start(), "subj:" + re.sub(r"[^a-z ]", "", m.group(0).lower()).strip().rstrip("s"), zh))
    for m in re.finditer(r"(?<![\w{])([A-Z][A-Za-z]*[A-Z0-9][A-Za-z0-9\-+]*|\{\{C\d+\}\})(?![\w}])", text):
        tok = m.group(1)
        if tok.isupper() and len(tok) <= 2:
            continue
        out.append((m.start(), "tok:" + tok, re.escape(tok)))
    return sorted(out)


def _nearest(pos, anchors, lo, hi, gap_ratio=1.6):
    """Nearest anchor within [lo, hi) if it is clearly nearer than the next different entity (else None: evidence too weak).
    Anchors written next to each other ("language model [ZHR+19]") are one entity."""
    c = sorted(((abs(p - pos), k, z, p) for p, k, z in anchors if lo <= p < hi), key=lambda x: x[0])
    if not c:
        return None
    first = c[0]
    second = next((x for x in c[1:] if x[1] != first[1] and abs(x[3] - first[3]) > 20), None)
    if second and second[0] < gap_ratio * max(first[0], 1):
        return None
    return first[:3]


def _sent_bounds(text, pos, zh=False):
    ends = r"[。！？；]" if zh else r"(?<=[.!?;])\s"
    starts = [0] + [m.end() for m in re.finditer(ends, text)]
    lo = max(s for s in starts if s <= pos)
    hi = min([s for s in starts if s > pos] + [len(text)])
    return lo, hi


def binding_issues(src, tgt):
    """Numbers (decimals / percentages) and evaluative words must stay attached to the same subject.
    Only reported when the source binding is unambiguous (clearly nearest subject in the same sentence)."""
    issues = []
    s, t = strip_ph(src).replace("{{", "{{"), tgt
    s = src  # keep placeholders as anchors
    a_src = _anchors_en(s)
    if len({k for _, k, _ in a_src}) < 2:
        return issues
    keys = {k: z for _, k, z in a_src}
    # an anchor is evidence only if the translation renders it at all (SOTA -> 当前最优 cannot be located)
    keys = {k: z for k, z in keys.items() if re.search(z, t)}
    a_src = [x for x in a_src if x[1] in keys]
    if len(keys) < 2:
        return issues

    def zh_anchors(text):
        out = []
        for k, z in keys.items():
            for m in re.finditer(z, text):
                out.append((m.start(), k, z))
        return sorted(out)
    a_tgt = zh_anchors(t)
    items = [(m.group(0), m.start(), re.escape(m.group(0))) for m in re.finditer(r"\d+\.\d+%?|\d+%", s)]
    items += [(m.group(0), m.start(), z) for rx, z in EVALUATIVE for m in re.finditer(rx, s, re.I)]
    found = []
    for tok, pos, trx in items:
        lo, hi = _sent_bounds(s, pos)
        src_a = _nearest(pos, a_src, lo, hi)
        if not src_a:
            continue
        occ = [m.start() for m in re.finditer(trx if not tok[0].isdigit() else re.escape(tok) + r"(?!\d)", t)]
        if len(occ) != 1 or (tok[0].isdigit() and len(re.findall(re.escape(tok) + r"(?!\d)", s)) != 1):
            continue  # repeated numbers / words: binding cannot be decided
        tlo, thi = _sent_bounds(t, occ[0], zh=True)
        tgt_a = _nearest(occ[0], a_tgt, tlo, thi, gap_ratio=1.0)
        if tgt_a:
            found.append((tok, src_a[1], tgt_a[1]))
    # anchors written next to each other in the source ("language model [ZHR+19]") are the same entity
    parent = {}

    def root(k):
        while parent.get(k, k) != k:
            k = parent[k]
        return k
    for (p1, k1, _), (p2, k2, _) in zip(a_src, a_src[1:]):
        if k1 != k2 and 0 < p2 - p1 <= 28 and not re.search(r"[.;]", s[p1:p2]):
            parent[root(k2)] = root(k1)
    found = [(x, root(a_), root(b_)) for x, a_, b_ in found]
    name = lambda k: k.split(":", 1)[1]
    # report only swaps: X belongs to a, Y belongs to b in the source, and the translation attaches X to b and Y to a
    for i, (x, xa, xb) in enumerate(found):
        for y, ya, yb in found[i + 1:]:
            if xa != xb and xa == yb and ya == xb:
                issues.append(f"'{x}' and '{y}' swapped their subjects: source binds '{x}' to '{name(xa)}' and '{y}' to '{name(ya)}', "
                              f"the translation the other way round")
    return issues


def table_cell_issues(src, tgt):
    """Numbers must stay in the same row and column."""
    sr = [r.split(" | ") for r in src.split("\n") if r.strip()]
    tr_ = [r.split(" | ") for r in tgt.split("\n") if r.strip()]
    if len(sr) != len(tr_):
        return []
    bad = []
    for i, (a, b) in enumerate(zip(sr, tr_)):
        if len(a) != len(b):
            continue
        for j, (x, y) in enumerate(zip(a, b)):
            if numbers(x) and _cell_values(x) != _cell_values(y):
                bad.append(f"row {i + 1} col {j + 1}: source {numbers(x)} vs translation {numbers(y)}")
    return bad


def _cell_values(cell):
    """Multiset of the quantities in a table cell; scale words resolved (410 billion = 4100 亿 = 410B)."""
    vals, used = Counter(), Counter()
    for v, raw in scaled_quantities(cell):
        vals[round(v, 6)] += 1
        used[raw] += 1
    for n in numbers(cell):
        if used[n] > 0:
            used[n] -= 1
        else:
            vals[n] += 1
    return vals


def _ph_counter(text):
    return Counter(PH_RE.findall(text or ""))


def check_segment(seg, tgt, gl, src_lang, tgt_lang):
    """Return list of issues {code, severity, detail} for one translated segment."""
    issues = []

    def add(code, sev, detail):
        issues.append({"code": code, "severity": sev, "detail": detail})
    src = seg.get("prot", "")
    if not tgt or not tgt.strip():
        add("empty", "error", "no translation")
        return issues
    # 1. placeholders (formulas, citations, URLs)
    ps, pt = _ph_counter(src), _ph_counter(tgt)
    miss = [k for k in ps if pt[k] < ps[k]]
    extra = [k for k in pt if pt[k] > ps.get(k, 0)]
    if miss:
        add("placeholder_missing", "error", "missing " + ", ".join(f"{{{{{k}}}}}={seg['ph'].get(k, '?')[:40]}" for k in miss))
    if extra:
        add("placeholder_extra", "error", "unknown/duplicated " + ", ".join("{{" + k + "}}" for k in extra))
    # 2. untranslated copy
    s_plain, t_plain = norm_ws(strip_ph(src)), norm_ws(strip_ph(tgt))
    if seg["kind"] != "table" and s_plain and s_plain == t_plain and len(latin_words(s_plain) if src_lang == "en" else s_plain) >= 3:
        add("untranslated_copy", "error", "translation is identical to the source")
    # 3. numbers (scale words: 6 billion = 60 亿 = 6B are the same quantity)
    ns, nt = Counter(numbers(src)), Counter(numbers(tgt))
    sq, tq = scaled_quantities(strip_ph(src)), scaled_quantities(strip_ph(tgt))
    matched_vals = []
    for val, raw in sq:
        hit = next((i for i, (v2, r2) in enumerate(tq) if v2 is not None and abs(v2 - val) <= 1e-9 * max(1.0, val)), None)
        if hit is not None:
            matched_vals.append(val)
            ns[raw] -= 1
            nt[tq[hit][1]] -= 1
            tq[hit] = (None, tq[hit][1])
    for i, (v2, r2) in enumerate(tq):  # the same quantity repeated in another notation, e.g. "60 亿（6 billion）"
        if v2 is not None and any(abs(v2 - v) <= 1e-9 * max(1.0, v) for v in matched_vals):
            nt[r2] -= 1
    for v, c in ns.items():
        if c <= 0 or nt[v] >= c:
            continue
        alt = None
        if tgt_lang == "zh" and re.fullmatch(r"\d+", v) and int(v) <= 99:
            alt = cn_numeral(int(v))
        n_alt = len(re.findall(alt, C_NUM_CONTEXT.sub("", tgt))) if alt else 0
        if nt[v] <= 0 and not n_alt:
            add("number_missing", "error", f"number '{v}' from the source is not in the translation")
        elif max(nt[v], 0) + n_alt < c:
            add("number_count", "warn", f"number '{v}' appears {c}x in source, {max(nt[v], 0)}x in translation")
    for v, c in nt.items():
        if c > 0 and ns[v] <= 0 and not (src_lang == "zh" and re.fullmatch(r"\d+", v) and int(v) <= 99):
            add("number_added", "warn", f"number '{v}' is in the translation but not in the source")
    # 4. units and counted nouns ("6 billion tokens" must not become "6 billion documents")
    t_units = [(x.group(2), x.start(1)) for x in RE_UNIT.finditer(strip_ph(tgt))]
    for m in RE_UNIT.finditer(strip_ph(src)):
        u = m.group(2)
        num = re.search(r"[\d.,]*\d$", strip_ph(src)[: m.start(1) + 1])
        alts = UNIT_ZH.get(u, re.escape(u)) if tgt_lang == "zh" else re.escape(u)
        if not re.search(r"(?:" + alts + r")", tgt):
            other = []
            if num:
                for tu, pos in t_units:
                    if tu != u and strip_ph(tgt)[: pos + 1].endswith(num.group(0)):
                        other.append(tu)
            if other:
                add("unit_changed", "error", f"'{num.group(0)} {u}' became '{num.group(0)} {other[0]}' in the translation")
            else:
                add("unit_missing", "warn", f"unit '{u}' (after {num.group(0) if num else '?'}) not found in translation")
    if src_lang == "en" and tgt_lang == "zh":
        for m in RE_DATA_SRC.finditer(strip_ph(src)):
            key = next(((fam, sc) for u, fam, sc in DATA_UNITS if re.fullmatch(u, m.group(2))), None)
            if not key:
                continue
            if not re.search(ZH_DATA[key], tgt):
                other = [k for k, z in ZH_DATA.items() if k != key and k[1] >= 1 and re.search(z, tgt)] or \
                        [k for k, z in ZH_DATA.items() if k[0] != key[0] and re.search(z, tgt)]
                if other:
                    add("unit_changed", "error", f"'{m.group(0)}' ({key[0]}, 1000^{key[1]}) is rendered as a {other[0][0]} unit "
                                                 f"of scale 1000^{other[0][1]} in the translation")
                else:
                    add("unit_missing", "warn", f"data unit of '{m.group(0)}' not found in the translation")
        for m in RE_COUNT_NOUN.finditer(strip_ph(src)):
            noun = m.group(2).lower()
            alts = COUNT_NOUNS.get(noun) or COUNT_NOUNS.get(re.sub(r"(es|s)$", "", noun)) or COUNT_NOUNS.get(noun[:-1])
            if alts and not re.search(alts, tgt, re.I):
                add("counted_noun_changed", "warn", f"'{m.group(0).strip()}': the counted noun '{noun}' ({alts}) is not in the translation")
    # 5. cross references (figures, tables, equations, sections) in both directions
    if src_lang == "en":
        src_refs = set()
        plain_src = strip_ph(src)
        for m in REF_EN.finditer(plain_src):
            kind = next(k for k, rx in REF_KIND_EN if re.match(rx, m.group(1), re.I))
            src_refs.add((kind, m.group(2).lower()))
            tail = REF_LIST_TAIL.match(plain_src, m.end())
            if tail:  # "Figures 3.14 and 3.15", "Sections 2.1-2.3": every listed number is a reference of the same kind
                for x in re.findall(r"[A-Z]?\d+(?:\.\d+)*[a-z]?", tail.group(0)):
                    src_refs.add((kind, x.lower()))
            n = re.escape(m.group(2))
            words = REF_ZH_WORDS[kind] if tgt_lang == "zh" else re.escape(m.group(1))
            pat = rf"(?:{words})\s*[\(（]?\s*{n}(?![0-9])|{n}{REF_LIST_ZH}\s*[\)）]?\s*(?:{words})"
            if kind == "eq":
                pat += rf"|(?<![图表节章法录理义论题])\s*[\(（]\s*{n}\s*[\)）]"
            if kind in ("sec", "chap") and tgt_lang == "zh" and re.fullmatch(r"\d+", m.group(2)):
                pat += "|第\\s*(?:" + (cn_numeral(int(m.group(2))) or "x") + ")\\s*[节章]"
            if not re.search(pat, tgt):
                add("crossref_mismatch", "error", f"'{m.group(0).strip()}' has no matching reference in the translation")
        if tgt_lang == "zh":
            for m in REF_ZH_STRICT.finditer(strip_ph(tgt)):
                w = m.group(1) or m.group(2) or m.group(3) or m.group(4) or m.group(7)
                n = m.group(5) or m.group(6)
                kind = ZH_WORD_KIND.get(w)
                if not kind or (kind, n.lower()) in src_refs:
                    continue
                if kind in ("sec", "chap") and any(k in ("sec", "chap") and x == n.lower() for k, x in src_refs):
                    continue
                if kind in ("sec", "chap") and "." in n and re.search(r"(?<![\w.])" + re.escape(n) + r"(?![\w.]*\d)", strip_ph(src)) \
                        and not any(x == n.lower() for _, x in src_refs):
                    continue  # bare section number in the source ("as discussed in 2.2")
                same_num = [k for k, x in src_refs if x == n.lower()]
                add("crossref_kind_mismatch", "error", f"'{m.group(0).strip()}' in the translation has no counterpart in the source"
                    + (f" (the source refers to {REF_KIND_NAME[same_num[0]]} {n})" if same_num else ""))
    else:
        for m in REF_ZH.finditer(strip_ph(src)):
            if m.group(1):
                w, n = m.group(1), m.group(2)
            else:
                w, n = m.group(4), m.group(3)
            pat = rf"(?:{REF_EN_WORDS.get(w, w)})\s*~?\s*\(?\s*{re.escape(n)}(?![0-9])"
            if not re.search(pat, tgt, re.I):
                add("crossref_mismatch", "error", f"'{m.group(0)}' has no matching reference in the translation")
    prose = seg["kind"] not in ("table", "equation")  # sentence-level heuristics do not apply to cell grids
    if prose:
        # 6. negation: overall count, then sentence by sentence when the sentence counts agree (scope)
        s_sents = split_sentences(s_plain, src_lang)
        t_sents = split_sentences(t_plain, tgt_lang)
        aligned = len(s_sents) == len(t_sents) and len(s_sents) >= 2
        if src_lang == "en":
            neg_s = lambda x: len(NEG_EN.findall(x))
            neg_t = lambda x: len(NEG_ZH.findall(NEG_ZH_NOISE.sub("", x)))
        else:
            neg_s = lambda x: len(NEG_ZH_SRC.findall(NEG_ZH_NOISE.sub("", x)))
            neg_t = lambda x: len(NEG_EN_TGT.findall(x)) + len(re.findall(r"\b(un|non|in|im|dis)[a-z]{3,}", x))
        ne, nz = neg_s(strip_ph(src)), neg_t(t_plain)
        if ne and nz == 0:
            add("negation_lost", "warn", f"source has {ne} negation cue(s); translation has none")
        elif ne > nz:
            add("negation_count", "warn", f"source has {ne} negation cue(s), translation only {nz}; check which statement lost its negation")
        elif aligned:
            for k, (a, b) in enumerate(zip(s_sents, t_sents), 1):
                if neg_s(a) and not neg_t(b):
                    add("negation_scope", "warn", f"sentence {k}: the source sentence is negated ('{a[:50]}…'), the aligned translation sentence is not")
                    break
        elif s_sents and t_sents:  # sentence counts differ: compare each negated source sentence with its proportional window
            n_s, n_t = len(s_sents), len(t_sents)
            for k, a in enumerate(s_sents):
                if not neg_s(a):
                    continue
                lo_, hi_ = max(0, (k * n_t) // n_s - 1), min(n_t, -(-((k + 1) * n_t) // n_s) + 1)
                if not any(neg_t(b) for b in t_sents[lo_:hi_]):
                    add("negation_scope", "warn", f"source sentence {k + 1} is negated ('{a[:50]}…') but no negation in translation sentences {lo_ + 1}-{hi_}")
                    break
        if src_lang == "en" and tgt_lang == "zh":
            for rx, zh in PARTIAL_NEG:
                mm = re.search(rx, strip_ph(src), re.I)
                if mm and not re.search(zh, t_plain):
                    add("negation_scope", "warn", f"'{mm.group(0)}' (scoped negation) has no scoped rendering ({zh.split('|')[0]}…) in the translation")
        # 7. qualifiers / logical connectives; quantifiers that widen a claim
        rules = QUALIFIERS if src_lang == "en" else QUALIFIERS_ZH
        for srx, trx in rules:
            ms = re.search(srx, strip_ph(src), re.I)
            if ms and not re.search(trx, tgt, re.I):
                add("qualifier_check", "warn", f"'{ms.group(0)}' — no corresponding qualifier/connective found; confirm the limiting/logical relation is kept")
        if src_lang == "en" and tgt_lang == "zh":
            mu = UNIV_ZH.search(t_plain)
            if mu and not UNIV_EN.search(strip_ph(src)):
                add("quantifier_added", "warn", f"'{mu.group(0)}' generalises the claim but the source has no universal quantifier (all/every/always/any…)")
            for msg in binding_issues(src, tgt):
                add("subject_binding", "warn", msg + " — check who/what the value refers to")
            # direction words (small/large, high/low, increase/decrease ...): a missing pole hints at a reversed claim
            for rx, zh in POLARITY:
                ms = next((m for m in re.finditer(rx, strip_ph(src), re.I) if not m.group(0).isupper()), None)  # LARGE = a name
                if ms and not re.search(zh, t_plain):
                    add("polarity_check", "warn", f"'{ms.group(0)}' has no counterpart ({zh}) in the translation; check the direction of the claim")
                    break
    # 8. glossary
    if gl:
        hits = find_terms(src, gl)
        for e in gl:
            if e["source"] in hits:
                tg = e["target"]
                ok = (tg.lower() in tgt.lower()) if e["keep_original"] or re.search(r"[A-Za-z]", tg) and not han_count(tg) else (tg in tgt)
                sev = "warn" if e["origin"] == "base" else "error"
                masked = tgt.replace(tg, "\0" * len(tg)) if tg else tgt
                bad = [v for v in e.get("variants", []) if v and v not in tg and v in masked]
                if not ok:
                    add("term_violation", sev, f"term '{e['source']}' must be rendered as '{tg}'" + (f" (found forbidden '{bad[0]}')" if bad else ""))
                elif bad:
                    add("term_violation", sev, f"forbidden rendering '{bad[0]}' of '{e['source']}' used (x{masked.count(bad[0])}) next to the locked '{tg}'; "
                                               f"the source has the term {hits[e['source']]}x")
    # 9. tables keep their grid
    if seg["kind"] == "table":
        sr = [r for r in src.split("\n") if r.strip()]
        tr = [r for r in tgt.split("\n") if r.strip()]
        if len(sr) != len(tr):
            add("table_rows", "error", f"table has {len(sr)} rows in source, {len(tr)} in translation")
        else:
            for k, (a, b) in enumerate(zip(sr, tr)):
                if a.count("|") != b.count("|"):
                    add("table_cells", "error", f"row {k + 1}: {a.count('|') + 1} cells in source, {b.count('|') + 1} in translation")
                    break
            else:
                moved = table_cell_issues(src, tgt)
                if moved:
                    add("table_cell_mismatch", "error", "; ".join(moved[:3]) + (f" (+{len(moved) - 3} more)" if len(moved) > 3 else ""))
    # 10. length ratio / residue / sentence count
    if src_lang == "en" and tgt_lang == "zh" and prose:
        w = len(latin_words(s_plain))
        h = han_count(t_plain)
        if w >= 12 and h and (h / w < 0.85 or h / w > 3.8):
            add("length_ratio", "warn", f"{h} Chinese chars for {w} English words (ratio {h / w:.2f}); possible omission or addition")
        keep = set(x.lower() for x in latin_words(s_plain) if re.search(r"[A-Z0-9]", x)) | {e["source"].lower() for e in gl if e.get("keep_original")}
        run = re.findall(r"(?:\b[a-z][a-z'\-]+\b[\s,]*){6,}", t_plain)
        run = [r for r in run if not all(x.lower() in keep for x in latin_words(r))]
        if run:
            add("untranslated_residue", "warn", f"English run left in translation: '{run[0][:60]}'")
        ss = len(split_sentences(s_plain, "en"))
        ts = len(split_sentences(t_plain, "zh"))
        if ss >= 3 and ts and (ts < ss * 0.5 or ts > ss * 2.0):
            add("sentence_count", "warn", f"{ss} source sentences vs {ts} translated sentences; check for merged/omitted sentences")
    elif src_lang == "zh" and prose:
        h = han_count(s_plain)
        w = len(latin_words(t_plain))
        if h >= 20 and w and (w / h < 0.3 or w / h > 1.3):
            add("length_ratio", "warn", f"{w} English words for {h} Chinese chars (ratio {w / h:.2f}); possible omission or addition")
        if han_count(t_plain) > 3:
            add("untranslated_residue", "warn", f"{han_count(t_plain)} Chinese characters left in translation")
    return issues


def term_stats(segs, trans, gl):
    """Per-term consistency: occurrences in source segments vs segments rendering the locked target."""
    stats = {}
    for e in gl:
        stats[e["source"]] = {"source": e["source"], "target": e["target"], "origin": e["origin"], "segments_with_term": 0,
                              "segments_consistent": 0, "violations": [], "variants": e.get("variants", []),
                              "keep_original": e["keep_original"], "note": e.get("note", "")}
    for s in segs:
        t = trans.get(s["id"], {}).get("tgt")
        if not s.get("translate") or not t:
            continue
        hits = find_terms(s["prot"], gl)
        for e in gl:
            if e["source"] in hits:
                st = stats[e["source"]]
                st["segments_with_term"] += 1
                tg = e["target"]
                ok = (tg.lower() in t.lower()) if (e["keep_original"] or not han_count(tg)) else (tg in t)
                if ok:
                    st["segments_consistent"] += 1
                else:
                    st["violations"].append(s["id"])
    return stats
