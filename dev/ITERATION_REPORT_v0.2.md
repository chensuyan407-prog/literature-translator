# literature-translator v0.1.0 → v0.2.0 迭代报告

依据：`grading/GRADE_REPORT.md`（独立评分 55/100）。所有“修复前”数字均用原包 `grading/pkg/submission`（v0.1.0，与原 zip 哈希一致）
在同一输入上重跑得到，“修复后”为本目录 `submission/`（v0.2.0）。路径相对于作品所在文件夹（含 `grading/`、`iteration_v0.2/`、
`literature-translator/`）。平台三例、DeepSeek 实际运行与 Linux 部署均**未测**。

## 1. 公式与表格解析（`scripts/pdf_layout.py`）

改动：题注锚定的行框表格重建（一行框 = 一单元格，表头留在表内，列由数值行的 x 区间合并得到）；行级陈列公式识别
（数学字体 + 公式编号 + 垂直相邻，图形区域内的标签排除），按字形位置重排上下标、用矢量分数线恢复分子分母；`�` 字形
显示为 `⟨?⟩`；无法可靠恢复的公式 `eq_status=needs_confirmation`，附页码、bbox 与局部图 `crops/Sxxxx.png`。

| 验收项（GloVe） | v0.1 | v0.2 | 证据 |
|---|---|---|---|
| 表 1 的 12 个数据格（含 6.6×10⁻⁵、7.8×10⁻⁴、8.5×10⁻²） | 网格 7 列，仅第 1 数据列 3/12 在正确位置 | 12/12 | `iteration_v0.2/item1_glove/glove_check_v0.1.json`、`glove_check_v0.2.json`、`table1_S0023.png` |
| 表头 “Probability and Ratio / k = solid …” | 拼入正文 S0022 | 在表内，5 列正确 | 同上 |
| p.2→p.3 条件概率定义续句 | 被表头打断 | “…appear in the context of word i.” 完整，pages [2,3]，逐块 boxes | 同上 |
| 式 (1) | 只剩 “, (1)” | `F(w_{i},w_{j},w̃_{k}) = P_{ik}/P_{jk}`，与原页一致 | `eq1_S0026.png` |
| 式 (2) | 只剩 “. (2)” | `F(w_{i} − w_{j},w̃_{k}) = P_{ik}/P_{jk}`，与原页一致 | `eq2_S0028.png` |
| 式 (8) | 未作为公式出现（碎片散在正文） | 标 needs_confirmation（大求和号与括号无 Unicode），附完整局部图 | `eq8_S0041.png` |
| p.3–4 全部陈列公式 | 碎片化 | 11 个：(1)(2)(5)(6)(7) 重建；(3)(4)(8)(9)(10)(11) 待确认并附图 | `execution/results/v0.2/glove_check.json` |

回归：ResNet p1–2 分段与 v0.1 完全一致（24 段，源文逐段相同）；Figure 2 内的框图标签曾被误判为公式，已修复（图形区域内无编号的公式块归为图内文字）。

## 2. 扫描 PDF 进入翻译流程（`tr.py`、`pdf_layout.py`、`SKILL.md`）

改动：无文字层且无 OCR 的页，每个版面区域生成 `image_region` 段（页码、bbox、`crops/Sxxxx.png`、预览图上标段号）；
`tr.py regions` 列出待转录区域；`tr.py transcribe` 回填转录（`@@ S0101..S0103` 一次覆盖相邻区域、`[table]` 等类型、
`UNREADABLE:`/`NONTEXT:` 标注、`--by agent_vision|ocr|human`），转录内容拆为子段 `S0101.1…` 后照常 batch/put/核验；
人工清单列出每个转录段（需对图复核）与未转录/无法辨认区域；只列出范围内页面（v0.1 对 3 页任务列出全部 16 页）。

| 验收项 | v0.1 | v0.2 | 证据 |
|---|---|---|---|
| BERT 扫描版 p.6 GLUE 表（双层表头：任务名 + 训练样本数） | 0 段可译，failed | 表格 7 行 × 10 列转录并翻译；79.6、82.1、392k、MNLI-(m/mm) 等保留，正文 80.5 对 72.8 保留；0 error 0 warning | `execution/results/v0.2/scan_bert_p6/`（result.json、bilingual.md、crops/） |
| BERT p.6 右栏 5 个区域 | — | 有意未转录，逐个列为 needs_transcription（S0113–S0117），status=partial | 同上 `result.json` → `unreadable_or_pending` |
| Hubble 1929 印刷页 170（PDF p.3） | 0 段可译，failed | 24 OBJECTS / 9 GROUPS 两组：K = +465 ± 50、+513 ± 60 km/sec per 10⁶ parsecs；V₀ = 306、247 km/sec；全部保留，0 error | `execution/results/v0.2/scan_hubble_p170/` |
| 无法辨认区域 | — | 本两页无真正无法辨认的区域；页眉页码以 NONTEXT 标注；跨页断词 “par-” 以 ⟪?⟫ 标出并进入人工清单 | 同上 `manual_review.md` |

说明：转录由开发代理（具视觉能力）完成，记录为 `source_origin=agent_vision`；转录正确性无法机检，已列入人工清单。
平台 DeepSeek 若无视觉能力，这些区域会保持 needs_transcription（有 tesseract 时自动 OCR）。

## 3. 核验器（`lt_core.py`、`eval/mutation_test.py`）

改动：数量级等价（6 billion = 60 亿 = 6B，1.28 million = 128 万）；单位被替换报 `unit_changed`；计数名词（tokens/documents…）；
交叉引用反向检查（译文中的 表（7） 在原文必须有 Table 7）；否定词计数与句级作用域；only 不再接受单独的“才”；新增全称量词；
方向词；表格/公式不做句级启发式；作者-键引用 `[VSP+17]` 保护修复。

| 集合 | v0.1 | v0.2 | 证据 |
|---|---|---|---|
| 评分方 14 项定向变异（逐项见下） | 9/14 | 14/14 | `iteration_v0.2/item3_checker/validation_v0.1.json`、`validation_v0.2_final.json` |
| 7 段干净对照 | 0 error、2 warning（60 亿） | 0 error、0 warning | 同上 |
| 留出集 1：GPT‑3 p3–4 共 12 段（规则冻结后首次测量） | 1 段 error（15 亿）、1 段 warning | 0 error、1 段 warning（polarity：increasingly→越来越）；修正后复测 0/0 | `heldout1_gpt3_v0.1.json`、`heldout1_gpt3_v0.2-frozen.json`、`heldout_gpt3_v0.2_final_fp.json` |
| 留出集 2：BERT 文字版 p1–2 共 21 段（在上述修正之后才首次测量） | 0/0 | 0 error、0 warning | `heldout2_bert_v0.1.json`、`heldout_bert_v0.2_final_fp.json` |
| 留出集变异检出 | — | GPT‑3 77/86，BERT 113/114 | `mutation_v0.2_heldout_*.json` |

14 项逐项（v0.1 → v0.2）：number 命中→命中；unit 漏→命中（counted_noun_changed）；crossref 命中→命中；crossref_kind（式→表）漏→命中
（crossref_kind_mismatch error）；math/cite drop/dup 4 项命中→命中；negation 漏→命中（negation_count）；qualifier 漏→命中（qualifier_check +
quantifier_added）；term、half、swapped 命中→命中；semantic（权重方向反转）漏→命中（polarity_check：overweighted 无对应）。
注意：semantic 的命中依赖方向词启发式，不代表能发现一般的语义反转。

## 4. 长文预算与交付（`tr.py`、`lt_render.py`）

改动：按本次运行实测吞吐（已译单位/已用秒）预测；批量按剩余时间缩放；预测超时时先一次译完全部标题；保留 120 s 收尾，
放不下下一段即 STOP；result.json 给出正文/标题/题注/表格分类覆盖率、未译段号、`resume_state/` 与 `session` 续译命令；
output_contract 按声明类型强制并记录 `contract_type_coercions`；`location.boxes` 逐块定位与 `page_breaks`。

| 验收项（GPT‑3 75 页，900 s，模拟时钟，同一吞吐假设） | v0.1 | v0.2 | 证据 |
|---|---|---|---|
| 定稿时间 | 1172 s 仍在批量翻译（无停止机制） | 802 s 定稿（792 s STOP） | `iteration_v0.2/item4_longdoc/sim_v0.1_log.json`、`sim_v0.2_log.json` |
| 标题 / 正文 / 题注 / 表格覆盖 | 15/53、75/390、12/109、5/24（无分类报告） | 53/53、50/377、5/109、1/24（result.json 内置） | 同上；`execution/results/v0.2/gpt3_longdoc_sim.json` |
| partial + 未译段号 + 续译 | partial、无续译信息 | partial、454 个未译段号、`session` 续译实测从 S0071 继续 | 同上 |
| verification_notes 要求 list | 交付 dict | 交付 list | `contract_types_v0.1.json`、`contract_types_v0.2.json` |
| 8 个契约字段类型（含 qa_checks:list、glossary_table:object） | 3 个类型不符 | 8/8 正确，记录 2 次转换 | 同上 |
| 跨页段落逐块定位 | 只有首块 bbox | 每块 {block, page, bbox} + page_breaks（如 S0036 p3->p4） | `execution/results/v0.2/…/result.json` |

说明：时间是模拟的（state.started 平移；14 s/请求 + 6 词/秒）；45 段使用评分方 B 组的真实译文，其余为标注 “〔SIM 模拟译文〕” 的占位
文本，只用于测调度与记账，不代表翻译质量。v0.2 覆盖正文少于 v0.1 是因为把预算优先给了全部标题并在预算内停止。

## 5. 回归

`src/reproduce.py`（38.8 s）：toy 例 5 类注入全部检出；ResNet 22/22、Attention 19/19（与 v0.1 相同，0 error）；ResNet/Attention 变异
164/169、113/113（新增变异类型后的分母）；中译英成功；加密 → failed；无文字层无 OCR → partial（v0.1 为 failed，v0.2 改为待转录）；
无 PyMuPDF → pdftotext 回退；缺源文件 → failed；分段确定性一致。完整输出：`execution/results/metrics.json`、`execution/run.log`。

## 6. 未测项与剩余缺陷

- 未测：平台 3 个正式案例、DeepSeek 实跑（请求数、token、真实耗时）、Linux `/input`→`/app/output` 部署、tesseract OCR 路径（本机无 tesseract）。
- 公式：大求和/积分号、矩阵、分段函数、多行对齐不重建，只标待确认；行内公式中的大运算符显示为 `⟨?⟩`。
- 表格：依赖“Table N”题注与“一行框一单元格”；无题注表格、单元格内换行的复杂表头、跨页续表仍走旧的 find_tables 逻辑。
- 扫描：XY-cut 区域边界会切开表格（需 `..` 范围转录）；转录错误无法机检。
- 核验：同一名词多次出现时只替换一处不报（noun_swap 0/1、ResNet 3 例）；半段漏译在长度比正常时漏报（GPT‑3 4/11）；
  另有否定词时删掉其一可能漏报；方向词启发式只覆盖有限词表；一般语义错误仍需人工。
- GitHub 公开链接仍未发布（GITHUB_URL.txt 为 NOT_YET_PUBLISHED）。
