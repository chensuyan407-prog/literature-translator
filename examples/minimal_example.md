# Minimal example (示例 — toy input shipped with the Skill, not an official case)

Run from the Skill directory:

```bash
python scripts/tr.py --work /tmp/lt init --source examples/sample.md --out /tmp/lt_out
python scripts/tr.py --work /tmp/lt glossary examples/sample_glossary.txt
python scripts/tr.py --work /tmp/lt batch
python scripts/tr.py --work /tmp/lt put examples/sample_translation_with_errors.zh.txt --verbose   # a flawed first draft
python scripts/tr.py --work /tmp/lt put examples/sample_translation.zh.txt                          # corrected
python scripts/tr.py --work /tmp/lt finalize
```

`batch` output (actual, abridged):

```
=== BATCH 1: S0001..S0006 (6 segments, ~61 units; 0 units left after this) ===
LOCKED TERMS (must use exactly): layer normalization => 层归一化; residual connection => 残差连接; training loss => 训练损失; sub-layer => 子层; baseline => 基线
@@ S0003  [paragraph · line 5 · L5 · § 1 Introduction]
We add a residual connection {{C1}} around each sub-layer, followed by layer normalization. The model does not use recurrence.
@@ S0004  [paragraph · line 7 · L7 · § 1 Introduction]
As shown in Fig. 2, the training loss drops below 0.15 after at least 20 epochs, which is 3.5× faster than the baseline in Table 1.
@@ S0006  [paragraph · line 11 · L11 · § 2 Method]
Each layer computes {{M1}}, where the residual connection keeps the gradient path short (see Section 2.1).
   (math: M1=$y = x + f(x)$)
```

`put` on the flawed draft (`残余连接`, negation dropped, `图 3` instead of `图 2`, "at least" dropped) — actual output:

```
stored 2 segment(s); coverage 2/6; errors in this put: 3, warnings: 3
MISSING from last batch (still untranslated): S0001, S0002, S0005, S0006
  S0003 [warn] negation_lost: source has 1 negation cue(s) (not); translation has none
  S0003 [error] term_violation: term 'residual connection' must be rendered as '残差连接' (found forbidden '残余连接')
  S0004 [error] number_missing: number '2' from the source is not in the translation
  S0004 [warn] number_added: number '3' is in the translation but not in the source
  S0004 [error] crossref_mismatch: 'Fig. 2' has no matching reference in the translation
  S0004 [warn] qualifier_check: 'at least' — no corresponding qualifier/connective found; confirm the limiting/logical relation is kept
```

After the corrected `put` and `finalize`:

```
FINAL status=success (all in-scope segments translated; no hard-check errors (warnings may remain))
coverage=1.0 term_consistency(locked)=1.0 placeholders=1.0 numbers=1.0 crossrefs=1.0 errors=0 warnings=0
```

`result.json` excerpt (shape; values from this toy run):

```json
{"status": "success",
 "segments": [{"id": "S0003", "kind": "paragraph", "location": {"line": 5, "section": "1 Introduction"},
               "status": "translated",
               "source": "We add a residual connection [3] around each sub-layer, followed by layer normalization. The model does not use recurrence.",
               "translation": "我们在每个子层周围添加残差连接 [3]，随后进行层归一化。该模型不使用循环结构。",
               "protected_tokens": {"C1": "[3]"}, "issues": null, "revisions": 2}]}
```

A full run on a real paper (ResNet p.1–2, example case built by the author) is in the submission
bundle under `execution/results/case_example/output/`.
