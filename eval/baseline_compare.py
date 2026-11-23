#!/usr/bin/env python3
"""Compare the text a plain agent typically feeds to a translator (pdftotext / PyMuPDF plain text,
split on blank lines) with the segments produced by `tr.py init`, on the same pages.

Deterministic, no model calls. Measures the structural defects that later become translation errors:
  broken_sentence_boundaries  chunk ends mid-sentence and the next chunk continues it (column/page/float breaks)
  noise_lines_in_text         running headers, page numbers, arXiv stamp, figure labels mixed into the text
  captions_glued_to_body      a Figure/Table caption shares a chunk with body text
  hyphen_line_breaks          words split as "xx-\\nyy"
  formula_fragment_lines      lines made only of symbols/1-2 letters (inline math torn apart)

    python baseline_compare.py --work /app/work/lt --pdf paper.pdf --pages 1-2 [--out compare.json]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import lt_core as C  # noqa: E402

TERM = re.compile(r"[\.\?\!:;。！？：；]\s*[\"'”’)\]]?\s*$")


def baseline_text(pdf, pages):
    if shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-f", str(pages[0]), "-l", str(pages[-1]), "-enc", "UTF-8", pdf, "-"],
                           capture_output=True)
        if r.returncode == 0:
            return r.stdout.decode("utf-8", "replace"), "pdftotext"
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    doc = fitz.open(pdf)
    return "\n\n".join(doc[p - 1].get_text("text") for p in pages), "pymupdf get_text('text')"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    lo, hi = [int(x) for x in a.pages.split("-")]
    pages = list(range(lo, hi + 1))
    text, tool = baseline_text(a.pdf, pages)
    chunks = [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]
    segs = [s for s in C.read_jsonl(os.path.join(a.work, "segments.jsonl")) if set(s.get("pages") or []) & set(pages)]
    dropped = [d for d in C.read_jsonl(os.path.join(a.work, "dropped.jsonl")) if d.get("page") in pages]
    broken = 0
    for x, y in zip(chunks, chunks[1:]):
        if not TERM.search(x) and re.match(r"^[a-z]", y):
            broken += 1
    noise = 0
    for d in dropped:
        t = C.norm_ws(d.get("text", ""))[:40]
        if len(t) >= 3 and t in C.norm_ws(text):
            noise += 1
    capt = 0
    for c in chunks:
        m = re.search(r"(?:^|\n)(Figure|Fig\.|Table)\s*\d+[\.:]", c)
        if m and len(c.split()) > 12 and (m.start() > 0 or not TERM.search(c.splitlines()[0])):
            body_lines = [l for l in c.splitlines() if l.strip()]
            if len(body_lines) > 1 and not re.match(r"^(Figure|Fig\.|Table)", body_lines[0]):
                capt += 1
    hyph = len(re.findall(r"[A-Za-z]{2,}-\n[a-z]", text))
    frag = sum(1 for l in text.splitlines() if l.strip() and len(l.strip()) <= 3 and not l.strip().isdigit())
    joined = sum(1 for s in segs if s.get("joined"))
    rep = {
        "pdf": os.path.basename(a.pdf), "pages": a.pages, "baseline_extractor": tool,
        "baseline": {"chunks": len(chunks), "broken_sentence_boundaries": broken, "noise_lines_in_text": noise,
                     "captions_glued_to_body": capt, "hyphen_line_breaks": hyph, "formula_fragment_lines": frag},
        "skill": {"segments": len(segs), "translatable_segments": sum(1 for s in segs if s.get("translate")),
                  "cross_column_or_page_joins": joined, "layout_noise_removed_and_logged": len(dropped),
                  "captions_as_own_segments": sum(1 for s in segs if s["kind"] == "caption"),
                  "formula_placeholders": sum(sum(1 for k in s.get("ph", {}) if k[0] == "M") for s in segs),
                  "citation_placeholders": sum(sum(1 for k in s.get("ph", {}) if k[0] == "C") for s in segs),
                  "display_equations_kept_verbatim": sum(1 for s in segs if s["kind"] == "equation")},
        "note": "baseline = plain text split on blank lines, i.e. what an agent without the Skill typically translates chunk by chunk",
    }
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if a.out:
        C.write_json(a.out, rep)


if __name__ == "__main__":
    main()
