# literature-translator v0.2.0 → v0.3.0 迭代报告

依据：`grading/v0.2.0/GRADE_REPORT.md`（73/100）。所有数字由 `src/reproduce.py` 生成，存于 `execution/results/metrics.json`。复现环境是只装了 `submission/requirements.txt` 的全新 venv（Python 3.13，PyMuPDF 2.1 + numpy 2.5.5，freeze 见 `iteration_v0.3/clean_env_pip_freeze.txt`），exit 0；在开发环境（PyMuPDF 1.28.2）下再跑一次，除计时外 **0 处差异**。用这个干净 venv 从解压后的 zip 重跑也成功。没有运行 submit.ps1；旧包和 `grading/` 均未改动。

## 1. 扫描 PDF：安装与复现

- **修改文件**：`requirements.txt`、`SKILL.md`、`README.md`、`scripts/pdf_layout.py`、`scripts/tr.py`、`scripts/lt_core.py`、`scripts/lt_render.py`、`src/build_bundle.py`；测试：`tests/check_scanned.py`、`tests/scanned/*`
- **修复前**：NumPy 用到了却没声明；转录夹具写死 `Sxxxx` 区域号；整页转录得到的子段只继承整页框。
- **修复后**：在 requirements、SKILL.md 和 ARM 依赖中声明 `numpy>=1.22`。转录定位改为 `@@ p6 [x0,y0,x1,y1] #label`、`@@ p6`，用 `REST:`/`DONE` 收尾，`put` 中可写 `@@ #label`。子段保留自己的框（`location_granularity`）；只能定位到整页时会标出 `page_level_location`。没有 numpy 时每页算一个区域（`granularity: page`），降级原因写入 result.json 和 uncertainty。

| 用例 | 事实保留 | 子段自带框 | 扫描覆盖 |
|---|---|---|---|
| BERT pp.5,6,12，有 numpy | 7/7 | 14 | 已转录 [6]，未开始 [5,12]，待转录区域 63/80 |
| BERT，无 numpy | 7/7 | 14 | 已转录 [6]，未开始 [5,12]，待转录 2/3（整页粒度），降级原因已记录 |
| Hubble p.3，有 / 无 numpy | 7/7 / 7/7 | 6 / 6 | success / success（降级已记录） |

- **本轮新发现并修复**：全新环境会装上 PyMuPDF 2.1，它附带 pymupdf_layout。它的版面模型会把作者块等无表格线的文字识别成表格（GPT‑3 的段数从 839 变成 815，留出段 12/12 误报）。现在 `pdf_layout.py` 在导入时关闭 `_layout_enabled`。修复后 7 篇论文在 1.28.2 与 2.1 下段号和内容完全一致，指标 0 差异。
- **证据**：`execution/results/v0.3/scan_*.json`、`scan_*/result.json`、`iteration_v0.3/metrics_dev_pymupdf1.28.json` 与 `metrics_clean_env_pymupdf2.1.json`
- **剩余缺陷**：转录文字本身是否正确无法机检，只列入人工清单。

## 2. 核验器

- **修改文件**：`scripts/lt_core.py`（数据单位族与数量级、比例窗口内的否定范围、`PARTIAL_NEG`、`subject_binding`、`table_cell_mismatch`、锁定译名出现时禁用变体仍报错、裸节号例外、区间和列表引用、表格按数值比较、区间共享数量级）、`scripts/tr.py`（enforce）；测试：`tests/run_validation_v2.py`、`tests/heldout_mutations.py`、`tests/heldout/attention_p8-9.zh.txt`
- **评分方 v0.2 集**（GPT‑3 pp.10–15）：修复前 11/16，干净段 1/10 报 error；修复后 **16/16**，干净段 0/10 error、0/10 warning。

| 变异 | v0.2 | v0.3 级别 |
|---|---|---|
| unit 10 兆字节→10 兆比特 | 漏 | error unit_changed |
| negation_scope 并非总是→总是 | 漏 | warn negation_scope |
| term 禁用变体（锁定译名也在场） | 漏 | error term_violation |
| semantic 谁容易作答对调 | 漏 | warn subject_binding |
| semantic_cells 人类/基线正确率互换 | 漏 | warn subject_binding |
| 其余 11 项 | 命中 | 同 v0.2（number/crossref/placeholder 为 error，negation/qualifier 为 warn） |

- “as discussed in 2.2”→“如第 2.2 节所述”不再误报，式(7)→表(7) 仍然报 error。
- **冻结规则后的独立复测**（Transformer pp.8–9，从未用于调参）：**17/22 = 77%**（其中 13 项为 error，4 项为 warn）；干净段 0/20 error、0/20 warning。**没有达到 ≥90% 的目标。** 漏报 5 项：TFLOPS→GFLOPS、3.5 天→3.5 小时（单位紧挨公式占位符）、删掉“约 40K”中的“约”、“更大的模型更好→更小的”、仅 WSJ 16K / 半监督 32K 互换。其他留出干净段：GPT‑3 p3–4 0/12、BERT p1–2 0/21。
- **真实长文运行中发现的 4 个误报（复测完成后修复）**：“第 2.1-2.3 节”、“图 3.14 和图 3.15”、表格中的 410 billion = 4100 亿、4-12 billion = 40-120 亿。修复后所有回归集结果不变（16/16、GloVe 14/14、留出 17/22、干净段 0 误报）。
- **enforce 回填**：v0.3 开发版中，锁定译名已在段内出现时 enforce 不替换禁用变体，与核验器结论矛盾。现已修复：替换、rev+1、记入 changes.jsonl（`metrics.v0.3.enforce_backfill`）。
- **仍需人工复核的语义关系**：比较级或方向反转（没有方向词时）、平行条件之间数值互换、非数据量单位（算力、时间）的替换、近似词被删、主体只出现一次的评价反转。`subject_binding` 只报两个主体互相对调的情况，而且只是 warning。
- **证据**：`execution/results/v0.3/checker_validation_v2.json`、`heldout_attention_mutations.json`、`heldout_*_fp.json`、`checker_validation_glove.json`

## 3. GloVe 公式与阅读顺序

- **修改文件**：`scripts/pdf_layout.py`（TeX 扩展字体字形表、`TEXT_CID_FOR_UNKNOWN_UNICODE`、行内与陈列公式判定、上下限挂接、同栏吸收）、`scripts/lt_core.py`（续句规则、行内单行公式并入段落、`formula_fragment`）；测试：`tests/check_glove.py`（表 1 的 12 格、4 处续句、式 (1)–(22) 逐个对照原页）
- **修复前 → 修复后**：表 1 12/12 → 12/12；续句（p2 S0021、p5 S0059–61、S0066–76、S0075）0/4 → **4/4**；式 (1)–(22) 全部通过 22/22 → 22/22，但精确重建从 **7 个升到 18 个**。(9)(15)(20)(22) 是分段函数、对齐多行公式或 ≠ 字形缺失，保留完整裁图和编号，标为 needs_confirmation。
- **证据**：`execution/results/v0.3/glove_check.json`
- **剩余缺陷**：二维公式不重建。

## 4. 合同类型与覆盖率

- **修改文件**：`scripts/lt_render.py`、`tests/check_contract.py`
- **修复前**：`confidence_level: number` 静默变成字符串 "unavailable"。
- **修复后**：该字段为 null，并写入 `contract_gaps`（field/expected/got/reason）；`verification_notes` 仍是 list。扫描覆盖单独放在 `metrics.scan`（已转录页、部分转录页、未开始页、待转录区域、粒度、已译转录段）。BERT 只转录 1/3 页时状态为 partial，原因写成 “scanned pages fully transcribed 1/3”。
- **证据**：`execution/results/v0.3/contract_check.json`

## 5. 长文调度：GPT‑3 真实计时

- **修改文件**：`scripts/tr.py`（resume_state 现在带上 changes.jsonl 和 dropped.jsonl；session 开始时记一条日志）；证据目录：`execution/results/v0.3/gpt3_real_run/`（20 批真实译文、术语表、逐次 put 的计时、`run_report.json`）
- **真实运行**：75 页，预算 900 s（预留 120 s）。20 次 put 后在 **723.3 s** 定稿，状态 partial。

| | v0.2 B1（评分方 Agent 翻译） | v0.3（作者 Agent，Claude Opus 5.5 翻译） |
|---|---|---|
| 用时 | 779.1 s | 723.3 s |
| 已译段 | 114/563 | 373/561 |
| 标题 | 53 | 53/53 |
| 正文段 / 词 | — | 258/375 段（词覆盖 78.6%） |
| 题注 | — | 43/109 段 |
| 表格 | — | 19/24 |
| error / warning | 0 / 13 | 1 / 48（S0074 是真问题：表头“300B”放错了行） |
| 未译清单 | 有 | 188 个段号，含位置和原文摘要，附续译命令 |

- **注意**：两次运行用的翻译 Agent 不同，差距主要来自译者速度，不能归功于 Skill 改动（调度逻辑与 v0.2 相同）。平台 DeepSeek 环境的请求数在本地观测不到：**未测**。
- **真实续译**：从导出的 resume_state 开 session 2，再 put 7 段。之前的 373 段一段未改，rev 和 ts 完全一致；changes.jsonl 共 387 条（session 1 的 379 条 + 1 条 session + 7 条），S0062、S0073、S0207 的修订都还在。
- **本轮新发现并修复**：开发版导出时没把 changes.jsonl 复制进 resume_state，续译后丢了 session 1 的变更历史。
- **复现**：reproduce.py 按相同 put 顺序重放，结果为 373/561、1 error、续译验证通过。原运行的时钟无法重放，记录在 run_report.json 中。操作说明：session 1 的输出目录因我操作失误被重新渲染过一次，elapsed 已从工作目录的状态恢复为 723.3 s。模拟时钟（`sim_longdoc.py`）只用于验证调度的计时逻辑。

## 回归与包

- ResNet p1–2：success，22 段，0 error，变异检出 0.97；Transformer p1–2：success，19 段，0 error，1.0；toy 5/5；zh→en success；4 个异常用例状态如实；两次运行段号哈希一致。
- 术语回填、段号唯一、bilingual.md/html、translation.zh.md、alignment.jsonl 均已检查。
- `literature-translator_arm_v0.3.0.zip`：zip 中每个文件的哈希都与开发目录一致。Skill 共 17 个文件（≤50），最大的是 pdf_layout.py，约 113 KB（≤500 KB），合计约 352 KB（≤2 MB），全部 UTF‑8，没有符号链接。旧包 v0.1.0（e223d643…）、v0.2.0（b5e8bd12…）以及 grading/ 均保留未改。
- 开源仓库：https://github.com/chensuyan407-prog/literature-translator （MIT；Skill 在根目录，复现与回归脚本在 dev/；不含论文 PDF、转录和整篇译文）。已写入 GITHUB_URL.txt。
- 平台提交：attempt 99244 与此前 v0.2 的 97912 都在预检阶段被拒（未进入沙箱，不扣次数），原因是 `resources/glossary_base.tsv` 不属于平台支持的 Skill 资源类型。已改名为 `resources/glossary_base.txt`（内容不变，仍为制表符分隔；加载器兼容旧名），干净环境复现指标 0 差异。
