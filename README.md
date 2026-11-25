# literature-translator 0.3.2 — 术语锁定 · 段落对齐 · 可自检的科研文献翻译 Skill

面向“阅读03｜文献翻译”。Agent 只负责“翻译文字”（以及扫描页的读图转录）；分段、公式/表格重建、引用保护、术语强制、
核验、时间预算、版本记录与交付文件全部由脚本完成，可重复运行、可回到原文、可机器复核。

## 1. 解决的失败模式

| 失败模式（直接 pdftotext + 让模型翻译） | 本 Skill 的处理 |
|---|---|
| 跨栏/跨页/被图表打断的段落被切开 | 按阅读顺序重排双栏；段尾未完句时跨过题注/脚注/表格续接；`location.boxes` 逐块给出页码与 bbox，`page_breaks` 标出 p2->p3 |
| 浮动表的表头被拼进正文，科学计数法单元格被拆开 | 以 “Table N” 题注为锚，按行框重建表格（一行框 = 一单元格，表头留在表内）；GloVe 表 1 的 12 个数据格全部正确 |
| 陈列公式碎成多段、分式丢分母 | 行级识别陈列公式区域，按字形位置重排上下标，用矢量分数线恢复分子/分母；不能可靠恢复的（大求和号无 Unicode、多行极限、分段函数）标 `needs_confirmation` 并附局部图 |
| 扫描件无文字层 → 零译文 | 每个版面区域生成带页码/bbox/局部图的段；`tr.py transcribe` 回填视觉/OCR/人工转录（支持跨区域表格、UNREADABLE、NONTEXT），之后正常翻译与核验 |
| 术语前后不一 | 可执行术语表：锁定译名、禁用变体、`enforce` 全篇回填，中途改名自动把旧译名变成禁用变体 |
| 公式、引用编号丢失或错位 | `{{M1}}`/`{{C1}}`/`{{U1}}` 占位符；作者-键引用 `[VSP+17]`、上标引用、作者-年份引用均受保护 |
| 数字、单位、计数名词、图表编号译错 | 数字多重集（含 6 billion = 60 亿、1.28 million = 128 万）；单位与“6 billion tokens”这类计数名词；图/表/式/节编号双向检查（式（7）→表（7）报错） |
| 否定、限定、逻辑关系丢失 | 否定词计数 + 句级作用域；only/仅、只有……才；新增“普遍/所有”类全称量词；方向词（small/large、increase/decrease、overweight） |
| 长文超时、漏译不报 | 实测吞吐调度：批量按剩余时间缩放，超预算时先译全部标题，预留 120 s 收尾并 STOP；按正文/标题/题注/表格分别报告覆盖率；`resume_state/` + `session` 续译 |
| 契约字段类型不符 | 按 `output_contract` 声明的类型强制（如 `verification_notes: list`），转换记录在 `contract_type_coercions` |

## 2. 安装与依赖

- Python ≥ 3.9；`PyMuPDF>=1.23`、`numpy>=1.22`（`pip install -r requirements.txt`）。无 PyMuPDF 时自动退回 poppler `pdftotext`（无 bbox，结果中注明）；无 numpy 时扫描页不能切分版面区域，整页作为一个区域，转录用“页码+框”定位仍保留各子段的 bbox，结果中记录降级原因。
- 可选：`tesseract`（含 `chi_sim`）做扫描页 OCR；`openpyxl` 读取 .xlsx 术语表。无网络依赖，不调用外部 API。
- 文件：`SKILL.md`、`scripts/tr.py`（CLI）、`scripts/lt_core.py`（分段/保护/术语/核验）、`scripts/lt_render.py`（交付文件）、
  `scripts/pdf_layout.py`（版式解析，源自作者此前的 pdf-evidence-reader 0.2，本版新增行级陈列公式、题注锚定表格、数学字体标记）、
  `resources/glossary_base.txt`、`eval/`（变异测试、基线对比、评分表）、`examples/`。

## 3. 使用

```bash
T=scripts/tr.py
python $T init --source paper.pdf --pages 1-4          # 或 --case /input/case.json；--pages 5,6,12；--sections "Introduction..Conclusion"
python $T regions --page 6                              # 扫描页：列出待转录区域与局部图
python $T transcribe regions.txt                        # "@@ S0101..S0103" + 原文；或 "UNREADABLE: 原因" / "NONTEXT: 页眉"
python $T glossary my_terms.txt                         # "residual learning => 残差学习 ! 剩余学习"
python $T batch                                         # 打印下一批（按剩余时间自动缩放）
python $T put b01.txt --next                            # 存译文 → 逐段核验 → 刷新全部输出 → 打印下一批
python $T check | issues | show S0012 | enforce | status
python $T finalize --set answer="..."                   # 补齐 output_contract 中未自动映射的字段
python $T --work out/resume_state session --budget 900  # 新会话续译
python $T audit other.md                                # 对任意外部译文做文档级核验
```

译文/转录文件格式：`@@ S0012` 一行，下接内容；转录可用 `@@ S0101..S0103` 一次覆盖相邻区域，首行 `[table]`/`[equation]`/`[footnote]` 指定类型。
术语表格式：`src => tgt ! 禁用1|禁用2  # 备注`；TSV、CSV、JSON、Markdown 表格、xlsx 均可；`=> =` 表示保留原文。优先级：任务术语表 > Agent > 基础表。

## 4. 段号与对齐约定

- 段号 `S0001…` 按阅读顺序编号；同一输入重复运行段号一致。扫描区域转录后的子段为 `S0101.1`、`S0101.2`…，紧随父区域。
- `kind`：title, heading, paragraph, caption, footnote, table, equation, reference, front_matter, image_region。
- `status`：translated / untranslated / kept_original（附原因）/ needs_transcription / unreadable / out_of_scope。
- 未译与存疑：范围内未完成的段在所有输出中显示“【未译】”并列入 `untranslated`；扫描区域未转录显示“【待转录】”；`⟪?⟫` 标记的存疑处进入人工清单。
- 参考文献、陈列公式、作者单位、纯数字表格、页眉页脚类区域保留原文。

## 5. 核验规则（`lt_core.check_segment`）

error：占位符缺失/多余；数字缺失；图/表/式/节编号缺失或类型不符（`crossref_kind_mismatch`，“如第 2.2 节所述”这类裸节号不误报；“Figures 3.14 and 3.15”“Sections 2.1-2.3”按列表/区间处理）；单位被换成另一单位（含数据量 byte/bit 与数量级，`unit_changed`）；锁定术语违例（禁用变体即使与锁定译名同时出现也报）；表格数字换行/换列（`table_cell_mismatch`，按数值比较：410 billion = 4100 亿）；与原文相同；空译文；表格行/列数不符；不同原文译文完全相同。
warning：数字次数不符或新增数字；单位缺失；计数名词被替换（`counted_noun_changed`）；否定词丢失/减少/句级错位（`negation_lost/count/scope`，含“并非总是→总是”这类部分否定）；限定词缺失；新增全称量词（`quantifier_added`）；方向词缺失（`polarity_check`）；主体与数值/评价对调（`subject_binding`，只报两主体互换）；长度比、句数、残留英文；基础术语违例。
表格与公式不做句级启发式检查；小整数允许中文数字；带数量级的数字按数值比较（区间两端共享数量级：4-12 billion = 40-120 亿）。

## 6. 实测（`src/reproduce.py` 可复现；均非平台正式案例）

| 测试 | v0.2 | v0.3 |
|---|---|---|
| GloVe 表 1（12 格）/ 跨栏与行内公式续句（p2 S0021、p5 S0059–61、S0066–76、S0075） | 12/12；4 处续句 0/4 | 12/12；4/4 |
| GloVe 式 (1)–(22)：与原页一致或标待确认附图 | 22/22，其中仅 7 个精确重建 | 22/22，18 个精确重建；(9)(15)(20)(22) 待确认 |
| 评分方 v0.2 集（GPT‑3 pp.10–15，16 项定向变异 / 10 段干净） | 11/16；1/10 段 error | 16/16；0/10 error、0/10 warning |
| 冻结规则后留出复测（Transformer pp.8–9，22 项变异 / 20 段干净） | — | 17/22（13 项为 error）；0/20 error、0/20 warning；未达 90% 目标 |
| 其他留出干净段（GPT‑3 p3–4 12 段、BERT p1–2 21 段） | 0 error | 0 error、0 warning |
| 扫描 BERT pp.5,6,12 / Hubble p.3（有 numpy、无 numpy 各一次） | 子段只继承整页框 | 4 次均 7/7 事实；子段各自带框；无 numpy 时整页一区、降级原因写入 result.json；未开始的页单列 |
| output_contract 类型 | confidence_level 静默为字符串 | 无法转换时为 null 并列入 contract_gaps |
| GPT‑3 75 页真实计时（900 s，真实译文） | 779 s，114/563 段（评分方 Agent 翻译） | 723 s，373/561 段，标题 53/53；作者 Agent（Claude Opus 5.5）翻译，速度差异主要来自译者；续译 1 次：373 段未改动、变更日志连续 |

局限（已知、如实）：留出集仍漏报算力/时间单位替换（TFLOPS→GFLOPS、天→小时）、删掉“约”、比较级反转（更大→更小）、平行条件间数值互换；
语义反转只在主体互换或表格单元格可定位时报出，其余需人工；大求和号、分段函数、对齐多行公式不重建，只标待确认；扫描页转录的正确性无法机检（列入人工清单）；
平台 DeepSeek 环境的请求数在本地无法观测（未测）。

## 7. 维护

- 新学科：在 `resources/glossary_base.txt` 追加无歧义术语，或随任务提供术语表。
- 新检查：在 `lt_core.check_segment` 中添加，并在 `eval/mutation_test.py` 加入对应注入类型，用未参与调参的段落复验误报。
- 版本：0.3.1（2026-11-25）。作者：chensuyan407（Claude Code + Antigravity 协助开发与优化）。
