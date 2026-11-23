#!/usr/bin/env python3
"""Layout-aware scientific PDF parser with page/block provenance.

Usage:
    python parse_pdf.py INPUT.pdf --out OUTDIR [--password PW] [--preview]
                        [--pages 1-5] [--ocr auto|force|off] [--lang eng+chi_sim]

Writes OUTDIR/doc.json (structured result), document.md (reading-order
markdown with source anchors), blocks.jsonl (one block per line, for grep),
tables/*.{md,csv,html}, qa_report.json and, with --preview, previews/*.png
with the detected blocks and their reading order drawn on each page.

Every block id has the form p{page}-b{index}; every output element keeps the
page number, bbox (PDF points, origin top-left) and the rule that classified it.
"""
import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except ImportError:  # pragma: no cover
    import fitz  # type: ignore
if hasattr(fitz, "_layout_enabled"):  # PyMuPDF >= 2 with pymupdf_layout: keep the classic text/table extraction (this module does its own layout)
    fitz._layout_enabled = False
if hasattr(fitz, "no_recommend_layout"):
    fitz.no_recommend_layout()

PARSER_VERSION = "0.3.0-lt"

# ----------------------------------------------------------------- patterns
RE_CAPTION = re.compile(
    r"^\s*(Fig\.|Figure|FIGURE|Fig|Table|TABLE|Tab\.|Scheme|Algorithm|Supplementary\s+(?:Figure|Table)|"
    r"Extended\s+Data\s+(?:Fig\.|Figure|Table)|图|表)\s*(S?(?:[A-Z]\.?)?\d+(?:\.\d+)*[a-zA-Z]?|[IVX]+)\s*[\.:|：．]?",
)
RE_HEAD_NUM = re.compile(
    r"^\s*((?:\d+\.)*\d+)\.?\s+([A-Z一-鿿][^\n]{0,120})$"
)
RE_HEAD_ROMAN = re.compile(r"^\s*([IVX]{1,5})\.\s+([A-Z][^\n]{0,120})$")
RE_HEAD_ALPHA = re.compile(r"^\s*([A-H])\.\s+([A-Z][^\n]{0,100})$")
RE_HEAD_APPX = re.compile(r"^\s*([A-Z](?:\.\d+)*)\s+([A-Z][^\n]{1,120})$")
RE_HEAD_CN = re.compile(r"^\s*(第[一二三四五六七八九十]+[章节]|[一二三四五六七八九十]+、)\s*(.{1,60})$")
KNOWN_SECTIONS = re.compile(
    r"^\s*(abstract|introduction|background|related work|methods?|materials and methods|"
    r"experimental( section| details)?|results( and discussion)?|discussion|conclusions?|"
    r"acknowledg(e)?ments?|references|bibliography|appendix( [a-z])?|supplementary (information|materials?)|"
    r"data availability|author contributions|competing interests|摘\s*要|引\s*言|参考文献|结\s*论|致\s*谢)\s*:?\s*$",
    re.I,
)
RE_REFS_HEAD = re.compile(r"^\s*((\d+|[IVX]+)\.?\s*)?(references|bibliography|literature cited|参考文献)\s*$", re.I)
RE_EQ_NUM = re.compile(r"\(\s*((?:\d+\.)?\d+[a-z]?|[A-Z]\d+)\s*\)\s*$")
RE_PAGE_NUM = re.compile(r"^\s*(page\s*)?(\d{1,4}|[ivxlc]{1,6})(\s*(of|/)\s*\d{1,4})?\s*$", re.I)
RE_REF_SPLIT_BRACKET = re.compile(r"(?=(?:^|\s)\[\d{1,4}\]\s)")
RE_REF_START_NUM = re.compile(r"^\s*(\[\d{1,4}\]|\d{1,4}\.)\s+")
MATH_CHARS = set("=+−-×·∑∏∫∂∇√∞≈≠≤≥±∈∉⊂⊆∀∃→←↔⇒αβγδεζηθικλμνξοπρστυφχψωΓΔΘΛΞΠΣΦΨΩ^_()[]{}|/<>")
SENT_END = re.compile(r"[\.\?\!。！？:：;；]\s*[\"'”’)\]]?\s*$")


def norm_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def norm_key(s):
    """Normalisation used to match repeated headers/footers across pages."""
    s = norm_ws(s).lower()
    s = re.sub(r"\d+", "#", s)
    return s


# ----------------------------------------------------------------- open
def open_document(path, password=None):
    """Return (doc, error_code, message)."""
    if not os.path.exists(path):
        return None, "file_not_found", f"{path} does not exist"
    try:
        doc = fitz.open(path)
    except Exception as e:  # damaged / not a pdf
        return None, "open_failed", f"PyMuPDF could not open the file: {e}"
    if getattr(doc, "needs_pass", False):
        ok = doc.authenticate(password or "")
        if not ok:
            return None, "encrypted", "PDF is password protected; pass --password or ask a human for the password"
    if doc.page_count == 0:
        return None, "empty", "PDF has zero pages"
    return doc, None, ""


# ----------------------------------------------------------------- spans/lines
MATH_FONT = re.compile(r"^(CM(MI|SY|EX|BX|R|MIB|BSY)\d*|MSBM|MSAM|EUFM|EUSM|.*Math.*|Symbol.*|STIX.*|rsfs.*)", re.I)
SCRIPT_CAPS = {"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ", "M": "ℳ", "R": "ℛ"}
DOUBLE_CAPS = {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"}


# literature-translator patch: stricter than MATH_FONT (which also matches CMR, the LaTeX body font)
STRICT_MATH_FONT = re.compile(r"CMMI|CMSY|CMEX|CMBSY|MSBM|MSAM|EUFM|EUSM|EUEX|rsfs|LMMath|Math|^Symbol|MTMI|MTSY|MTEX|TXMI|TXSY|txmi|txsy|NewTXMI|esint|stmary|wasy", re.I)
TAG_OPEN, TAG_CLOSE = "", ""
TAG_WOPEN, TAG_WCLOSE = "", ""
WEAK_MATH_FONT = re.compile(r"^(CMR|CMBX|CMSL|CMSS|CMB\d|LMRoman)", re.I)
STRONG_MATH = set("�∑∏∫∮√∂∇≤≥≠≈≡∝∞±∓×÷∘→←↔⇒⇐⇔↦∈∉∋⊂⊃⊆⊇∪∩∧∨∀∃∅ℝℕℤℚℂ⊕⊗⊙⊥∥‖αβγδεϵζηθϑικλνξπϖρϱσςτυφϕχψωΓΔΘΛΞΠΣΥΦΨΩ")


def _mostly_math_symbols(t):
    core = [c for c in t if not c.isspace()]
    return bool(core) and len(core) <= 12 and sum(c in STRONG_MATH for c in core) / len(core) >= 0.5


def _math_alpha(ch, style):
    """Unicode mathematical alphanumeric for one ASCII letter."""
    if not ch.isascii() or not ch.isalpha():
        return ch
    up = ch.isupper()
    off = ord(ch) - (65 if up else 97)
    if style == "bold":
        return chr((0x1D400 if up else 0x1D41A) + off)
    if style == "cal" and up:
        return SCRIPT_CAPS.get(ch, chr(0x1D49C + off))
    if style == "bb" and up:
        return DOUBLE_CAPS.get(ch, chr(0x1D538 + off))
    if style == "frak":
        return chr((0x1D504 if up else 0x1D51E) + off)
    return ch


def span_math(t, font):
    """(latex, unicode) for one span, using the font name to recover the math alphabet."""
    core = t.strip()
    if not core:
        return t, t
    lead, trail = t[:len(t) - len(t.lstrip())], t[len(t.rstrip()):]
    style = None
    if re.match(r"CMSY|EUSM|rsfs", font, re.I) and re.fullmatch(r"[A-Z]+", core):
        style = "cal"
    elif re.match(r"MSBM", font, re.I) and re.fullmatch(r"[A-Z]+", core):
        style = "bb"
    elif re.match(r"EUFM", font, re.I):
        style = "frak"
    elif re.match(r"CMBX|CMMIB|CMBSY", font, re.I) or (re.search(r"bold", font, re.I) and re.search(r"math|stix", font, re.I)):
        style = "bold"
    cmd = {"cal": "\\mathcal", "bb": "\\mathbb", "frak": "\\mathfrak", "bold": "\\mathbf"}.get(style)
    if cmd and re.fullmatch(r"[A-Za-z]+", core):
        lx = f"{cmd}{{{core}}}"
        uni = "".join(_math_alpha(c, style) for c in core)
    else:
        lx = core.replace("{", "\\{").replace("}", "\\}") if re.match(r"CMSY", font, re.I) else core
        uni = core
    return lead + lx + trail, lead + uni + trail


def span_char_count(sp):
    return len(sp.get("text", "").strip())


def build_line(line):
    spans = [s for s in line.get("spans", []) if s.get("text", "") != ""]
    if not spans:
        return None
    # dominant span = most characters
    sizes = Counter()
    for s in spans:
        sizes[round(s["size"], 1)] += max(1, span_char_count(s))
    base = sizes.most_common(1)[0][0]
    dom = max(spans, key=lambda s: (round(s["size"], 1) == base, span_char_count(s)))
    baseline = dom["origin"][1]
    text_parts, marked_parts, latex_parts, umath_parts, tagged_parts = [], [], [], [], []
    bold_chars = italic_chars = total = math_chars = 0
    fonts = Counter()
    n_sup = n_sub = 0
    for s in spans:
        t = s["text"]
        text_parts.append(t)
        c = span_char_count(s)
        total += c
        fname = s.get("font", "")
        fonts[fname] += c
        flags = s.get("flags", 0)
        if flags & 16 or re.search(r"bold|black|heavy|semibold|\.B$|-B$|CMBX", fname, re.I):
            bold_chars += c
        if flags & 2 or re.search(r"italic|oblique|CMMI|CMTI", fname, re.I):
            italic_chars += c
        small = s["size"] < 0.85 * base
        dy = s["origin"][1] - baseline
        lx, um = span_math(t, fname)
        if MATH_FONT.match(fname):
            math_chars += c
        # literature-translator patch: wrap spans set in a math font (or made of math symbols)
        # in private-use sentinels so the translation layer can protect inline formulas.
        is_math = bool(STRICT_MATH_FONT.search(fname)) or _mostly_math_symbols(t)
        # "weak" math: CM roman/bold spans (digits, brackets, bold vectors) - math only next to a strong span
        is_weak = not is_math and bool(WEAK_MATH_FONT.match(fname))
        to, tc = (TAG_OPEN, TAG_CLOSE) if is_math else ((TAG_WOPEN, TAG_WCLOSE) if is_weak else ("", ""))
        if t.strip() and ((flags & 1 and small) or (small and dy < -0.12 * base)):
            marked_parts.append("^{" + t.strip() + "}")
            latex_parts.append("^{" + lx.strip() + "}")
            umath_parts.append("^{" + um.strip() + "}")
            tagged_parts.append(to + "^{" + t.strip() + "}" + tc)
            n_sup += 1
        elif t.strip() and small and dy > 0.08 * base:
            marked_parts.append("_{" + t.strip() + "}")
            latex_parts.append("_{" + lx.strip() + "}")
            umath_parts.append("_{" + um.strip() + "}")
            tagged_parts.append(to + "_{" + t.strip() + "}" + tc)
            n_sub += 1
        else:
            marked_parts.append(t)
            latex_parts.append(lx)
            umath_parts.append(um)
            if to and t.strip():
                lead_ws, trail_ws = t[:len(t) - len(t.lstrip())], t[len(t.rstrip()):]
                tagged_parts.append(lead_ws + to + t.strip() + tc + trail_ws)
            else:
                tagged_parts.append(t)
    d = line.get("dir", (1, 0))
    lead = next((s for s in spans if s["text"].strip()), spans[0])
    lead_bold = bool(lead.get("flags", 0) & 16 or re.search(r"bold|black|heavy|semibold|CMBX", lead.get("font", ""), re.I))
    return {
        "lead_bold": lead_bold,
        "text": "".join(text_parts),
        "marked": "".join(marked_parts),
        "tagged": "".join(tagged_parts),
        "latex": "".join(latex_parts),
        "umath": "".join(umath_parts),
        "math_chars": math_chars,
        "bbox": [round(v, 2) for v in line["bbox"]],
        "size": base,
        "bold": total > 0 and bold_chars / total > 0.6,
        "italic": total > 0 and italic_chars / total > 0.6,
        "fonts": [f for f, _ in fonts.most_common(3)],
        "horizontal": abs(d[0]) > 0.99 and abs(d[1]) < 0.01,
        "n_sup": n_sup,
        "n_sub": n_sub,
    }


def join_lines(lines, key="text"):
    """Join lines of a block, de-hyphenating words split at line ends.
    Returns (text, n_dehyphenated)."""
    out = ""
    n = 0
    for i, ln in enumerate(lines):
        t = ln[key].strip()
        if not t:
            continue
        if not out:
            out = t
            continue
        if re.search(r"[A-Za-z]{2,}-$", out) and re.match(r"^[a-z]", t) and not re.match(r"^[a-z]+-", t):
            out = out[:-1] + t
            n += 1
        elif re.search(r"[一-鿿]$", out) and re.match(r"^[一-鿿]", t):
            out = out + t  # CJK: no space between wrapped lines
        else:
            out = out + " " + t
    return out, n


def heading_like(text):
    t = norm_ws(text)
    if not t or len(t) > 130:
        return None
    for rx, kind in ((RE_HEAD_NUM, "numeric"), (RE_HEAD_ROMAN, "roman"), (RE_HEAD_ALPHA, "alpha"), (RE_HEAD_CN, "cn"), (RE_HEAD_APPX, "appendix")):
        m = rx.match(t)
        if m:
            if kind in ("appendix", "named", "numeric") and re.search(r"(\.\s*){3,}\s*\d+$|\s\d{1,3}$", t) and \
                    not re.search(r"\b(19|20)\d\d$", t):
                return None  # table-of-contents entry ("A Details ... 43")
            return kind, m.group(1), m.group(2)
    if KNOWN_SECTIONS.match(t):
        return "named", "", t
    return None


def make_block(page_no, lines, source="text_layer", conf=None):
    x0 = min(l["bbox"][0] for l in lines)
    y0 = min(l["bbox"][1] for l in lines)
    x1 = max(l["bbox"][2] for l in lines)
    y1 = max(l["bbox"][3] for l in lines)
    text, n_dehy = join_lines(lines, "text")
    marked, _ = join_lines(lines, "marked")
    tagged = join_lines(lines, "tagged")[0] if any(TAG_OPEN in l.get("tagged", "") for l in lines) else None
    has_math = any(l.get("math_chars", 0) for l in lines)
    latex = join_lines(lines, "latex")[0] if has_math else None
    umath = join_lines(lines, "umath")[0] if has_math else None
    sizes = [l["size"] for l in lines]
    return {
        "id": None,
        "page": page_no,
        "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
        "lines": lines,
        "text": text,
        "marked": marked if marked != text else None,
        "tagged": tagged,
        "latex": latex,
        "unicode_math": umath,
        "math_fonts": sorted({f for l in lines for f in l["fonts"] if MATH_FONT.match(f)})[:6] if has_math else None,
        "size": round(statistics.median(sizes), 2),
        "bold": all(l["bold"] for l in lines),
        "fonts": sorted({f for l in lines for f in l["fonts"]})[:4],
        "horizontal": all(l["horizontal"] for l in lines),
        "n_dehyphenated": n_dehy,
        "source": source,
        "ocr_conf": conf,
        "role": "body",
        "role_rule": "default",
        "column": None,
        "order": None,
    }


def split_raw_block(page_no, raw_lines):
    """Split a MuPDF block where a heading / caption line is glued to body text,
    or where a large vertical gap separates paragraphs."""
    groups, cur = [], []
    prev = None
    for ln in raw_lines:
        start_new = False
        if prev is not None:
            gap = ln["bbox"][1] - prev["bbox"][3]
            lh = max(1.0, prev["bbox"][3] - prev["bbox"][1])
            if gap > 0.9 * lh:
                start_new = True
            elif (heading_like(prev["text"]) and (prev["bold"] != ln["bold"] or abs(prev["size"] - ln["size"]) > 0.5)):
                start_new = True
            elif RE_CAPTION.match(ln["text"]) and not RE_CAPTION.match(cur[0]["text"]) if cur else False:
                start_new = True
            elif abs(prev["size"] - ln["size"]) > 1.5:
                start_new = True
        if start_new and cur:
            groups.append(cur)
            cur = []
        cur.append(ln)
        prev = ln
    if cur:
        groups.append(cur)
    return [make_block(page_no, g) for g in groups]


# TeX math-extension fonts (cmex / txex / pxex / ntxexx / esint ...) use a fixed glyph table; their codes reach the
# text layer as ASCII letters or control characters (txex 'P' = text-size sum, 'X' = display sum, 0x10/0x11 = big ()).
EXT_FONT = re.compile(r"(?:^|\+)(?:cm|tx|px|ntx|lm|rtx|rntx|zx|nx)?(?:ex|exx|ex\d+)(?:$|[-,])|^esint|Extension|^MTEX", re.I)
CMEX = {}
for _code, _chars in ((0x00, "()[]⌊⌋⌈⌉{}⟨⟩|‖/\\"), (0x10, "()()[]⌊⌋⌈⌉{}⟨⟩/\\"), (0x21, ")[]⌊⌋⌈⌉{}⟨⟩/\\/\\"),
                      (0x30, "⎛⎞⎡⎤⎣⎦⎢⎥⎧⎫⎩⎭⎨⎬⎪⏐"), (0x40, "⎝⎠⎜⎟⟨⟩⨆⨆∮∮⨀⨀⨁⨁⨂⨂"), (0x50, "∑∏∫⋃⋂⨄⋀⋁∑∏∫⋃⋂⨄⋀⋁"),
                      (0x60, "∐∐^^^~~~[]⌊⌋⌈⌉{}"), (0x70, "√√√√√")):
    for _i, _ch in enumerate(_chars):
        CMEX[_code + _i] = _ch
SYM_FONT = re.compile(r"txsy|cmsy|msam|msbm|^Symbol|MTSY|stmary|wasy", re.I)
BIG_OPS = set("∑∏∫∮⋃⋂∐⨁⨂⨀⨄⋀⋁⨆")


def _decode_span(s):
    chars = s.get("chars")
    if chars is None:
        return s.get("text", "")
    if EXT_FONT.search(s.get("font", "")):
        return "".join(CMEX.get(ord(ch["c"]), ch["c"]) if ch["c"] != " " else " " for ch in chars)
    if SYM_FONT.search(s.get("font", "")):
        size = s.get("size", 10) or 10
        return "".join("\ufffd" if ch["c"] in ",.;:" and (ch["bbox"][2] - ch["bbox"][0]) > 0.45 * size else ch["c"] for ch in chars)
    return "".join("\ufffd" if ord(ch["c"]) < 0x20 and ch["c"] not in "\t\n\r" else ch["c"] for ch in chars)


def _raw_lines(page):
    """All non-empty raw MuPDF lines of a page as (block_index, line_dict); span text rebuilt from rawdict characters so
    math-extension glyph codes can be decoded instead of becoming U+FFFD."""
    flags = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP | getattr(fitz, "TEXT_CID_FOR_UNKNOWN_UNICODE", 128)
    d = page.get_text("rawdict", flags=flags)
    out = []
    for bi, b in enumerate(d.get("blocks", [])):
        if b.get("type", 0) != 0:
            continue
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                s["text"] = _decode_span(s)
                s.pop("chars", None)
            if "".join(s.get("text", "") for s in l.get("spans", [])).strip():
                out.append((bi, l))
    return out


def _line_text(l):
    return "".join(s.get("text", "") for s in l.get("spans", []))


def _in_box(bb, box, pad=1.5):
    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    return box[0] - pad <= cx <= box[2] + pad and box[1] - pad <= cy <= box[3] + pad


def extract_text_blocks(page, page_no, exclude_boxes=None, raw=None):
    """Blocks of running text. Lines inside `exclude_boxes` are kept (tables are re-labelled later);
    display-math regions are cut out of their MuPDF blocks and returned as one equation block each."""
    raw = raw if raw is not None else _raw_lines(page)
    eq_blocks, used = display_math_blocks(page, page_no, raw, exclude_boxes or [])
    blocks = []
    by_block = defaultdict(list)
    for k, (bi, l) in enumerate(raw):
        if k not in used:
            by_block[bi].append(l)
    for bi in sorted(by_block):
        lines = [build_line(l) for l in by_block[bi]]
        lines = [l for l in lines if l and l["text"].strip()]
        if lines:
            blocks.extend(split_raw_block(page_no, lines))
    return blocks + eq_blocks


# ----------------------------------------------------------------- display math (literature-translator 0.2)
RE_EQNUM_LINE = re.compile(r"^[\s\.,;:]*\(\s*((?:[A-Z]\.?)?\d+(?:\.\d+)?[a-z]?)\s*\)\s*$")
WEAK_MATH_FONT2 = re.compile(r"^(CMR|CMBX|CMSL|CMSS|CMB\d|LMRoman)|^r?txr|^r?ntxr|txex|CMEX|^MTEX", re.I)


def _span_is_math(s):
    f = s.get("font", "")
    t = s.get("text", "")
    return bool(STRICT_MATH_FONT.search(f) or WEAK_MATH_FONT2.search(f)) or any(c in STRONG_MATH or c == "�" for c in t)


def _line_math_stats(l):
    tot = mth = 0
    for s in l["spans"]:
        n = len(s.get("text", "").replace(" ", ""))
        tot += n
        if _span_is_math(s):
            mth += n
    return tot, mth


def _prose_words(l):
    """Words of >=3 lowercase letters set in a text (non-math) font."""
    n = 0
    for s in l["spans"]:
        if not _span_is_math(s):
            n += sum(1 for w in re.findall(r"\b[a-z]{3,}\b", s.get("text", "")) if w not in MATH_WORDS)
    return n


MATH_WORDS = {"log", "exp", "max", "min", "sin", "cos", "tan", "lim", "sup", "inf", "det", "arg", "mod", "var", "cov", "diag",
              "argmax", "argmin", "softmax", "sgn", "sign", "tanh", "relu"}


def display_math_blocks(page, page_no, raw, exclude_boxes):
    """Group raw lines into display-equation regions (same column, vertically adjacent, math-heavy or
    equation-number lines) and linearise each region from its spans and fraction bars."""
    W = page.rect.width
    sizes = Counter()
    for _, l in raw:
        for s in l["spans"]:
            sizes[round(s["size"], 1)] += len(s.get("text", ""))
    body = sizes.most_common(1)[0][0] if sizes else 10.0
    # figure graphics (raster images, clusters of vector drawings): labels inside them are not display equations
    fig_boxes = []
    try:
        fig_boxes += [list(im["bbox"]) for im in page.get_image_info() if (im["bbox"][2] - im["bbox"][0]) > 40 and (im["bbox"][3] - im["bbox"][1]) > 30]
    except Exception:
        pass
    try:
        if hasattr(page, "cluster_drawings"):
            fig_boxes += [[r.x0, r.y0, r.x1, r.y1] for r in page.cluster_drawings() if r.width > 40 and r.height > 30]
    except Exception:
        pass
    def side_of(bb):
        return 0 if bb[2] <= W / 2 + 8 else (1 if bb[0] >= W / 2 - 8 else 2)

    def row_prose(k, bb, side):
        """prose words on the same text row (same column, vertical overlap > 50%), excluding the line itself"""
        h = max(1.0, bb[3] - bb[1])
        n = 0
        for k2, (_, l2) in enumerate(raw):
            if k2 == k:
                continue
            b2 = l2["bbox"]
            if side_of(b2) != side and 2 not in (side, side_of(b2)):
                continue
            ov = min(bb[3], b2[3]) - max(bb[1], b2[1])
            if ov > 0.5 * min(h, max(1.0, b2[3] - b2[1])):
                n += _prose_words(l2)
        return n
    block_prose = Counter()
    for bi_, l_ in raw:
        block_prose[bi_] += _prose_words(l_)
    cand = []
    for k, (bi, l) in enumerate(raw):
        bb = l["bbox"]
        if any(_in_box(bb, box) for box in exclude_boxes) or any(_in_box(bb, box, pad=0) for box in fig_boxes):
            continue
        d = l.get("dir", (1, 0))
        if abs(d[0]) < 0.99:
            continue
        txt = _line_text(l).strip()
        tot, mth = _line_math_stats(l)
        side = 0 if bb[2] <= W / 2 + 8 else (1 if bb[0] >= W / 2 - 8 else 2)
        colw = (W / 2 - 60) if side in (0, 1) else (W - 120)
        num = RE_EQNUM_LINE.match(txt)
        prose = _prose_words(l)
        kind = None
        if num:
            kind = "num"
        elif tot and mth / tot >= 0.35 and prose <= 2 and (bb[2] - bb[0]) < 0.92 * colw:
            kind = "strong"
        elif tot and tot <= 4 and prose == 0 and not re.search(r"[\.!?]$", txt):
            kind = "weak"
        elif tot and mth / tot >= 0.15 and prose <= 2 and (bb[2] - bb[0]) < 0.6 * colw and not SENT_END.search(txt):
            kind = "weak"
        if (kind == "weak" and (txt.endswith(":") or block_prose.get(bi, 0) >= 3)) or                 (kind == "strong" and txt.endswith(":") and block_prose.get(bi, 0) >= 3):
            kind = None  # "...frequency rank of that word pair, r_ij:" = last line of a paragraph
        if kind in ("strong", "weak") and row_prose(k, bb, side) + prose >= 2:
            kind = None  # e.g. "Recalling our notation for X_i = ∑_k X_ik and": inline math inside a text line
        if kind:
            cand.append({"k": k, "bb": bb, "side": side, "kind": kind, "l": l})
    # cluster: lines on the same side, vertically adjacent or overlapping
    cand.sort(key=lambda c: (c["side"], c["bb"][1]))
    clusters = []
    for c in cand:
        placed = False
        for cl in clusters:
            if cl["side"] != c["side"] and 2 not in (cl["side"], c["side"]):
                continue
            y0, y1 = cl["y0"], cl["y1"]
            if c["bb"][1] <= y1 + 0.9 * body and c["bb"][3] >= y0 - 0.9 * body:
                cl["items"].append(c)
                cl["y0"], cl["y1"] = min(y0, c["bb"][1]), max(y1, c["bb"][3])
                placed = True
                break
        if not placed:
            clusters.append({"side": c["side"], "items": [c], "y0": c["bb"][1], "y1": c["bb"][3]})
    out, used = [], set()
    for cl in clusters:
        kinds = [c["kind"] for c in cl["items"]]
        if "strong" not in kinds and not ("num" in kinds and len(kinds) >= 2):
            continue
        if kinds == ["num"]:
            continue
        core = "".join(_line_text(c["l"]) for c in cl["items"])
        if len(re.sub(r"[\s�]", "", core)) < 3 and "num" not in kinds:
            continue  # a lone big-operator glyph belongs to the inline formula around it
        # absorb short text lines lying inside the region's vertical band (e.g. "if x < xmax", "otherwise")
        x0 = min(c["bb"][0] for c in cl["items"])
        x1 = max(c["bb"][2] for c in cl["items"])
        ids = {c["k"] for c in cl["items"]}
        for k, (bi, l) in enumerate(raw):
            if k in ids:
                continue
            bb = l["bbox"]
            cy = (bb[1] + bb[3]) / 2
            if cl["y0"] - 1 <= cy <= cl["y1"] + 1 and bb[0] >= x0 - 40 and bb[2] <= x1 + 40 and _prose_words(l) <= 2 \
                    and len(_line_text(l).strip()) <= 30 and (side_of(bb) == cl["side"] or 2 in (cl["side"], side_of(bb))):
                ids.add(k)
        lines = [raw[k][1] for k in sorted(ids)]
        blk = _equation_block(page, page_no, lines, body)
        if blk:
            out.append(blk)
            used |= ids
    return out, used


def _fraction_bars(page, box):
    bars = []
    try:
        for p in page.get_drawings():
            for it in p.get("items", []):
                if it[0] == "l":
                    a, b = it[1], it[2]
                    if abs(a.y - b.y) < 1.0 and abs(a.x - b.x) > 3:
                        bars.append([min(a.x, b.x), (a.y + b.y) / 2, max(a.x, b.x)])
                elif it[0] == "re":
                    r = it[1]
                    if r.height < 1.6 and r.width > 3:
                        bars.append([r.x0, (r.y0 + r.y1) / 2, r.x1])
    except Exception:
        return []
    return [b for b in bars if box[0] - 2 <= b[0] and b[2] <= box[2] + 2 and box[1] - 2 <= b[1] <= box[3] + 2]


def _linearise(spans, main):
    """Order spans left to right on their row(s); mark small raised/lowered spans as ^{}/_{}."""
    if not spans:
        return "", 0
    mains = [s for s in spans if s["size"] >= 0.85 * main and s["text"].strip()]
    rows = []
    for s in sorted(mains, key=lambda s: s["cy"]):
        for r in rows:
            if abs(r["cy"] - s["cy"]) <= 0.5 * main:
                r["items"].append(s)
                r["cy"] = sum(x["cy"] for x in r["items"]) / len(r["items"])
                break
        else:
            rows.append({"cy": s["cy"], "items": [s]})
    if not rows:
        rows = [{"cy": spans[0]["cy"], "items": []}]
    for s in spans:
        if s["size"] >= 0.85 * main and s["text"].strip():
            continue
        r = min(rows, key=lambda r: abs(r["cy"] - s["cy"]))
        r["items"].append(s)
    texts = []
    for r in sorted(rows, key=lambda r: r["cy"]):
        base = [x for x in r["items"] if x["size"] >= 0.85 * main and not x.get("frac")]
        bl = statistics.median([x["oy"] for x in base]) if base else r["cy"]
        parts, last_x1 = [], None
        for s in sorted(r["items"], key=lambda s: (s["x0"], s["cy"])):
            t = s["text"]
            if not t.strip():
                continue
            if not s.get("frac") and s["size"] < 0.85 * main:
                if s["oy"] < bl - 0.15 * main:
                    t = "^{" + t.strip() + "}"
                elif s["oy"] > bl + 0.05 * main:
                    t = "_{" + t.strip() + "}"
            sep = " " if last_x1 is not None and s["x0"] - last_x1 > 0.18 * main else ""
            parts.append(sep + t)
            last_x1 = s["x1"]
        texts.append("".join(parts).strip())
    txt = " ⏎ ".join(t for t in texts if t)
    for _ in range(6):
        txt2 = re.sub(r"([_^])\{([^{}]*)\}\1\{", r"\1{\2", txt)
        if txt2 == txt:
            break
        txt = txt2
    txt = re.sub(r"˜\s*([A-Za-z])", lambda m: m.group(1) + "̃", txt)
    return re.sub(r"[ \t]+", " ", txt), len(rows)


BIG_DELIMS = set("()[]{}⟨⟩|‖⌊⌋⌈⌉⎛⎞⎝⎠⎜⎟⎡⎤⎣⎦⎢⎥⎧⎫⎩⎭⎨⎬⎪/\\")


def _attach_limits(spans, msize):
    """Big operators (∑ ∏ ∫ ...) and big delimiters from math-extension fonts join the main text row they belong to
    (their glyphs hang below the PDF origin); small spans directly above / below an operator become ^{..} / _{..}."""
    mains = [s for s in spans if "y0" in s and s["size"] >= 0.85 * msize and not set(s["text"].strip()) <= BIG_OPS
             and not EXT_FONT.search(s.get("font", "")) and s["text"].strip()]
    if not mains:
        return
    # baselines of the text rows (aligned / multi-line equations have several)
    bls = []
    for b in sorted(x["oy"] for x in mains):
        if bls and abs(b - bls[-1][-1]) <= 0.4 * msize:
            bls[-1].append(b)
        else:
            bls.append([b])
    bls = [statistics.median(g) for g in bls]

    def row_of(sp):
        vis = sp["oy"] + 0.6 * msize  # visual centre of a glyph hanging below its origin
        return min(bls, key=lambda b: abs(b - vis))
    for d in [s for s in spans if not s.get("used") and EXT_FONT.search(s.get("font", "")) and s["text"].strip()
              and set(s["text"].strip()) <= BIG_DELIMS and "y0" in s]:
        b = row_of(d)
        d.update(cy=b - 0.3 * msize, oy=b, size=msize)
    for op in [s for s in spans if not s.get("used") and s["text"].strip() and set(s["text"].strip()) <= BIG_OPS and "y0" in s]:
        bl = row_of(op)
        cx = (op["x0"] + op["x1"]) / 2
        half = max(5.0, (op["x1"] - op["x0"]) * 0.75)
        small = [s for s in spans if s is not op and not s.get("used") and "y0" in s and s["size"] < 0.9 * msize]
        near = [s for s in small if abs((s["x0"] + s["x1"]) / 2 - cx) <= half]
        above = [s for s in near if s["y1"] <= bl - 0.55 * msize and s["y1"] >= bl - 2.6 * msize]
        below = [s for s in near if s["y0"] >= bl + 0.05 * msize and s["y0"] <= bl + 1.6 * msize]
        for grp in (above, below):  # limits wider than the operator ("i∈corpus") continue on the same baseline
            grew = True
            while grew and grp:
                grew = False
                for s in small:
                    if s in grp or s in above or s in below:
                        continue
                    if any(abs(s["oy"] - g["oy"]) < 1.0 and (abs(s["x0"] - g["x1"]) < 3.5 or abs(g["x0"] - s["x1"]) < 3.5) for g in grp):
                        grp.append(s)
                        grew = True
        def stack(grp):  # several limit lines (i∈corpus / j∈context(i)) -> "i∈corpus, j∈context(i)"
            lines_ = []
            for s in sorted(grp, key=lambda s: s["oy"]):
                if lines_ and abs(s["oy"] - lines_[-1][0]["oy"]) < 1.0:
                    lines_[-1].append(s)
                else:
                    lines_.append([s])
            return ", ".join("".join(x["text"].strip() for x in sorted(ln, key=lambda s: s["x0"])) for ln in lines_)
        t_ = op["text"].strip()
        if below:
            t_ += "_{" + stack(below) + "}"
        if above:
            t_ += "^{" + stack(above) + "}"
        for s in above + below:
            s["used"] = True
        op.update(text=t_, cy=bl - 0.3 * msize, oy=bl, size=msize, limits=bool(above or below))


def _equation_block(page, page_no, raw_lines, body):
    spans = []
    num = None
    for l in raw_lines:
        lt = _line_text(l).strip()
        m = RE_EQNUM_LINE.match(lt)
        if m:
            num = m.group(1)
            pre = re.sub(r"\(\s*" + re.escape(m.group(1)) + r"\s*\)\s*$", "", lt).strip()
            if pre:
                spans.append({"text": pre, "size": body, "x0": l["bbox"][0], "x1": l["bbox"][0] + 3, "cy": (l["bbox"][1] + l["bbox"][3]) / 2,
                              "oy": l["bbox"][3] - 2, "font": "punct"})
            continue
        for s in l["spans"]:
            if not s.get("text", ""):
                continue
            bb = s["bbox"]
            spans.append({"text": s["text"], "size": s["size"], "x0": bb[0], "x1": bb[2], "cy": (bb[1] + bb[3]) / 2,
                          "oy": s["origin"][1], "y0": bb[1], "y1": bb[3], "font": s.get("font", "")})
    if not spans:
        return None
    x0 = min(l["bbox"][0] for l in raw_lines)
    y0 = min(l["bbox"][1] for l in raw_lines)
    x1 = max(l["bbox"][2] for l in raw_lines)
    y1 = max(l["bbox"][3] for l in raw_lines)
    main = Counter()
    for s in spans:
        main[round(s["size"], 1)] += len(s["text"].strip())
    tot_chars = max(1, sum(main.values()))
    # the main size is the LARGEST size carrying a fair share of the characters (limits and indices can outnumber it)
    msize = next((sz for sz, n in sorted(main.items(), key=lambda kv: -kv[0]) if n >= 0.2 * tot_chars),
                 max(main.items(), key=lambda kv: (kv[1], kv[0]))[0]) if main else body
    notes = []
    bars = _fraction_bars(page, [x0, y0, x1, y1])
    n_frac_ok = 0
    for bx0, by, bx1 in sorted(bars, key=lambda b: b[2] - b[0]):
        inx = [s for s in spans if not s.get("used") and "y0" in s and bx0 - 1.5 <= (s["x0"] + s["x1"]) / 2 <= bx1 + 1.5]
        ops = [s for s in inx if set(s["text"].strip()) <= BIG_OPS and s["text"].strip()]
        rest = [s for s in inx if s not in ops]
        numr = [s for s in rest if s["y1"] <= by + 1.8 and s["y1"] >= by - 2.4 * msize] + [s for s in ops if by - 2.4 * msize <= s["oy"] < by]
        den = [s for s in rest if s["y0"] >= by - 1.8 and s["y0"] <= by + 2.4 * msize] + [s for s in ops if by <= s["oy"] <= by + 2.6 * msize]
        if not numr or not den:
            notes.append(f"fraction bar at x={bx0:.0f}-{bx1:.0f} without numerator/denominator")
            continue
        _attach_limits(numr, msize)  # marks consumed limit spans as used
        _attach_limits(den, msize)
        numr = [s for s in numr if not s.get("used")]
        den = [s for s in den if not s.get("used")]
        for s in numr + den:
            s["used"] = True
        nt, _ = _linearise(numr, msize)
        dt, _ = _linearise(den, msize)
        wrap = lambda t: t if re.fullmatch(r"[\wͰ-Ͽ̃{}^_]+", t) else "(" + t + ")"
        spans.append({"text": wrap(nt) + "/" + wrap(dt), "size": msize, "x0": bx0, "x1": bx1, "cy": by, "oy": by + 0.3 * msize,
                      "frac": True, "font": "fraction"})
        n_frac_ok += 1
    _attach_limits([s for s in spans if not s.get("used")], msize)
    text, n_rows = _linearise([s for s in spans if not s.get("used")], msize)
    if not text.strip():
        return None
    if "�" in text:
        notes.append("unmapped glyph(s) shown as � (typically a large operator such as ∑, ∏ or ∫, or a big bracket)")
        text = text.replace("�", "⟨?⟩")
    if n_rows > 1:
        notes.append(f"{n_rows} stacked rows (limits, cases or aligned lines) linearised top-to-bottom with ⏎")
    confident = not notes
    lines_b = [build_line(l) for l in raw_lines]
    lines_b = [l for l in lines_b if l]
    blk = make_block(page_no, lines_b)
    blk.update({"bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)], "text": text + (f" ({num})" if num else ""),
                "marked": None, "tagged": None, "latex": None, "unicode_math": text, "role": "equation",
                "role_rule": "display-math region (line level: math fonts + equation number + vertical adjacency)",
                "equation_number": num, "eq_linear": text, "eq_fractions": n_frac_ok, "eq_confident": confident, "eq_notes": notes})
    return blk


# ----------------------------------------------------------------- caption-anchored tables (literature-translator 0.2)
RE_TABLE_CAP = re.compile(r"^\s*(Table|TABLE|Tab\.)\s*(S?\d+|[IVX]+)\s*[\.:|]", re.I)
RE_NUMCELL = re.compile(r"^[\(\[]?[-−+±~<>≈]?\d[\d,\.]*\s*(?:[×x]\s*10(?:\^\{[^}]*\}|[−\-]?\d+))?\s*[%\)\]\*†‡]*$|^[-–—]$")


def merge_scripts(s):
    """10^{−}^{4} -> 10^{−4}; h_{t}_{−}_{1} -> h_{t−1}."""
    for _ in range(8):
        s2 = re.sub(r"([_^])\{([^{}]*)\}\1\{", r"\1{\2", s)
        if s2 == s:
            return s
        s = s2
    return s


def _row_bands(lines):
    rows = []
    for l in sorted(lines, key=lambda l: (l["bbox"][1] + l["bbox"][3]) / 2):
        cy = (l["bbox"][1] + l["bbox"][3]) / 2
        h = l["bbox"][3] - l["bbox"][1]
        for r in rows:
            if abs(r["cy"] - cy) <= 0.45 * max(h, r["h"]):
                r["lines"].append(l)
                break
        else:
            rows.append({"cy": cy, "h": h, "lines": [l]})
    for r in rows:
        r["lines"].sort(key=lambda l: l["bbox"][0])
    return rows


def caption_line_tables(page, page_no, raw):
    """Rebuild tables from MuPDF line boxes below/above a 'Table N' caption: one line per cell, rows by
    vertical band, columns by x-interval union over numeric rows. Returns table dicts (rows, cells, bbox)."""
    out = []
    by_block = defaultdict(list)
    for bi, l in raw:
        by_block[bi].append(l)
    for bi, lines in by_block.items():
        first = _line_text(lines[0]).strip()
        if not RE_TABLE_CAP.match(first):
            continue
        cap = [min(l["bbox"][0] for l in lines), min(l["bbox"][1] for l in lines), max(l["bbox"][2] for l in lines),
               max(l["bbox"][3] for l in lines)]
        others = [l for b2, l in raw if b2 != bi and l["bbox"][0] >= cap[0] - 30 and l["bbox"][2] <= cap[2] + 30
                  and abs(l.get("dir", (1, 0))[0]) > 0.99]
        best = None
        for direction in ("below", "above"):
            if direction == "below":
                cand = [l for l in others if l["bbox"][1] >= cap[3] - 1]
            else:
                cand = [l for l in others if l["bbox"][3] <= cap[1] + 1]
            bands = _row_bands(cand)
            if direction == "above":
                bands = bands[::-1]
            rows, last = [], cap
            for r in bands:
                ry0 = min(l["bbox"][1] for l in r["lines"])
                ry1 = max(l["bbox"][3] for l in r["lines"])
                gap = ry0 - last[3] if direction == "below" else last[1] - ry1
                if gap > 2.6 * r["h"]:
                    break
                l0 = r["lines"][0]
                single_prose = len(r["lines"]) == 1 and (
                    ((l0["bbox"][2] - l0["bbox"][0]) > 0.55 * (cap[2] - cap[0]) and _prose_words(l0) >= 5)
                    or _prose_words(l0) >= 4 or bool(re.search(r"[A-Za-z][\.!?:]$", _line_text(l0).strip()))
                    or (sum(1 for x in rows if len(x["lines"]) >= 2) >= 2
                        and l0["bbox"][0] < min(x["lines"][0]["bbox"][0] for x in rows) - 10))
                prose_band = any(_prose_words(l) >= 4 for l in r["lines"]) or \
                    (len(r["lines"]) <= 2 and any(re.search(r"[a-z][\.!?]$", _line_text(l).strip()) for l in r["lines"]))
                gaps = [b["y0"] - a["y1"] for a, b in zip(rows, rows[1:])] if direction == "below" else \
                    [a["y0"] - b["y1"] for a, b in zip(rows, rows[1:])]
                big_gap = len(rows) >= 2 and gaps and gap > max(2.5 * statistics.median(gaps), 1.2 * r["h"])
                if single_prose or prose_band or big_gap or RE_CAPTION.match(_line_text(r["lines"][0]).strip()):
                    break
                r["y0"], r["y1"] = ry0, ry1
                rows.append(r)
                last = [0, ry0, 0, ry1]
            if direction == "above":
                rows = rows[::-1]
            multi = sum(1 for r in rows if len(r["lines"]) >= 2)
            if len(rows) >= 2 and multi >= 2 and (best is None or multi > best[1]):
                best = (rows, multi, direction)
        if not best:
            continue
        rows = best[0]
        cell_txt = lambda l: merge_scripts(norm_ws((build_line(l) or {}).get("marked") or _line_text(l)))
        numeric_rows = [r for r in rows if sum(bool(RE_NUMCELL.match(cell_txt(l))) for l in r["lines"]) >= max(1, len(r["lines"]) // 2)]
        basis = numeric_rows if len(numeric_rows) >= 1 else rows
        iv = sorted([l["bbox"][0], l["bbox"][2]] for r in basis for l in r["lines"])
        cols = []
        for a, b in iv:
            if cols and a <= cols[-1][1] + 2:
                cols[-1][1] = max(cols[-1][1], b)
            else:
                cols.append([a, b])
        if len(cols) < 2:
            continue
        grid = []
        for r in rows:
            row = [None] * len(cols)
            for l in r["lines"]:
                ov = [max(0, min(l["bbox"][2], c[1]) - max(l["bbox"][0], c[0])) for c in cols]
                hit = [i for i, o in enumerate(ov) if o > 0.5]
                if not hit:
                    cx = (l["bbox"][0] + l["bbox"][2]) / 2
                    hit = [min(range(len(cols)), key=lambda i: min(abs(cx - cols[i][0]), abs(cx - cols[i][1])))]
                i = hit[0]
                row[i] = (row[i] + " " + cell_txt(l)) if row[i] else cell_txt(l)
                for j in hit[1:]:
                    row[j] = row[j] if row[j] is not None else ""  # spanned by the same text
            grid.append(row)
        filled = sum(1 for r in grid for c in r if c)
        if filled < 0.5 * len(grid) * len(cols):
            continue
        tb = [min(l["bbox"][0] for r in rows for l in r["lines"]), min(l["bbox"][1] for r in rows for l in r["lines"]),
              max(l["bbox"][2] for r in rows for l in r["lines"]), max(l["bbox"][3] for r in rows for l in r["lines"])]
        cells = [{"row": ri, "col": ci, "text": c, "colspan": 1, "rowspan": 1} for ri, r in enumerate(grid) for ci, c in enumerate(r) if c]
        out.append({"id": None, "page": page_no, "bbox": [round(v, 2) for v in tb], "n_rows": len(grid), "n_cols": len(cols),
                    "rows": [[c if c else None for c in r] for r in grid], "cells": cells, "header": None,
                    "caption": norm_ws(" ".join(_line_text(l) for l in lines)), "caption_block": None, "continued_from": None,
                    "method": f"caption-anchored line grid ({best[2]} caption; columns from {'numeric rows' if numeric_rows else 'all rows'})",
                    "span_inference": "one MuPDF line = one cell; empty cells left empty"})
    return out


# ----------------------------------------------------------------- OCR
def tesseract_langs():
    exe = shutil.which("tesseract")
    if not exe:
        return None, []
    try:
        out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=30)
        langs = [l.strip() for l in (out.stdout + out.stderr).splitlines()[1:] if l.strip() and " " not in l.strip()]
        return exe, langs
    except Exception:
        return exe, []


def ocr_page(page, page_no, workdir, lang_pref, dpi=300):
    """OCR one page with the tesseract CLI. Returns (blocks, info dict)."""
    exe, langs = tesseract_langs()
    if not exe:
        return [], {"ocr": "unavailable", "reason": "tesseract executable not found on PATH"}
    wanted = [l for l in lang_pref.split("+") if l in langs] or (["eng"] if "eng" in langs else langs[:1])
    lang = "+".join(wanted) if wanted else "eng"
    os.makedirs(workdir, exist_ok=True)
    img = os.path.join(workdir, f"ocr_p{page_no}.png")
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    pix.save(img)
    try:
        res = subprocess.run([exe, img, "stdout", "-l", lang, "--psm", "3", "tsv"],
                             capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
    except Exception as e:
        return [], {"ocr": "failed", "reason": str(e)}
    scale = 72.0 / dpi
    groups = defaultdict(list)
    low_conf = []
    for row in res.stdout.splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) < 12 or parts[0] != "5":
            continue
        text = parts[11]
        if not text.strip():
            continue
        try:
            conf = float(parts[10])
        except ValueError:
            conf = -1
        key = (int(parts[2]), int(parts[3]), int(parts[4]))
        l, t, w, h = (int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9]))
        groups[key].append((l, t, w, h, text, conf))
        if 0 <= conf < 60:
            low_conf.append({"word": text, "conf": conf, "bbox": [round(l * scale, 1), round(t * scale, 1), round((l + w) * scale, 1), round((t + h) * scale, 1)]})
    by_par = defaultdict(list)
    for (blk, par, ln), words in sorted(groups.items()):
        words.sort(key=lambda w: w[0])
        x0 = min(w[0] for w in words) * scale
        y0 = min(w[1] for w in words) * scale
        x1 = max(w[0] + w[2] for w in words) * scale
        y1 = max(w[1] + w[3] for w in words) * scale
        txt = " ".join(w[4] for w in words)
        confs = [w[5] for w in words if w[5] >= 0]
        by_par[(blk, par)].append({
            "text": txt, "marked": txt, "latex": txt, "umath": txt, "math_chars": 0, "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
            "size": round((y1 - y0), 1), "bold": False, "lead_bold": False, "italic": False, "fonts": ["ocr"],
            "horizontal": True, "n_sup": 0, "n_sub": 0, "_conf": statistics.mean(confs) if confs else None,
        })
    blocks = []
    for key, lines in by_par.items():
        confs = [l["_conf"] for l in lines if l["_conf"] is not None]
        b = make_block(page_no, lines, source="ocr", conf=round(statistics.mean(confs), 1) if confs else None)
        blocks.append(b)
    return blocks, {"ocr": "done", "lang": lang, "dpi": dpi, "low_conf_words": low_conf[:200],
                    "n_low_conf_words": len(low_conf)}


def ink_mask(page, dpi=150, clip=None):
    """Boolean ink mask of a rendered page (None if numpy is unavailable)."""
    try:
        if os.environ.get("LT_NO_NUMPY"):
            raise ImportError("numpy disabled by LT_NO_NUMPY (degraded-path test)")
        import numpy as np
    except ImportError:
        return None, None
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, clip=clip)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, :pix.width]
    return arr < 160, pix


def xy_cut_regions(mask, scale, min_h_gap=10, min_v_gap=16, min_ink=30):
    """Recursive XY-cut on an ink mask; returns layout regions in PDF points
    (x0, y0, x1, y1, ink_ratio) in reading order (top-down, left-right)."""
    import numpy as np
    out = []

    def longest_gap(flags):
        best, start = (0, 0), None
        for i, f in enumerate(flags):
            if not f:
                if start is None:
                    start = i
            elif start is not None:
                if i - start > best[1] - best[0]:
                    best = (start, i)
                start = None
        return best  # interior gaps only (ends are trimmed before)

    def cut(x0, y0, x1, y1, depth):
        sub = mask[y0:y1, x0:x1]
        if sub.sum() < min_ink:
            return
        ys = np.where(sub.any(axis=1))[0]
        xs = np.where(sub.any(axis=0))[0]
        y0, y1, x0, x1 = y0 + ys[0], y0 + ys[-1] + 1, x0 + xs[0], x0 + xs[-1] + 1
        sub = mask[y0:y1, x0:x1]
        if depth < 14:
            vg = longest_gap(sub.any(axis=0))
            hg = longest_gap(sub.any(axis=1))
            vlen, hlen = vg[1] - vg[0], hg[1] - hg[0]
            if vlen >= min_v_gap and (vlen >= hlen or hlen < min_h_gap):
                m = x0 + (vg[0] + vg[1]) // 2
                cut(x0, y0, m, y1, depth + 1)
                cut(m, y0, x1, y1, depth + 1)
                return
            if hlen >= min_h_gap:
                m = y0 + (hg[0] + hg[1]) // 2
                cut(x0, y0, x1, m, depth + 1)
                cut(x0, m, x1, y1, depth + 1)
                return
        ink = float(sub.mean())
        out.append((round(x0 * scale, 1), round(y0 * scale, 1), round(x1 * scale, 1), round(y1 * scale, 1), round(ink, 4)))

    cut(0, 0, mask.shape[1], mask.shape[0], 0)
    return out


def page_image_and_regions(page, page_no, out_dir, dpi=150):
    """For pages without usable text: save the page image and layout regions
    so an agent with vision can read a region and cite it by coordinates."""
    os.makedirs(os.path.join(out_dir, "page_images"), exist_ok=True)
    rel = f"page_images/page_{page_no:03d}.png"
    mask, pix = ink_mask(page, dpi)
    if pix is None:
        pix = page.get_pixmap(dpi=dpi)
    pix.save(os.path.join(out_dir, rel))
    regions = []
    if mask is not None:
        for k, (x0, y0, x1, y1, ink) in enumerate(xy_cut_regions(mask, 72.0 / dpi)):
            regions.append({"id": f"p{page_no}-r{k + 1}", "page": page_no, "bbox": [float(x0), float(y0), float(x1), float(y1)],
                            "ink_ratio": float(ink), "granularity": "region"})
    else:
        r = page.rect
        regions.append({"id": f"p{page_no}-page", "page": page_no, "bbox": [0.0, 0.0, round(r.width, 1), round(r.height, 1)],
                        "granularity": "page",
                        "degraded": "numpy is not installed: layout regions cannot be computed, the whole page is one region "
                                    "(install numpy, or transcribe with page + box locators so each part keeps its own bbox)"})
    return rel, dpi, regions


def page_is_scanned(page, text_blocks):
    chars = sum(len(b["text"]) for b in text_blocks)
    area = page.rect.width * page.rect.height
    img_area = 0.0
    try:
        for info in page.get_image_info():
            r = fitz.Rect(info["bbox"]) & page.rect
            img_area += r.width * r.height
    except Exception:
        pass
    return chars < 40 and img_area > 0.3 * area, chars, img_area / area if area else 0


def garbled_ratio(text):
    if not text:
        return 0.0
    bad = sum(1 for c in text if c == "�" or 0xE000 <= ord(c) <= 0xF8FF)
    bad += 4 * len(re.findall(r"\(cid:\d+\)", text))
    return bad / max(1, len(text))


# ----------------------------------------------------------------- layout
def mark_figure_text(blocks, regions, W):
    """Text that belongs to a figure (tick labels, legends, rotated axis titles):
    inside or just around a graphic region, and small or rotated or short."""
    if not regions:
        return 0
    sizes = Counter()
    for b in blocks:
        for ln in b["lines"]:
            sizes[ln["size"]] += len(ln["text"])
    local_body = sizes.most_common(1)[0][0] if sizes else 10.0
    n = 0
    for b in blocks:
        eq_like = b["role"] == "equation" and str(b.get("role_rule", "")).startswith("display-math region") and not b.get("equation_number")
        if (b["role"] != "body" and not eq_like) or RE_CAPTION.match(b["text"]):
            continue
        x0, y0, x1, y1 = b["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        for r in regions:
            pad = 16
            if r[0] - pad <= cx <= r[2] + pad and r[1] - pad <= cy <= r[3] + pad:
                small = b["size"] < 0.85 * local_body
                if small or not b["horizontal"] or len(b["text"]) < 25 or eq_like:
                    b["role"] = "figure_text"
                    b["role_rule"] = ("rotated text next to a graphic (axis title)" if not b["horizontal"]
                                      else "small/short text inside a graphic region")
                    b["figure_region"] = [round(v, 1) for v in r]
                    n += 1
                break
    return n


def detect_headers_footers(pages_blocks, page_heights, body_size=None, page_widths=None):
    """Mark running headers/footers/page numbers and rotated margin text."""
    n_pages = len(pages_blocks)
    cand = defaultdict(set)
    for pi, blocks in enumerate(pages_blocks):
        H = page_heights[pi]
        for b in blocks:
            if b["bbox"][3] < 0.085 * H or b["bbox"][1] > 0.915 * H:
                cand[norm_key(b["text"])].add(pi)
    hf = []
    for pi, blocks in enumerate(pages_blocks):
        H = page_heights[pi]
        for b in blocks:
            top = b["bbox"][3] < 0.085 * H
            bottom = b["bbox"][1] > 0.915 * H
            if b["role"] == "figure_text":
                continue
            if not b["horizontal"]:
                W = page_widths[pi] if page_widths else 612
                if b["bbox"][2] < 0.12 * W or b["bbox"][0] > 0.88 * W:
                    b["role"], b["role_rule"] = "margin_note", "rotated text in outer page margin (e.g. arXiv stamp)"
                else:
                    b["role"], b["role_rule"] = "rotated_text", "rotated text inside the page area, not near a detected figure"
                hf.append(b)
                continue
            if not (top or bottom):
                continue
            k = norm_key(b["text"])
            repeated = len(cand[k]) >= 2 and len(cand[k]) >= 0.3 * n_pages
            pagenum = bool(RE_PAGE_NUM.match(b["text"]))
            small_short = body_size is not None and b["size"] < 0.9 * body_size and len(b["text"]) < 100
            if repeated or pagenum or (len(b["text"]) < 90 and len(cand[k]) >= 2) or small_short:
                b["role"] = "header" if top else "footer"
                if pagenum:
                    b["role_rule"] = "page number"
                elif repeated or len(cand[k]) >= 2:
                    b["role_rule"] = f"repeats on {len(cand[k])} pages in margin band"
                else:
                    b["role_rule"] = "short small-font line in margin band (not repeated; check)"
                hf.append(b)
    return hf


def find_gutters(blocks, W):
    """Return x positions of column gutters from narrow body blocks."""
    narrow = [b for b in blocks if (b["bbox"][2] - b["bbox"][0]) < 0.58 * W and len(b["text"]) > 15]
    if len(narrow) < 3:
        return []
    nb = 200
    occ = [0.0] * nb
    for b in narrow:
        i0 = max(0, int(b["bbox"][0] / W * nb))
        i1 = min(nb - 1, int(b["bbox"][2] / W * nb))
        h = b["bbox"][3] - b["bbox"][1]
        for i in range(i0, i1 + 1):
            occ[i] += h
    xs = [b["bbox"][0] for b in narrow]
    xe = [b["bbox"][2] for b in narrow]
    left_edge, right_edge = min(xs), max(xe)
    # A gutter may be crossed by a few centred blocks (author lines, e-mail,
    # short headings), so "empty" means: occupied by < 12% of the typical
    # column occupancy, not strictly zero.
    mid = [o for o in occ[int(0.2 * nb):int(0.8 * nb)] if o > 0]
    typical = sorted(mid)[int(0.75 * (len(mid) - 1))] if mid else 0
    low = max(1e-6, 0.2 * typical)
    total_h = sum(b["bbox"][3] - b["bbox"][1] for b in narrow)
    gutters = []
    i = int(0.2 * nb)
    while i < int(0.8 * nb):
        if occ[i] <= low:
            j = i
            while j < nb and occ[j] <= low:
                j += 1
            # the gutter is the widest stretch of minimum occupancy inside the low run
            m = min(occ[i:j])
            best, k = (i, i + 1), i
            while k < j:
                if occ[k] <= m + 0.02 * typical:
                    e = k
                    while e < j and occ[e] <= m + 0.02 * typical:
                        e += 1
                    if e - k > best[1] - best[0]:
                        best = (k, e)
                    k = e
                else:
                    k += 1
            g = (best[0] + best[1]) / 2 / nb * W
            left_h = sum(b["bbox"][3] - b["bbox"][1] for b in narrow if b["bbox"][2] <= g)
            right_h = sum(b["bbox"][3] - b["bbox"][1] for b in narrow if b["bbox"][0] >= g)
            def side_ok(side):
                # a real column: some height, and either several blocks or one column-wide block
                bs = [b for b in narrow if (b["bbox"][2] <= g if side == "L" else b["bbox"][0] >= g)]
                h = sum(b["bbox"][3] - b["bbox"][1] for b in bs)
                return h > 30 and any(len(b["text"]) >= 80 for b in bs) and \
                    (len(bs) >= 2 or any(b["bbox"][2] - b["bbox"][0] >= 0.33 * W for b in bs))
            if left_edge < g < right_edge and side_ok("L") and side_ok("R") and (j - i) >= 2:
                gutters.append(round(g, 1))
            i = j
        else:
            i += 1
    return gutters


OUT_OF_FLOW = ("header", "footer", "margin_note", "figure_text", "rotated_text")


def order_page(blocks, W):
    """Assign column + reading order. Full-width blocks cut the page into
    horizontal bands; inside a band, columns are read left to right."""
    body = [b for b in blocks if b["role"] not in OUT_OF_FLOW]
    gutters = find_gutters([b for b in body if b["role"] != "table"], W)
    tol = 3.0
    spanning, cols = [], []
    for b in body:
        x0, x1 = b["bbox"][0], b["bbox"][2]
        if any(x0 < g - tol and x1 > g + tol for g in gutters):
            b["column"] = "span"
            spanning.append(b)
        else:
            cx = (x0 + x1) / 2
            b["column"] = sum(1 for g in gutters if cx > g)
            cols.append(b)
    spanning.sort(key=lambda b: b["bbox"][1])
    ordered = []
    remaining = sorted(cols, key=lambda b: (b["column"], b["bbox"][1], b["bbox"][0]))
    for s in spanning:
        before = [b for b in remaining if (b["bbox"][1] + b["bbox"][3]) / 2 < s["bbox"][1] + 1]
        ordered.extend(sorted(before, key=lambda b: (b["column"], b["bbox"][1], b["bbox"][0])))
        remaining = [b for b in remaining if b not in before]
        ordered.append(s)
    ordered.extend(sorted(remaining, key=lambda b: (b["column"], b["bbox"][1], b["bbox"][0])))
    margin = [b for b in blocks if b["role"] in OUT_OF_FLOW]
    return ordered, margin, gutters


# ----------------------------------------------------------------- tables
def ruling_segments(page):
    """Horizontal / vertical rules from vector drawings (lines and thin rects)."""
    hs, vs = [], []
    try:
        drawings = page.get_drawings()
    except Exception:
        return hs, vs
    for d in drawings:
        for it in d.get("items", []):
            if it[0] == "l":
                p, q = it[1], it[2]
                if abs(p.y - q.y) < 1.0 and abs(p.x - q.x) > 30:
                    hs.append((min(p.x, q.x), max(p.x, q.x), (p.y + q.y) / 2))
                elif abs(p.x - q.x) < 1.0 and abs(p.y - q.y) > 6:
                    vs.append((min(p.y, q.y), max(p.y, q.y), (p.x + q.x) / 2))
            elif it[0] == "re":
                r = it[1]
                if r.height < 2.5 and r.width > 30:
                    hs.append((r.x0, r.x1, (r.y0 + r.y1) / 2))
                elif r.width < 2.5 and r.height > 6:
                    vs.append((r.y0, r.y1, (r.x0 + r.x1) / 2))
    # merge collinear horizontal pieces
    hs.sort(key=lambda s: (round(s[2]), s[0]))
    merged = []
    for s in hs:
        if merged and abs(merged[-1][2] - s[2]) < 1.0 and s[0] <= merged[-1][1] + 2:
            m = merged[-1]
            merged[-1] = (m[0], max(m[1], s[1]), m[2])
        else:
            merged.append(s)
    return merged, vs


def column_intervals(words, x0, x1, min_gap=7.0):
    """Infer column intervals from whitespace gaps common to all rows."""
    if not words:
        return [(x0, x1)]
    n = int(x1 - x0) + 2
    occ = [0] * n
    for w in words:
        a = max(0, int(w[0] - x0))
        b = min(n - 1, int(w[2] - x0))
        for i in range(a, b + 1):
            occ[i] += 1
    cols, start, i = [], None, 0
    gap_start = None
    for i in range(n):
        if occ[i] > 0:
            if start is None:
                start = i
            elif gap_start is not None and i - gap_start >= min_gap:
                cols.append((x0 + start, x0 + gap_start))
                start = i
            gap_start = None
        else:
            if start is not None and gap_start is None:
                gap_start = i
    if start is not None:
        end = gap_start if gap_start is not None else n - 1
        cols.append((x0 + start, x0 + end))
    return cols or [(x0, x1)]


def group_rows(words, tol=2.5):
    rows = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        cy = (w[1] + w[3]) / 2
        if rows and abs(rows[-1]["cy"] - cy) <= tol:
            rows[-1]["words"].append(w)
            rows[-1]["cy"] = (rows[-1]["cy"] * (len(rows[-1]["words"]) - 1) + cy) / len(rows[-1]["words"])
        else:
            rows.append({"cy": cy, "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w[0])
    return rows


def phrases(words, max_gap=4.5):
    out = []
    for w in words:
        if out and w[0] - out[-1]["x1"] <= max_gap:
            out[-1]["text"] += " " + w[4]
            out[-1]["x1"] = w[2]
        else:
            out.append({"x0": w[0], "x1": w[2], "text": w[4]})
    return out


RE_NUMTOK = re.compile(r"^[\(\[]?[-−+±~<>≈]?\$?\d[\d,\.]*(?:[eE][-+]?\d+)?[%×x]?[\)\]\*†‡]*$|^[-–—]$|^n/?a$", re.I)


def is_num(tok):
    return bool(RE_NUMTOK.match(tok.strip()))


def row_gaps(r):
    ws = r["words"]
    return [b[0] - a[2] for a, b in zip(ws, ws[1:])]


def line_is_prose(r, width):
    """A running-text line: many words, no wide gaps, mostly not numbers."""
    ws = r["words"]
    if len(ws) < 6:
        return False
    gaps = row_gaps(r)
    nums = sum(1 for w in ws if is_num(w[4]))
    span = ws[-1][2] - ws[0][0]
    return max(gaps) < 6.5 and span > 0.55 * width and nums < 0.3 * len(ws)


def line_is_tablelike(r):
    ws = r["words"]
    nums = sum(1 for w in ws if is_num(w[4]))
    gaps = row_gaps(r)
    return nums >= 3 or (len(ws) >= 2 and max(gaps) >= 8) or (nums >= 2 and nums >= 0.4 * len(ws))


def band_rows(words, x0, x1, y0, y1):
    tw = [w for w in words if x0 - 2 <= (w[0] + w[2]) / 2 <= x1 + 2 and y0 < (w[1] + w[3]) / 2 < y1]
    return group_rows(tw)


def align_columns(body_rows, x0, x1, tol=2.0):
    """Columns from token alignment across body rows (left, right or centre edges)."""
    nrows = len(body_rows)
    toks = []
    for ri, r in enumerate(body_rows):
        for wi, w in enumerate(r["words"]):
            toks.append((ri, wi, w))
    cands = []
    for anchor, fn in (("left", lambda w: w[0]), ("right", lambda w: w[2]), ("center", lambda w: (w[0] + w[2]) / 2)):
        vals = sorted(toks, key=lambda t: fn(t[2]))
        cur = []
        for t in vals:
            if cur and fn(t[2]) - fn(cur[-1][2]) > tol:
                cands.append((anchor, cur))
                cur = []
            cur.append(t)
        if cur:
            cands.append((anchor, cur))
    scored = []
    for anchor, members in cands:
        rows = {t[0] for t in members}
        if len(members) != len(rows):  # two tokens of one row in one cluster: not a column
            members = list({t[0]: t for t in members}.values())
        scored.append((len(rows), anchor, members))
    scored.sort(key=lambda s: -s[0])
    claimed = set()
    cols = []
    thresh = max(2, 0.3 * nrows)
    for support, anchor, members in scored:
        new = [t for t in members if (t[0], t[1]) not in claimed]
        if len({t[0] for t in new}) < thresh or len(new) < 0.6 * len(members):
            continue
        for t in new:
            claimed.add((t[0], t[1]))
        cols.append({"anchor": anchor, "x0": min(t[2][0] for t in new), "x1": max(t[2][2] for t in new),
                     "support": len({t[0] for t in new})})
    cols.sort(key=lambda c: c["x0"])
    # merge columns whose spans overlap heavily (same column, mixed alignment)
    merged = []
    for c in cols:
        if merged:
            p = merged[-1]
            ov = min(p["x1"], c["x1"]) - max(p["x0"], c["x0"])
            if ov > 0.5 * min(p["x1"] - p["x0"], c["x1"] - c["x0"]):
                p["x0"], p["x1"] = min(p["x0"], c["x0"]), max(p["x1"], c["x1"])
                p["support"] = max(p["support"], c["support"])
                continue
        merged.append(dict(c))
    # leading label column for unclaimed tokens left of the first column
    lead = [t for t in toks if (t[0], t[1]) not in claimed and merged and t[2][2] <= merged[0]["x0"] + 1]
    if lead and len({t[0] for t in lead}) >= thresh:
        merged.insert(0, {"anchor": "left", "x0": min(t[2][0] for t in lead), "x1": max(t[2][2] for t in lead),
                          "support": len({t[0] for t in lead})})
    return merged


def assign_col(cols, x0, x1):
    """Column for a token: the column whose span contains it, else the nearest column to its left."""
    cx = (x0 + x1) / 2
    for i, c in enumerate(cols):
        if c["x0"] - 1.5 <= cx <= c["x1"] + 1.5:
            return i
    left = [i for i, c in enumerate(cols) if c["x1"] <= x0 + 1.5]
    if left:
        return left[-1]
    return min(range(len(cols)), key=lambda i: abs((cols[i]["x0"] + cols[i]["x1"]) / 2 - cx))


def assign_header_col(cols, x0, x1):
    """Header tokens are often centred over their column: use midpoints between columns."""
    cx = (x0 + x1) / 2
    for i, c in enumerate(cols):
        lo = (cols[i - 1]["x1"] + c["x0"]) / 2 if i > 0 else -1e9
        hi = (c["x1"] + cols[i + 1]["x0"]) / 2 if i + 1 < len(cols) else 1e9
        if lo <= cx < hi:
            return i
    return len(cols) - 1


def merge_label_columns(cols, head, body):
    """A column without any header word whose values are all text continues the
    text column on its left (multi-word row labels such as "GPT-3 Few-Shot")."""
    if len(cols) < 3:
        return cols
    has_head = [False] * len(cols)
    for r in head:
        for w in r["words"]:
            has_head[assign_header_col(cols, w[0], w[2])] = True
    vals = [[] for _ in cols]
    for r in body:
        for w in r["words"]:
            vals[assign_col(cols, w[0], w[2])].append(w[4])
    textual = [bool(v) and not any(is_num(x) for x in v) for v in vals]
    out = [dict(cols[0])]
    out_text = [textual[0]]
    for i in range(1, len(cols)):
        if not has_head[i] and textual[i] and out_text[-1]:
            out[-1]["x1"] = max(out[-1]["x1"], cols[i]["x1"])
            continue
        out.append(dict(cols[i]))
        out_text.append(textual[i])
    return out


def detect_ruled_tables(page, page_no):
    """Tables delimited by horizontal rules (booktabs or full grids).

    Rules are chained into one table while the band between them contains no
    prose line or caption; columns come from vertical rules when present,
    otherwise from token alignment in the body rows; header rows (above the
    header rule) are mapped onto the leaf columns, and short rules under a
    header phrase (cmidrules) turn it into a spanning group header.
    """
    hs, vs = ruling_segments(page)
    if len(hs) < 2:
        return []
    words = page.get_text("words")
    W = page.rect.width
    groups = []
    for s in sorted(hs, key=lambda s: (s[2], s[0])):
        placed = False
        for g in reversed(groups):
            gx0 = min(r[0] for r in g)
            gx1 = max(r[1] for r in g)
            ov = min(gx1, s[1]) - max(gx0, s[0])
            if ov <= 0.6 * min(gx1 - gx0, s[1] - s[0]):
                continue
            last_y = max(r[2] for r in g)
            if s[2] - last_y > 2:
                rows = band_rows(words, max(gx0, s[0]), min(gx1, s[1]), last_y, s[2])
                width = min(gx1, s[1]) - max(gx0, s[0])
                if any(line_is_prose(r, width) or RE_CAPTION.match(" ".join(w[4] for w in r["words"])) for r in rows):
                    break
            g.append(s)
            placed = True
            break
        if not placed:
            groups.append([s])
    tables = []
    for g in groups:
        maxlen = max(s[1] - s[0] for s in g)
        main = sorted([s for s in g if s[1] - s[0] >= 0.8 * maxlen], key=lambda s: s[2])
        partial = [s for s in g if s[1] - s[0] < 0.8 * maxlen]
        if not main:
            continue
        x0 = min(s[0] for s in main)
        x1 = max(s[1] for s in main)
        y0, y1 = main[0][2], main[-1][2]
        header_rule = main[1][2] if len(main) >= 3 else None
        extended = False
        # no bottom rule: extend downward while lines stay table-like
        if len(main) <= 2:
            below = band_rows(words, x0, x1, y1, page.rect.height)
            last = y1
            pitch = None
            taken = 0
            for r in below:
                text = " ".join(w[4] for w in r["words"])
                top = min(w[1] for w in r["words"])
                gap = top - last
                if (not line_is_tablelike(r)) or line_is_prose(r, x1 - x0) or RE_CAPTION.match(text):
                    break
                if pitch is not None and gap > 2.5 * pitch + 4:
                    break
                pitch = max(4.0, gap) if pitch is None else pitch
                last = max(w[3] for w in r["words"])
                taken += 1
            if taken >= 2:
                if len(main) == 2 and header_rule is None:
                    header_rule = y1
                y1 = last + 1
                extended = True
        if y1 - y0 < 6:
            continue
        rows = band_rows(words, x0, x1, y0 - 1, y1 + 1)
        if len(rows) < 2:
            continue
        if header_rule is not None:
            head = [r for r in rows if r["cy"] < header_rule]
            body = [r for r in rows if r["cy"] >= header_rule]
        else:
            k = 0
            while k < len(rows) and sum(1 for w in rows[k]["words"] if is_num(w[4])) < 0.5 * len(rows[k]["words"]):
                k += 1
            k = max(1, min(k, len(rows) - 1))
            head, body = rows[:k], rows[k:]
        if not body or sum(1 for r in body if line_is_tablelike(r)) < max(1, 0.5 * len(body)):
            continue
        if sum(1 for r in body if line_is_prose(r, x1 - x0)) > 0.2 * len(body):
            continue  # boxed running text (e.g. prompt examples), not a table
        near = band_rows(words, x0, x1, y0 - 40, y0 - 1) + band_rows(words, x0, x1, y1 + 1, y1 + 40)
        near_caps = [" ".join(w[4] for w in r["words"]) for r in near]
        if any(re.match(r"^\s*(Fig\.?|Figure|FIGURE)\s*[A-Z]?\.?\d", t) for t in near_caps) and \
                not any(re.match(r"^\s*(Table|TABLE|Tab\.)\s*[A-Z]?\.?\d", t) for t in near_caps):
            continue  # ruled box captioned as a figure
        inner_v = sorted({round(v[2], 1) for v in vs if v[0] < y1 and v[1] > y0 and x0 - 2 <= v[2] <= x1 + 2})
        if len(inner_v) >= 2:
            edges = inner_v
            if edges[0] - x0 > 5:
                edges = [x0] + edges
            if x1 - edges[-1] > 5:
                edges = edges + [x1]
            cols = [{"anchor": "rules", "x0": a, "x1": b, "support": len(body)} for a, b in zip(edges, edges[1:])]
            col_rule = "vertical rules"
        else:
            cols = align_columns(body, x0, x1)
            col_rule = "token alignment across body rows"
            # sparse columns (few filled rows) survive only if a header sits over them
            inside = lambda cx: any(c["x0"] - 2 <= cx <= c["x1"] + 2 for c in cols)
            hw = sorted([w for r in head for w in r["words"] if not inside((w[0] + w[2]) / 2)], key=lambda w: w[0])
            spans = []
            for w in hw:
                if spans and w[0] <= spans[-1][1] + 3:
                    spans[-1][1] = max(spans[-1][1], w[2])
                else:
                    spans.append([w[0], w[2]])
            for a, b in spans:
                orphans = [w for r in body for w in r["words"] if a - 3 <= (w[0] + w[2]) / 2 <= b + 3 and not inside((w[0] + w[2]) / 2)]
                if orphans:
                    cols.append({"anchor": "header", "x0": min(min(w[0] for w in orphans), a), "x1": max(max(w[2] for w in orphans), b),
                                 "support": len(orphans)})
            cols.sort(key=lambda c: c["x0"])
            cols = merge_label_columns(cols, head, body)
        if len(cols) < 2:
            continue
        ncol = len(cols)
        grid, cells, records = [], [], []
        # ---- header rows
        header_cells = []  # per header row: list of (c0, c1, text)
        for ri, r in enumerate(head):
            slots = {}
            spans = []
            for w in r["words"]:
                wcx = (w[0] + w[2]) / 2
                pr = next((p for p in partial if p[0] - 2 <= wcx <= p[1] + 2 and 0 < p[2] - r["cy"] < 14), None)
                if pr is not None:
                    under = [i for i, c in enumerate(cols) if c["x0"] < pr[1] - 1 and c["x1"] > pr[0] + 1]
                    if under:
                        key = (under[0], under[-1])
                        slots.setdefault(key, []).append(w)
                        continue
                ci = assign_header_col(cols, w[0], w[2])
                slots.setdefault((ci, ci), []).append(w)
            row_cells = []
            for (c0, c1), ws in sorted(slots.items()):
                ws.sort(key=lambda w: w[0])
                row_cells.append((c0, c1, " ".join(w[4] for w in ws),
                                  [round(min(w[0] for w in ws), 2), round(min(w[1] for w in ws), 2),
                                   round(max(w[2] for w in ws), 2), round(max(w[3] for w in ws), 2)]))
            header_cells.append(row_cells)
        for ri, row_cells in enumerate(header_cells):
            flat = [""] * ncol
            for c0, c1, text, bb in row_cells:
                if flat[c0] not in ("", None):
                    flat[c0] = flat[c0] + " " + text
                else:
                    flat[c0] = text
                for k in range(c0 + 1, c1 + 1):
                    flat[k] = None
                cells.append({"row": ri, "col": c0, "text": text, "colspan": c1 - c0 + 1, "rowspan": 1, "bbox": bb, "header": True})
            grid.append(flat)
        paths = []
        for ci in range(ncol):
            path = []
            for row_cells in header_cells:
                for c0, c1, text, bb in row_cells:
                    if c0 <= ci <= c1 and text and text not in path:
                        path.append(text)
            paths.append(path)
        # ---- body rows
        nh = len(head)
        for bi, r in enumerate(body):
            slots = defaultdict(list)
            for w in r["words"]:
                slots[assign_col(cols, w[0], w[2])].append(w)
            flat = [""] * ncol
            for ci, ws in sorted(slots.items()):
                ws.sort(key=lambda w: w[0])
                text = " ".join(w[4] for w in ws)
                flat[ci] = text
                cells.append({"row": nh + bi, "col": ci, "text": text, "colspan": 1, "rowspan": 1,
                              "bbox": [round(min(w[0] for w in ws), 2), round(min(w[1] for w in ws), 2),
                                       round(max(w[2] for w in ws), 2), round(max(w[3] for w in ws), 2)]})
            grid.append(flat)
        # key (row-label) columns: leading columns whose body values are mostly non-numeric
        key_cols = []
        for ci in range(ncol):
            vals = [grid[nh + bi][ci] for bi in range(len(body)) if grid[nh + bi][ci]]
            if vals and sum(1 for v in vals if is_num(v.split()[0])) < 0.3 * len(vals):
                key_cols.append(ci)
            else:
                break
        names = []
        for ci in range(ncol):
            n = " / ".join(paths[ci]) or f"col{ci}"
            while n in names:
                n += "'"
            names.append(n)
        for bi in range(len(body)):
            rec = {"_row": nh + bi}
            for ci in range(ncol):
                rec[names[ci]] = grid[nh + bi][ci]
            records.append(rec)
        tables.append({
            "id": None, "page": page_no, "bbox": [round(x0, 2), round(y0 - 2, 2), round(x1, 2), round(y1 + 2, 2)],
            "n_rows": len(grid), "n_cols": ncol, "rows": grid, "cells": cells,
            "header": {"n_header_rows": nh, "names": names},
            "columns": [{"index": ci, "path": paths[ci], "name": names[ci], "x0": round(cols[ci]["x0"], 2),
                         "x1": round(cols[ci]["x1"], 2), "align": cols[ci]["anchor"]} for ci in range(ncol)],
            "key_columns": key_cols,
            "records": records,
            "caption": None, "caption_block": None, "continued_from": None,
            "detector": f"ruling lines ({len(main)} full rules, {len(partial)} partial; columns from {col_rule}"
                        + ("; body extended below last rule" if extended else "") + ")",
            "span_inference": "group header spans from short rules (cmidrules) under the header phrase; "
                              "None marks covered cells; other spans not inferred",
        })
    return tables


def extract_tables(page, page_no):
    out = detect_ruled_tables(page, page_no)
    if out:
        return out, None
    if not hasattr(page, "find_tables"):
        return out, None
    try:
        try:  # explicit ruling-line strategy: PyMuPDF >= 2 may otherwise also report text-only 'tables' (author blocks, prose)
            tabs = page.find_tables(strategy="lines")
        except TypeError:
            tabs = page.find_tables()
    except Exception as e:
        return out, f"find_tables failed: {e}"
    for ti, t in enumerate(tabs.tables if hasattr(tabs, "tables") else tabs):
        try:
            rows = t.extract()
        except Exception:
            continue
        if not rows or len(rows) < 2 or max(len(r) for r in rows) < 2:
            continue
        ncol = max(len(r) for r in rows)
        rows = [list(r) + [None] * (ncol - len(r)) for r in rows]
        nonempty = sum(1 for r in rows for c in r if c not in (None, ""))
        if nonempty < 4:
            continue
        cells = []
        for ri, r in enumerate(rows):
            for ci, c in enumerate(r):
                if c is None:
                    continue
                colspan = 1
                k = ci + 1
                while k < ncol and r[k] is None and (ri == 0 or rows[ri - 1][k] is not None or rows[ri - 1][ci] is None):
                    colspan += 1
                    k += 1
                rowspan = 1
                k = ri + 1
                while k < len(rows) and rows[k][ci] is None and colspan == 1 and ci == 0:
                    rowspan += 1
                    k += 1
                cells.append({"row": ri, "col": ci, "text": norm_ws(c), "colspan": colspan, "rowspan": rowspan})
        try:
            hdr = t.header
            header = {"names": [norm_ws(x) if x else "" for x in hdr.names], "external": bool(hdr.external)}
        except Exception:
            header = None
        out.append({
            "id": None,
            "page": page_no,
            "bbox": [round(v, 2) for v in t.bbox],
            "n_rows": len(rows),
            "n_cols": ncol,
            "rows": [[norm_ws(c) if c is not None else None for c in r] for r in rows],
            "cells": cells,
            "header": header,
            "caption": None,
            "caption_block": None,
            "continued_from": None,
            "span_inference": "None cells from PyMuPDF treated as merged-cell continuations (heuristic)",
        })
    return out, None


def table_to_markdown(t):
    rows = [[(c if c is not None else "") for c in r] for r in t["rows"]]
    if not rows:
        return ""
    w = len(rows[0])
    esc = lambda s: s.replace("|", "\\|")
    lines = ["| " + " | ".join(esc(c) for c in rows[0]) + " |", "|" + "---|" * w]
    for r in rows[1:]:
        lines.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(lines)


def table_to_html(t):
    spans = {(c["row"], c["col"]): c for c in t["cells"]}
    n_header = (t.get("header") or {}).get("n_header_rows") or 1
    out = ["<table>"]
    for ri, row in enumerate(t["rows"]):
        out.append("  <tr>")
        tag = "th" if ri < n_header else "td"
        for ci, val in enumerate(row):
            if val is None:
                continue  # covered by a spanning cell
            c = spans.get((ri, ci), {"colspan": 1, "rowspan": 1})
            attrs = ""
            if c["colspan"] > 1:
                attrs += f' colspan="{c["colspan"]}"'
            if c["rowspan"] > 1:
                attrs += f' rowspan="{c["rowspan"]}"'
            out.append(f"    <{tag}{attrs}>{html.escape(val)}</{tag}>")
        out.append("  </tr>")
    out.append("</table>")
    return "\n".join(out)


def bbox_center_in(b, box, pad=2):
    cx = (b[0] + b[2]) / 2
    cy = (b[1] + b[3]) / 2
    return box[0] - pad <= cx <= box[2] + pad and box[1] - pad <= cy <= box[3] + pad


# ----------------------------------------------------------------- roles
def math_ratio(t):
    t = t.replace(" ", "")
    if not t:
        return 0
    return sum(1 for c in t if c in MATH_CHARS) / len(t)


def classify_roles(ordered, body_size, page_heights, first_page_no):
    """Assign title/author/abstract/heading/caption/equation/footnote/reference roles."""
    in_refs = False
    seen_abstract = False
    title_done = False
    # title: largest font on first page upper half
    first = [b for b in ordered if b["page"] == first_page_no and b["bbox"][1] < 0.5 * page_heights[first_page_no]]
    if first:
        maxsize = max(b["size"] for b in first)
        if maxsize >= body_size * 1.3:
            for b in first:
                if b["size"] >= maxsize - 0.3 and len(b["text"]) < 300:
                    b["role"], b["role_rule"] = "title", f"largest font on page 1 ({b['size']}pt)"
                    title_done = True
    for i, b in enumerate(ordered):
        if b["role"] in ("title", "table") or (b["role"] == "equation" and str(b.get("role_rule", "")).startswith("display-math region")):
            continue
        t = norm_ws(b["text"])
        H = page_heights[b["page"]]
        if RE_REFS_HEAD.match(t):
            b["role"], b["role_rule"] = "heading", "references heading"
            in_refs = True
            continue
        if in_refs:
            if heading_like(t) and (b["bold"] or b["size"] > body_size * 1.05) and not RE_REF_START_NUM.match(t):
                in_refs = False
            else:
                b["role"], b["role_rule"] = "reference", "after references heading"
                continue
        cm = RE_CAPTION.match(t)
        if cm:
            has_sep = bool(re.search(r"[\.:|：．]\s*$", cm.group(0))) and not re.match(r"^\s*(Fig|Tab)\.\s*\d+\s*$", cm.group(0))
            lead_bold = bool(b["lines"]) and b["lines"][0].get("lead_bold")
            smaller = b["size"] < body_size * 0.97
            if has_sep or lead_bold or b["bold"] or (smaller and "。" not in t[:200]):
                why = "separator" if has_sep else ("bold label" if (lead_bold or b["bold"]) else "smaller font")
                b["role"], b["role_rule"] = "caption", f"caption prefix (Figure/Table/图/表 N) + {why}"
                continue
        hl = heading_like(t)
        short = len(b["lines"]) <= 2 and len(t) < 130
        if hl and short and (b["bold"] or b["size"] > body_size * 1.05 or hl[0] in ("named", "roman", "cn")
                             or (hl[0] == "numeric" and not SENT_END.search(t))):
            b["role"], b["role_rule"] = "heading", f"{hl[0]} heading pattern"
            b["heading"] = {"kind": hl[0], "number": hl[1], "title": norm_ws(hl[2])}
            if re.match(r"^\s*abstract", t, re.I) or re.match(r"^\s*摘\s*要", t):
                seen_abstract = True
            continue
        if re.match(r"^\s*(abstract|摘\s*要)\s*[:：\.—-]", t, re.I) or (not seen_abstract and re.match(r"^\s*abstract\b", t, re.I)):
            b["role"], b["role_rule"] = "abstract", "starts with 'Abstract'"
            seen_abstract = True
            continue
        if (b["size"] < body_size * 0.92 and b["bbox"][1] > 0.72 * H and len(t) < 600
                and (re.match(r"^\s*([\*†‡§¶]|\d{1,2}\s|[a-z]\s)", t) or (b.get("marked") or "").startswith("^{"))):
            b["role"], b["role_rule"] = "footnote", "small font, bottom of page, starts with marker"
            continue
        m = RE_EQ_NUM.search(t)
        mr = math_ratio(t)
        mathfont = any(re.search(r"math|cmmi|cmsy|cmex|symbol|stix", f, re.I) for f in b["fonts"])
        if len(t) < 260 and ((m and (mr > 0.12 or mathfont or "=" in t)) or (mr > 0.3 and len(t) > 3)):
            b["role"] = "equation"
            b["role_rule"] = "math-symbol density" + (" + trailing equation number" if m else "")
            b["equation_number"] = m.group(1) if m else None
            continue
    # authors / affiliations: blocks between title and abstract on first page
    if title_done:
        for b in ordered:
            if b["page"] != first_page_no:
                break
            if b["role"] in ("abstract",) or (b["role"] == "heading" and re.match(r"abstract|摘", b["text"], re.I)):
                break
            if b["role"] == "body" and b["bbox"][1] < 0.6 * page_heights[first_page_no]:
                prev_title = any(x["role"] == "title" and x["bbox"][3] <= b["bbox"][1] + 1 for x in ordered if x["page"] == first_page_no)
                if prev_title and len(b["text"]) < 400:
                    b["role"], b["role_rule"] = "author_affiliation", "between title and abstract on page 1"


def heading_level(h):
    if h["kind"] == "numeric":
        return h["number"].count(".") + 1
    if h["kind"] in ("roman", "named", "cn"):
        return 1
    if h["kind"] == "alpha":
        return 2
    if h["kind"] == "appendix":
        return h["number"].count(".") + 1
    return 1


def split_references(blocks):
    text = " ".join(norm_ws(b["text"]) for b in blocks)
    refs = []
    if re.search(r"\[\d{1,4}\]\s", text):
        parts = [p.strip() for p in RE_REF_SPLIT_BRACKET.split(text) if p.strip()]
    else:
        parts = []
        for b in blocks:
            cur = ""
            for ln in b["lines"]:
                t = ln["text"].strip()
                if RE_REF_START_NUM.match(t) and cur:
                    parts.append(cur)
                    cur = t
                else:
                    cur = (cur + " " + t).strip()
            if cur:
                parts.append(cur)
    for p in parts:
        # locate the block that contains the start of the entry
        key = norm_ws(p)[:40]
        src = next((b for b in blocks if key[:25] in norm_ws(b["text"])), blocks[0] if blocks else None)
        m = RE_REF_START_NUM.match(p)
        refs.append({
            "label": m.group(1).strip("[].") if m else None,
            "text": p,
            "page": src["page"] if src else None,
            "block_id": src["id"] if src else None,
        })
    return refs


# ----------------------------------------------------------------- main parse
def parse(path, out_dir, password=None, pages=None, ocr_mode="auto", lang="eng+chi_sim", preview=False):
    t0 = time.time()
    os.makedirs(out_dir, exist_ok=True)
    warnings = []
    doc, err, msg = open_document(path, password)
    meta = {
        "source_file": os.path.abspath(path),
        "parser": f"pdf-evidence parse_pdf.py {PARSER_VERSION}",
        "pymupdf": getattr(fitz, "VersionBind", "?"),
    }
    if os.path.exists(path):
        with open(path, "rb") as fh:
            meta["sha256"] = hashlib.sha256(fh.read()).hexdigest()
    if doc is None:
        result = {"status": "failed", "error": err, "message": msg, "meta": meta,
                  "human_takeover": "Provide a decrypted/valid PDF or the password; nothing was extracted."}
        write_json(os.path.join(out_dir, "doc.json"), result)
        write_json(os.path.join(out_dir, "qa_report.json"), {"status": "failed", "error": err, "message": msg})
        return result
    meta.update({"page_count": doc.page_count, "metadata": {k: v for k, v in (doc.metadata or {}).items() if v},
                 "was_encrypted": bool(getattr(doc, "is_encrypted", False))})
    try:
        labels = [doc[i].get_label() for i in range(doc.page_count)]
        if any(labels):
            meta["page_labels"] = labels
    except Exception:
        pass
    page_idx = list(range(doc.page_count))
    if pages:
        page_idx = [p - 1 for p in pages if 1 <= p <= doc.page_count]
    pages_blocks, page_info, page_heights, all_tables, figures = [], [], [], [], []
    ocr_pages, need_ocr_unresolved = [], []
    for pi in page_idx:
        page = doc[pi]
        pno = pi + 1
        W, H = page.rect.width, page.rect.height
        raw = _raw_lines(page)
        try:
            cap_tables = caption_line_tables(page, pno, raw)
        except Exception as ex:  # never let the table heuristic break a page
            cap_tables = []
            warnings.append({"page": pno, "code": "caption_table_failed", "detail": str(ex)[:200]})
        blocks = extract_text_blocks(page, pno, exclude_boxes=[t["bbox"] for t in cap_tables], raw=raw)
        scanned, chars, img_frac = page_is_scanned(page, blocks)
        g = garbled_ratio(" ".join(b["text"] for b in blocks))
        info = {"page": pno, "width": round(W, 2), "height": round(H, 2), "text_layer_chars": chars,
                "image_coverage": round(img_frac, 3), "garbled_ratio": round(g, 4), "source": "text_layer"}
        if (scanned and ocr_mode == "auto") or ocr_mode == "force" or (g > 0.05 and ocr_mode == "auto"):
            reason = "no text layer (scanned)" if scanned else ("garbled font encoding" if g > 0.05 else "forced")
            oblocks, oinfo = ocr_page(page, pno, os.path.join(out_dir, "_ocr"), lang)
            info["ocr"] = oinfo
            info["ocr_reason"] = reason
            if oblocks:
                blocks = oblocks
                info["source"] = "ocr"
                ocr_pages.append(pno)
            else:
                need_ocr_unresolved.append(pno)
                warnings.append({"page": pno, "code": "ocr_needed_but_unavailable",
                                 "detail": f"{reason}; {oinfo.get('reason', '')}. Text for this page is missing or unreliable. "
                                           f"Page image and layout regions are provided for visual reading."})
                try:
                    rel, dpi_used, regs = page_image_and_regions(page, pno, out_dir)
                    info["page_image"] = rel
                    info["page_image_dpi"] = dpi_used
                    info["image_regions"] = regs
                    info["source"] = "image_only"
                except Exception as ex:
                    info["page_image_error"] = str(ex)
        elif g > 0.01:
            warnings.append({"page": pno, "code": "possible_garbled_text", "detail": f"garbled char ratio {g:.3f}"})
        tables, terr = extract_tables(page, pno) if info["source"] == "text_layer" else ([], None)
        if info["source"] == "text_layer" and cap_tables:
            def _ov(a, b):
                ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
                iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
                return ix * iy / max(1e-6, min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])))
            tables = cap_tables + [t for t in tables if not any(_ov(t["bbox"], ct["bbox"]) > 0.3 for ct in cap_tables)]
        if terr:
            warnings.append({"page": pno, "code": "table_detection_unavailable", "detail": terr})
        for t in tables:
            for b in blocks:
                if bbox_center_in(b["bbox"], t["bbox"]) and not RE_CAPTION.match(b["text"]):
                    b["role"], b["role_rule"] = "table", "inside detected table bbox"
                    b["table_ref"] = len(all_tables) + tables.index(t)
        all_tables.extend(tables)
        try:
            for im in page.get_image_info():
                bb = [round(v, 2) for v in im["bbox"]]
                if (bb[2] - bb[0]) * (bb[3] - bb[1]) > 0.01 * W * H and info["source"] == "text_layer":
                    figures.append({"page": pno, "bbox": bb, "kind": "raster", "caption": None})
        except Exception:
            pass
        try:
            if hasattr(page, "cluster_drawings") and info["source"] == "text_layer":
                for r in page.cluster_drawings():
                    if r.width > 0.15 * W and r.height > 0.08 * H:
                        tb = [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]
                        if not any(abs(tb[1] - t["bbox"][1]) < 15 and abs(tb[0] - t["bbox"][0]) < 15 for t in tables if t["page"] == pno):
                            figures.append({"page": pno, "bbox": tb, "kind": "vector", "caption": None})
        except Exception:
            pass
        # graphic regions (for attaching tick labels / legends / axis titles to figures)
        regions = []
        if info["source"] == "text_layer":
            try:
                rects = [fitz.Rect(im["bbox"]) for im in page.get_image_info()]
                if hasattr(page, "cluster_drawings"):
                    rects += list(page.cluster_drawings())
                for r in rects:
                    r = r & page.rect
                    if r.width < 40 or r.height < 30 or r.width > 0.95 * W:
                        continue
                    rb = [r.x0, r.y0, r.x1, r.y1]
                    if any(min(rb[2], t["bbox"][2]) - max(rb[0], t["bbox"][0]) > 0 and
                           min(rb[3], t["bbox"][3]) - max(rb[1], t["bbox"][1]) > 0.3 * r.height for t in tables):
                        continue
                    regions.append(rb)
            except Exception:
                pass
        info["n_figure_text_blocks"] = mark_figure_text(blocks, regions, W)
        pages_blocks.append(blocks)
        page_info.append(info)
        page_heights.append(H)

    pre_sizes = Counter()
    for blocks in pages_blocks:
        for b in blocks:
            for ln in b["lines"]:
                pre_sizes[ln["size"]] += len(ln["text"])
    hf = detect_headers_footers(pages_blocks, page_heights, pre_sizes.most_common(1)[0][0] if pre_sizes else None,
                                [p["width"] for p in page_info])
    page_h = {p["page"]: p["height"] for p in page_info}
    ordered_all, margin_all = [], []
    for pi, blocks in enumerate(pages_blocks):
        W = page_info[pi]["width"]
        ordered, margin, gutters = order_page(blocks, W)
        page_info[pi]["column_gutters"] = gutters
        page_info[pi]["n_columns"] = len(gutters) + 1
        for k, b in enumerate(ordered):
            b["id"] = f"p{b['page']}-b{k + 1}"
        km = kg = 0
        for b in margin:
            if b["role"] == "figure_text":
                kg += 1
                b["id"] = f"p{b['page']}-g{kg}"
            else:
                km += 1
                b["id"] = f"p{b['page']}-m{km}"
        ordered_all.extend(ordered)
        margin_all.extend(margin)
    for k, b in enumerate(ordered_all):
        b["order"] = k + 1

    body_sizes = Counter()
    for b in ordered_all:
        for ln in b["lines"]:
            body_sizes[ln["size"]] += len(ln["text"])
    body_size = body_sizes.most_common(1)[0][0] if body_sizes else 10.0
    meta["body_font_size"] = body_size
    first_page_no = page_idx[0] + 1 if page_idx else 1
    classify_roles(ordered_all, body_size, page_h, first_page_no)

    # ---- sections
    sections, stack = [], []
    for b in ordered_all:
        if b["role"] == "heading":
            h = b.get("heading") or {"kind": "named", "number": "", "title": norm_ws(b["text"])}
            lvl = heading_level(h)
            while stack and stack[-1]["level"] >= lvl:
                stack.pop()
            sec = {"id": f"s{len(sections) + 1}", "level": lvl, "number": h["number"] or None,
                   "title": h["title"], "page": b["page"], "block_id": b["id"],
                   "parent": stack[-1]["id"] if stack else None, "rule": b["role_rule"]}
            sections.append(sec)
            stack.append(sec)
            b["section_path"] = [s["title"] for s in stack]
        else:
            b["section_path"] = [s["title"] for s in stack]
            b["section_id"] = stack[-1]["id"] if stack else None
    # numbering sanity check: report jumps such as 2 -> 4
    nums = [s["number"] for s in sections if s["number"] and re.match(r"^\d+$", s["number"])]
    for a, c in zip(nums, nums[1:]):
        if int(c) - int(a) > 1:
            warnings.append({"code": "section_number_gap", "detail": f"top-level section {a} followed by {c}; a heading may be missed"})

    def overlap_frac(a, b):
        ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
        return ix * iy / area
    figures = [f for f in figures if not any(t["page"] == f["page"] and overlap_frac(f["bbox"], t["bbox"]) > 0.5 for t in all_tables)]
    # ---- captions -> figures/tables
    captions = [b for b in ordered_all if b["role"] == "caption"]
    for t_i, t in enumerate(all_tables):
        t["id"] = f"T{t_i + 1}"
        cands = [c for c in captions if c["page"] == t["page"] and re.match(r"^\s*(Table|TABLE|Tab\.|表)", c["text"])]
        if cands:
            c = min(cands, key=lambda c: min(abs(c["bbox"][3] - t["bbox"][1]), abs(c["bbox"][1] - t["bbox"][3])))
            t["caption"], t["caption_block"] = norm_ws(c["text"]), c["id"]
    # cross-page table continuation
    for a, c in zip(all_tables, all_tables[1:]):
        if c["page"] == a["page"] + 1 and c["n_cols"] == a["n_cols"] and not c["caption"]:
            Ha = page_h[a["page"]]
            Hc = page_h[c["page"]]
            if a["bbox"][3] > 0.6 * Ha and c["bbox"][1] < 0.4 * Hc:
                c["continued_from"] = a["id"]
                c["caption"] = (a["caption"] or "") + " (continued)"
    for f_i, f in enumerate(figures):
        f["id"] = f"F{f_i + 1}"
        cands = [c for c in captions if c["page"] == f["page"] and re.match(r"^\s*(Fig|FIG|Figure|FIGURE|图|Scheme|Extended)", c["text"])]
        if cands:
            c = min(cands, key=lambda c: abs(c["bbox"][1] - f["bbox"][3]))
            f["caption"], f["caption_block"] = norm_ws(c["text"]), c["id"]
    # figure captions without a detected graphic still count as figure locations
    linked = {f.get("caption_block") for f in figures}
    for c in captions:
        if c["id"] not in linked and re.match(r"^\s*(Fig|FIG|Figure|FIGURE|图|Scheme|Extended)", c["text"]):
            figures.append({"id": f"F{len(figures) + 1}", "page": c["page"], "bbox": None, "kind": "caption_only",
                            "caption": norm_ws(c["text"]), "caption_block": c["id"]})

    equations = []
    eq_dir = os.path.join(out_dir, "equations")
    for b in ordered_all:
        if b["role"] != "equation":
            continue
        unc = ["LaTeX/Unicode rebuilt from font names and glyph positions; fractions, roots, stacked limits and "
               "matrices are not reconstructed - compare with the crop image"]
        if not b.get("math_fonts"):
            unc.append("no recognisable math font names: bold/calligraphic/blackboard alphabets cannot be recovered from the text layer")
        e = {"block_id": b["id"], "page": b["page"], "bbox": b["bbox"], "number": b.get("equation_number"),
             "text": b["text"], "marked": b.get("marked") or b["text"], "latex": b.get("latex"),
             "unicode_math": b.get("unicode_math"), "math_fonts": b.get("math_fonts"), "rule": b["role_rule"],
             "uncertainty": unc, "crop": None}
        try:
            os.makedirs(eq_dir, exist_ok=True)
            r = fitz.Rect(b["bbox"]) + (-4, -4, 4, 4)
            name = f"eq_p{b['page']}_{b['id'].split('-')[1]}.png"
            doc[b["page"] - 1].get_pixmap(dpi=200, clip=r & doc[b["page"] - 1].rect).save(os.path.join(eq_dir, name))
            e["crop"] = f"equations/{name}"
        except Exception as ex:  # rendering is best-effort
            e["uncertainty"].append(f"crop not rendered: {ex}")
        equations.append(e)
    footnotes = [{"block_id": b["id"], "page": b["page"], "text": norm_ws(b["text"])} for b in ordered_all if b["role"] == "footnote"]
    references = split_references([b for b in ordered_all if b["role"] == "reference"])
    eq_nums = [e["number"] for e in equations if e["number"] and re.match(r"^\d+$", e["number"])]
    for a, c in zip(eq_nums, eq_nums[1:]):
        if int(c) - int(a) > 1:
            warnings.append({"code": "equation_number_gap", "detail": f"equation ({a}) followed by ({c}); an equation may be missed"})

    # ---- paragraphs: merge body blocks split across columns / pages
    paragraphs = []
    for b in ordered_all:
        if b["role"] not in ("body", "abstract"):
            continue
        t = norm_ws(b["text"])
        if paragraphs:
            p = paragraphs[-1]
            same_sec = p["section_id"] == b.get("section_id")
            between = [x["role"] for x in ordered_all[p["last_order"]:b["order"] - 1]]
            skippable = all(r in ("footnote", "caption", "table", "figure") for r in between)
            if same_sec and p["role"] == b["role"] and not SENT_END.search(p["text"]) and re.match(r"^[a-z(\[,;]", t) \
                    and skippable:
                joiner = "" if p["text"].endswith("-") and re.match(r"^[a-z]", t) else " "
                text = (p["text"][:-1] if joiner == "" else p["text"]) + joiner + t
                p["text"] = text
                p["sources"].append({"block_id": b["id"], "page": b["page"], "bbox": b["bbox"]})
                p["continued"] = True
                if between:
                    p.setdefault("skipped_over", []).extend(between)
                p["last_order"] = b["order"]
                continue
        paragraphs.append({"id": f"para{len(paragraphs) + 1}", "role": b["role"], "section_id": b.get("section_id"),
                           "section_path": b.get("section_path"), "text": t,
                           "sources": [{"block_id": b["id"], "page": b["page"], "bbox": b["bbox"]}],
                           "continued": False, "last_order": b["order"]})
    for p in paragraphs:
        p.pop("last_order", None)

    if need_ocr_unresolved:
        status = "partial"
    elif not ordered_all:
        status = "failed"
        warnings.append({"code": "no_text", "detail": "no text extracted from any page"})
    else:
        status = "success"
    if any(w["code"] == "possible_garbled_text" for w in warnings):
        status = "partial" if status == "success" else status

    for b in ordered_all + margin_all:
        b.pop("table_ref", None)
    result = {
        "status": status,
        "meta": meta,
        "warnings": warnings,
        "pages": page_info,
        "ocr_pages": ocr_pages,
        "unresolved_pages": need_ocr_unresolved,
        "sections": sections,
        "blocks": [slim(b) for b in ordered_all],
        "headers_footers": [slim(b) for b in margin_all],
        "paragraphs": paragraphs,
        "tables": all_tables,
        "figures": figures,
        "equations": equations,
        "footnotes": footnotes,
        "references": references,
        "stats": {
            "n_blocks": len(ordered_all), "n_sections": len(sections), "n_tables": len(all_tables),
            "n_figures": len(figures), "n_equations": len(equations), "n_references": len(references),
            "n_footnotes": len(footnotes), "n_headers_footers_removed": len(margin_all),
            "n_dehyphenated": sum(b["n_dehyphenated"] for b in ordered_all),
            "n_cross_block_paragraph_joins": sum(len(p["sources"]) - 1 for p in paragraphs),
            "seconds": round(time.time() - t0, 3),
        },
    }
    write_outputs(result, out_dir)
    if preview:
        render_previews(doc, result, os.path.join(out_dir, "previews"))
    return result


def slim(b):
    out = {k: v for k, v in b.items() if k not in ("lines",)}
    out["n_lines"] = len(b["lines"])
    if b.get("marked") is None:
        out.pop("marked", None)
    return out


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def write_outputs(r, out_dir):
    write_json(os.path.join(out_dir, "doc.json"), r)
    with open(os.path.join(out_dir, "blocks.jsonl"), "w", encoding="utf-8") as fh:
        for b in r["blocks"] + r["headers_footers"]:
            fh.write(json.dumps({k: b.get(k) for k in ("id", "page", "order", "role", "column", "bbox", "section_path", "text", "marked")},
                                ensure_ascii=False) + "\n")
    tdir = os.path.join(out_dir, "tables")
    os.makedirs(tdir, exist_ok=True)
    for t in r["tables"]:
        with open(os.path.join(tdir, f"{t['id']}.md"), "w", encoding="utf-8") as fh:
            fh.write(f"<!-- {t['id']} page {t['page']} bbox {t['bbox']} caption: {t['caption']} -->\n" + table_to_markdown(t) + "\n")
        with open(os.path.join(tdir, f"{t['id']}.html"), "w", encoding="utf-8") as fh:
            fh.write(table_to_html(t) + "\n")
        with open(os.path.join(tdir, f"{t['id']}.csv"), "w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows([[c if c is not None else "" for c in row] for row in t["rows"]])
    # markdown in reading order with anchors
    md = [f"<!-- source: {r['meta']['source_file']} | status: {r['status']} | parser: {r['meta']['parser']} -->"]
    tables_by_block = {}
    for t in r["tables"]:
        tables_by_block.setdefault(t.get("caption_block"), []).append(t)
    emitted_tables = set()
    cur_page = None
    for b in r["blocks"]:
        if b["page"] != cur_page:
            cur_page = b["page"]
            md.append(f"\n<!-- ===== page {cur_page} ===== -->")
        anchor = f"<!-- {b['id']} {b['role']} col={b['column']} bbox={b['bbox']} -->"
        text = b.get("marked") or b["text"]
        role = b["role"]
        if role == "title":
            md.append(f"{anchor}\n# {norm_ws(text)}")
        elif role == "heading":
            sec = next((s for s in r["sections"] if s["block_id"] == b["id"]), None)
            lvl = min(6, (sec["level"] if sec else 1) + 1)
            md.append(f"{anchor}\n{'#' * lvl} {norm_ws(b['text'])}")
        elif role == "table":
            t = next((t for t in r["tables"] if t["page"] == b["page"] and bbox_center_in(b["bbox"], t["bbox"])), None)
            if t and t["id"] not in emitted_tables:
                emitted_tables.add(t["id"])
                md.append(f"<!-- {t['id']} table page={t['page']} bbox={t['bbox']} -->\n" + table_to_markdown(t))
        elif role == "equation":
            md.append(f"{anchor}\n$$ {norm_ws(text)} $$" + (f"  (eq. {b.get('equation_number')})" if b.get("equation_number") else ""))
        elif role == "caption":
            md.append(f"{anchor}\n> **{norm_ws(text)}**")
            for t in tables_by_block.get(b["id"], []):
                if t["id"] not in emitted_tables:
                    emitted_tables.add(t["id"])
                    md.append(f"<!-- {t['id']} table page={t['page']} bbox={t['bbox']} -->\n" + table_to_markdown(t))
        elif role == "footnote":
            md.append(f"{anchor}\n[^fn]: {norm_ws(text)}")
        elif role == "reference":
            md.append(f"{anchor}\n- {norm_ws(text)}")
        else:
            md.append(f"{anchor}\n{norm_ws(text)}")
    for t in r["tables"]:
        if t["id"] not in emitted_tables:
            md.append(f"<!-- {t['id']} table page={t['page']} bbox={t['bbox']} -->\n" + table_to_markdown(t))
    if r["headers_footers"]:
        md.append("\n<!-- ===== removed running headers / footers / margin notes ===== -->")
        for b in r["headers_footers"]:
            md.append(f"<!-- {b['id']} {b['role']}: {norm_ws(b['text'])[:120]} -->")
    with open(os.path.join(out_dir, "document.md"), "w", encoding="utf-8") as fh:
        fh.write("\n\n".join(md) + "\n")
    qa = {"status": r["status"], "warnings": r["warnings"], "unresolved_pages": r["unresolved_pages"],
          "ocr_pages": r["ocr_pages"], "stats": r["stats"],
          "pages": [{k: p.get(k) for k in ("page", "source", "n_columns", "text_layer_chars", "garbled_ratio", "image_coverage")} for p in r["pages"]]}
    write_json(os.path.join(out_dir, "qa_report.json"), qa)


ROLE_COLORS = {"title": (0.8, 0, 0), "heading": (0.9, 0.4, 0), "body": (0, 0.3, 0.9), "abstract": (0, 0.5, 0.5),
               "caption": (0.6, 0, 0.6), "equation": (0, 0.6, 0), "table": (0.5, 0.3, 0), "footnote": (0.4, 0.4, 0.4),
               "reference": (0.2, 0.2, 0.6), "author_affiliation": (0.7, 0.2, 0.4),
               "header": (0.6, 0.6, 0.6), "footer": (0.6, 0.6, 0.6), "margin_note": (0.6, 0.6, 0.6)}


def render_previews(doc, r, pdir, dpi=80):
    os.makedirs(pdir, exist_ok=True)
    tmp = fitz.open()
    tmp.insert_pdf(doc)
    for p in r["pages"]:
        page = tmp[p["page"] - 1]
        for b in r["blocks"] + r["headers_footers"]:
            if b["page"] != p["page"]:
                continue
            col = ROLE_COLORS.get(b["role"], (0, 0, 0))
            rect = fitz.Rect(b["bbox"])
            page.draw_rect(rect, color=col, width=0.8)
            label = f"{b['order'] or '-'} {b['role']}"
            page.insert_text((rect.x0, max(8, rect.y0 - 1)), label, fontsize=6, color=col)
        for t in r["tables"]:
            if t["page"] == p["page"]:
                page.draw_rect(fitz.Rect(t["bbox"]), color=(1, 0.5, 0), width=1.5)
        for g in p.get("column_gutters", []):
            page.draw_line((g, 0), (g, p["height"]), color=(1, 0, 0), width=0.4, dashes="[2] 0")
        page.get_pixmap(dpi=dpi).save(os.path.join(pdir, f"page_{p['page']:03d}.png"))
    tmp.close()


def parse_pages(spec):
    if not spec:
        return None
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--out", required=True)
    ap.add_argument("--password", default=None)
    ap.add_argument("--pages", default=None, help="e.g. 1-3,7")
    ap.add_argument("--ocr", default="auto", choices=["auto", "force", "off"])
    ap.add_argument("--lang", default="eng+chi_sim")
    ap.add_argument("--preview", action="store_true", help="render PNG previews with block boxes")
    a = ap.parse_args()
    r = parse(a.pdf, a.out, a.password, parse_pages(a.pages), a.ocr, a.lang, a.preview)
    s = r.get("stats", {})
    print(json.dumps({"status": r["status"], "out": os.path.abspath(a.out), "error": r.get("error"),
                      "warnings": len(r.get("warnings", [])), **s}, ensure_ascii=False))
    sys.exit(0 if r["status"] in ("success", "partial") else 2)


if __name__ == "__main__":
    main()
