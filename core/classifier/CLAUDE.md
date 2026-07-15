# core/classifier/ 关键点（阶段5实现，阶段N做了目录拆分+规则注册表重构）

`classify_issues(result: ProofreadResult, parsed: ParsedDocument, chunked: ChunkedDocument | None = None) -> ClassifiedResult`是唯一对外入口：对阶段4产出的每条 `RawIssue` 做系统层强制归层校验，不信任 LLM 自报的 `category`/`confidence`，产出 `ClassifiedIssue`（带 `layer`/`priority`/`layer_notes`，并保留 `llm_category`/`llm_confidence`/`original_suggestion` 供阶段7追问溯源）。纯规则逻辑，不调用LLM。**`chunked` 是真实使用中发现分块边界误判问题（规则H，见下方补丁段落）后追加的可选参数**，不传时（如离线用 `--from-json` 数据反复调规则）规则H不生效，其余规则不受影响，向后兼容阶段5原始签名。

## 目录拆分

- `_types.py`：`ClassifiedIssue`/`ClassifiedResult`/`_ClassificationState`。
- `heuristics.py`：引文/事实文本特征启发式（`_has_quotation_feature`/`_has_factual_feature`），基础规则A/B的漏报兜底能力落点。
- `base_rules.py`：基础归层规则A/B/E/F（`_rule_quotation`/`_rule_factual`/`_rule_style`/`_rule_default`，`_BASE_RULES` 元组）。
- `modifier_rules.py`：修饰规则C/D/G/H+优先级覆盖（`_MODIFIER_RULES` 显式有序注册表，见下方"规则注册表重构"）。
- `postprocess.py`：跨块去重/排序/统计（`_dedup`/`_sort`/`_compute_stats`）。
- `__init__.py`：`classify_issue()`/`classify_issues()` 编排入口，对外只暴露 `ClassifiedIssue`/`ClassifiedResult`/`classify_issue`/`classify_issues`（`__all__`）。

原本是单文件 `core/classifier.py`（484行，core/下最大单块），拆分前先做了下面的"规则注册表重构"（顺序不能反——复杂度根源是规则叠加顺序而非文件长度本身，见下一节），再按 `core/parser/`/`core/chunker/` 先例拆目录，两步都是纯粹的职责重排、无逻辑变更。`tests/test_stage5.py` 全部23个 `classify_issue()`/`classify_issues()` 调用点都走公开API，拆分/重构均零测试改动。

## 规则注册表重构（阶段N，先于目录拆分完成）

原实现是 `classify_issue` 里手写的四行连续函数调用，每个函数签名都不一样（有的碰priority有的不碰），叠加顺序完全靠这四行的书写顺序隐式表达，没有任何地方声明"规则G必须在H之前跑"。改成 `_ClassificationState` 统一状态载体 + `_MODIFIER_RULES` 显式有序注册表：所有修饰规则（含原来连名字都没有、也不往 `layer_notes` 留痕的优先级覆盖，现已补上记录）统一签名 `(raw, block, tail_blocks, state) -> state`，想知道叠加顺序看 `modifier_rules.py::_MODIFIER_RULES` 这一个列表就够了，不用通读整个函数体；以后加新规则只是往列表里插一条并写清楚为什么插在这个位置。基础规则（A/B/E/F）不受影响：那部分本来就是"元组+for循环+first-match"，已经足够清晰，没有做同样的改造。

这次重构是在"其他并发session也在改动本仓库其他文件"的约束下完成的：改动范围严格限定在 `core/classifier.py`（当时还是单文件）内部，未触碰任何测试文件（`tests/test_stage5.py` 的23处调用点全部走公开API，grep确认零私有函数依赖后才动手），也未影响同期另一个session往 `core/chunker.py` 追加的table-skip补丁。

## 归层结构：基础规则 + 修饰叠加两层，不是六条规则简单互斥

这是相对 `prompt/阶段5提示词_结果分层.md` 字面顺序描述的一处解读，已在实现时确认：

1. **基础归层**（互斥，先命中先生效）：规则A引文保护 → 规则B事实置信度 → 规则E风格 → 规则F默认兜底（必命中）。
2. **修饰叠加**（在基础归层结果之上，各自独立判断是否命中）：规则C——block的 `ocr_confidence` 低于 `config.OCR_CONF_DOWNGRADE_THRESHOLD`（默认0.90）且 `issue_type` 属于"错别字与拼写"/"标点符号问题"时，把已判定的"确定性错误"降级为"存疑待核实"（OCR可能认错字，不代表原文真错）；规则D——`located=False` 时同样把"确定性错误"封顶降级为"存疑待核实"。
3. 最后叠加优先级覆盖：`issue_type` 属于"政治敏感性表述"/"民族与地名规范"，无论前面落在哪一层，`priority` 强制改为'高'。

判定原因：提示词原文里规则C/D的处理措辞是"**原**layer若为确定性错误→降级"、"**最高只能到**存疑待核实"，这类表述预设"已经有一个layer存在"，是修饰而非独立分支——按字面顺序当六条互斥规则逐条测试会导致C/D的命中条件（OCR置信度、located字段）永远无法与A/B/E已经命中的情况共同生效。`tests/test_stage5.py::test_rule_c_stacks_on_top_of_base_rule_f` 专门验证这一叠加行为（F判定确定性错误后被C的OCR降级修饰，`layer_notes` 里两条依据都保留）。

其余启发式兜底规则（弥补LLM未自报 `category` 或 `issue_type` 判断不准的漏报场景，均在 `config.py` 里可调，宁可漏判不复杂化）：规则A额外识别书名号《》、≥10字的成对引号、文言虚词密度；规则B额外识别人名职务/机构名关键词+年份格式的"改写型"建议。**这两条兜底能力是设计铁律第2、3条"系统层兜底、不能只信LLM自报"的直接体现**——`tests/test_stage5.py::test_rule_a_book_title_heuristic_overrides_llm_miss` 就是验证"LLM没自报quotation，系统仍强制保护"的核心断言。

跨块去重（`postprocess.py::_dedup`）按 `(block_index, 归一化original_text)` 分组，保留更保守的一条（保守度：引文类>存疑待核实>风格可选>确定性错误，风格可选的相对位置是提示词未明确、按"不涉及对错、影响介于两者之间"做的合理假设）；`located=False` 的条目没有可靠 `block_index`，不参与去重、原样全部保留。排序直接按 `block_index` 升序（`block_index` 本身已是阶段2按阅读顺序分配的全局序号，天然满足"页码升序,同页按block_index"，未定位条目排最后）。`stats` 字段名（`total_issues`/`count_confirmed`/`count_doubtful`/`count_quotation`/`count_optional`/`high_priority_count`）与 `db/database.py` 里 `records` 表列名逐一对齐，供阶段6/8直接写库。

**相对旧版 `core/classifier.py` 占位签名的偏差**：阶段1随手写的 stub 是 `classify_issue(raw_issue: dict) -> dict`（单条、字典进字典出），全仓库无引用，不构成既定现实。按阶段5提示词实现为 `classify_issues(result: ProofreadResult, parsed: ParsedDocument) -> ClassifiedResult`（批量、dataclass），内部按条处理的辅助函数也叫 `classify_issue`，但改成 `(raw: RawIssue, block_by_index: dict) -> ClassifiedIssue`，与阶段4 `RawIssue` 的 dataclass 风格保持一致。

调试用 `tools/preview_classify.py <file> [--max-chunks N]`（全链路，耗额度）或 `tools/preview_classify.py <file> --from-json <path>`（复用 `tools/run_proofread.py --save-json` 存的 `RawIssue` 数据离线反复调规则，不耗额度，仍需原文件路径以便重新 `parse_document` 拿 `ParsedDocument` 供规则C查OCR置信度）。

## 补丁：规则G——知识时效性误判豁免

真实使用中发现后追加，非阶段5原始设计。现象：LLM可能仅因为文中某个年份超出了它的训练数据覆盖范围就产生怀疑（"这个日期看起来太新/我没见过"），但这种怀疑**只针对"这个时间点本身是否存在"，不针对"该时间点发生的事是否属实"**——是模型知识时效性局限造成的误判，不是真正的事实性错误，且这类问题在真实文档里出现频率不低，值得单独处理。

判定条件（`modifier_rules.py::_apply_recency_downgrade`，作为第三个"修饰"叠加在OCR降级、未定位封顶之后）：`issue_type`/`category` 落在事实类范围内 **且** `reason`/`suggestion` 命中 `config.RECENCY_DOUBT_KEYWORDS`（"训练数据""知识截止""较新"等怀疑"时间太新"的措辞，不是泛泛的事实怀疑）**且** 原文里最大的年份落在 `[config.LLM_KNOWLEDGE_CUTOFF_YEAR, +config.KNOWLEDGE_CUTOFF_GRACE_YEARS]` 区间内——超过宽限期（默认10年）视为真的离谱，不豁免，保留原判定供人工核实。命中后**不是直接从结果里剔除**，而是强制降级为"存疑待核实"+最低优先级（`PRIORITY_LOW`），并在 `suggestion` 前缀说明"疑似因AI知识时效性产生的误判，建议直接忽略"、`layer_notes` 记录判定依据——选择降级而非剔除，是为了保留可审计性：用户在界面上一眼就能判断要不要忽略，即使关键词误伤了真正的事实性错误，信息也不会凭空消失。`config.LLM_KNOWLEDGE_CUTOFF_YEAR` 是按当前配置模型（`qwen3.6-plus`）拍脑袋定的初值，需要按真实知识截止时间校正；`RECENCY_DOUBT_KEYWORDS` 同样是初版关键词表，宁可漏判不复杂化，后续按真实LLM措辞调整。测试见 `tests/test_stage5.py` "规则G" 一节四条用例（容忍窗口内降级、超出宽限期不豁免、无"时效性"措辞不触发、非事实类issue_type不触发）。

## 补丁：规则H——分块(chunk)边界截断误判豁免

真实使用中发现后追加，非阶段5原始设计，和规则G是同一批真实文档反馈里发现的两个独立问题，不要混淆。现象与规则G表面相似（都是"看起来不完整/存疑"），但根因完全不同：真实文档里一段用悬挂缩进排版的列表条目（如"分类标签、\n浏览量、发布时间..."）被PyMuPDF在提取阶段拆成了两个独立 `ParsedBlock`（这本身是`core/parser/native_pdf.py`的一处已知不精确之处，几何上很难用一般规则可靠合并回一个block，见 [core/parser/CLAUDE.md](../parser/CLAUDE.md)），又恰好被 `core/chunker/` 的贪心装填算法切在两个不同的chunk里——前一个block变成chunk K正文的最后一块，后一个block变成chunk K+1正文的第一块。`config.OVERLAP_BLOCKS` 的"重叠区"设计只让chunk K+1向后携带chunk K的尾部内容供理解上文，从未设计"向前预览下一chunk"，所以LLM校对chunk K时，看到的正文原文就在"...分类标签、"处硬生生截断，将"分块导致看不到后续"误判成"内容不完整、标点缺失"——这是LLM基于它能看到的（不完整的）内容做出的合理判断，不是它凭空捏造错误，因此不能靠改prompt单方面指望LLM"猜到"自己看漏了内容，需要系统层能确定性地知道"这个block在这个位置就是被截断了"。

判定条件（`modifier_rules.py::_apply_chunk_boundary_downgrade`，作为第四个"修饰"叠加在规则G之后）：issue所在 `block_index` 是它所在chunk **正文**部分（不含重叠区）的最后一个block **且** 该chunk不是全文档最后一个chunk（`_compute_chunk_tail_blocks` 用 `ChunkedDocument.chunks[i].block_indices[-1]` 算出这个集合，全文档最后一个chunk天然排除——它后面确实没有更多内容了，不存在"看不到后续"的问题）**且** 该block原始文本本身不以句末标点（`。！？`）收尾（正常收尾的段落即使恰好卡在chunk边界也不该被怀疑）**且** 被flag的 `original_text` 原样是该block文本的结尾片段（`block.text.endswith(original_text)`，排除"block确实在chunk尾但issue其实出在block中间别处"的情况）。只在基础归层已判定为"确定性错误"时才降级（存疑/引文/风格本身已经比确定性错误宽松，无需再降）——和规则C/D的"降级不剔除"哲学一致，命中后强制改判"存疑待核实"+最低优先级，`suggestion` 前缀说明"疑似因文档分块处理...被分块边界截断"，`layer_notes` 记录判定依据，保留可审计性。

**为什么不在解析阶段（`core/parser/native_pdf.py`）直接合并这类被拆开的block**：诊断这次真实案例时发现，被拆开的两个block之间的行间距（约12pt）和同一悬挂缩进段落里"真正的新条目"之间的行间距几乎完全相同（同一份文档实测都在12.2~13.0pt），唯一能可靠区分"这是同一段的换行"还是"这是新条目"的信号是横坐标缩进（延续行统一缩进到x0=111，新条目统一从x0=90起，且新条目行首带项目符号字符）——这是这份文档排版软件生成PDF的具体几何特征，换一份用不同软件/不同悬挂缩进量生成的PDF未必成立，贸然把这套坐标启发式写进 `native_pdf.py` 会带来新的误合并风险（比如把两个本来独立的短条目错误粘成一段），且完全不覆盖OCR(B类)/Word(C类)两条通道（它们根本没有可比较的坐标信息）。相比之下，规则H命中的"block是chunk正文尾块"是从 `ChunkedDocument` 直接读出的确定性结构事实，不依赖任何长文档排版的几何假设，能覆盖"任意两个内容相关但被拆成不同block、又恰好卡在chunk边界"的更一般情况，不只是这一种悬挂缩进列表的样式。测试见 `tests/test_stage5.py` "规则H" 一节六条用例（真实场景复现降级、正常句末标点收尾不豁免、全文档最后一个chunk不豁免、block非chunk尾块不豁免、flag文本不在block结尾不豁免、不传chunked时规则不生效）；另有脚本对真实文档（`data/uploads/` 里的"后台管理员使用手册"样本，`record_id=8/9` 报告的原始issue）直接复现验证：`block_index=75` 命中规则H，`layer` 从"确定性错误"改判为"存疑待核实"，`priority` 从"中"改判为"低"。
