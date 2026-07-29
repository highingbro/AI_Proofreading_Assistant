# core/classifier/ 关键点

`classify_issues(result: ProofreadResult, parsed: ParsedDocument, chunked: ChunkedDocument | None = None, mode: str = config.PROOFREAD_MODE_DEEP) -> ClassifiedResult`是唯一对外入口：对每条 `RawIssue` 做系统层强制归层校验，不信任 LLM 自报的 `category`/`confidence`，产出 `ClassifiedIssue`（带 `layer`/`priority`/`layer_notes`，并保留 `llm_category`/`llm_confidence`/`original_suggestion` 供追问溯源）。纯规则逻辑，不调用LLM、不查库。`chunked` 是可选参数，不传时（如离线用 `--from-json` 数据反复调规则）规则H不生效，其余规则不受影响。

## 目录结构

- `_types.py`：`ClassifiedIssue`/`ClassifiedResult`/`_ClassificationState`。
- `heuristics.py`：引文/事实文本特征启发式（`_has_quotation_feature`/`_has_factual_feature`），基础规则A/B的漏报兜底能力落点。
- `base_rules.py`：基础归层规则A/B/E/F（`_rule_quotation`/`_rule_factual`/`_rule_style`/`_rule_default`，`_BASE_RULES` 元组，互斥、先命中先生效）。
- `modifier_rules.py`：修饰规则C/D/I/J/G/H+优先级覆盖，用 `_MODIFIER_RULES` 显式有序注册表声明执行顺序——想知道"规则G/H谁先跑"看这一个列表就够了，不用通读函数体；加新规则时往列表插一条，并写清楚为什么插在这个位置（是否依赖前面某条规则已改过layer/priority）。
- `postprocess.py`：跨块去重/排序/统计/视觉无实质改动的建议过滤/解析乱码控制字符过滤（`_dedup`/`_sort`/`_compute_stats`/`_filter_visually_no_op`，最后一个详见下方专门一节）。
- `__init__.py`：`classify_issue()`/`classify_issues()` 编排入口，对外只暴露 `ClassifiedIssue`/`ClassifiedResult`/`classify_issue`/`classify_issues`（`__all__`）。

## 归层结构：基础规则 + 修饰叠加两层，不是七条规则简单互斥

1. **基础归层**（互斥，先命中先生效）：规则A引文保护 → 规则B事实置信度 → **精简模式语法结构降级**（见下）→ **精简模式"汉字冒充标点"降级**（见下）→ 规则E风格 → 规则F默认兜底（必命中）。
2. **修饰叠加**（在基础归层结果之上，各自独立判断是否命中）：规则C——block的 `ocr_confidence` 低于 `config.OCR_CONF_DOWNGRADE_THRESHOLD`（默认0.90）且 `issue_type` 属于"错别字与拼写"/"标点符号问题"时，把已判定的"确定性错误"降级为"存疑待核实"（OCR可能认错字，不代表原文真错）；规则D——`located=False` 时同样把"确定性错误"封顶降级为"存疑待核实"；规则I——`original_text` 里的空格若被证实是PDF内部换行符被LLM转写产生的伪影（原文里并无此空格），同样封顶降级（详见下方"规则I"一节）。
3. 最后叠加优先级覆盖：`issue_type` 属于"政治敏感性表述"/"民族与地名规范"，无论前面落在哪一层，`priority` 强制改为'高'。

判定原因：提示词原文里规则C/D的处理措辞是"**原**layer若为确定性错误→降级"、"**最高只能到**存疑待核实"，这类表述预设"已经有一个layer存在"，是修饰而非独立分支——按字面顺序当六条互斥规则逐条测试会导致C/D的命中条件（OCR置信度、located字段）永远无法与A/B/E已经命中的情况共同生效。`tests/test_classifier.py::test_rule_c_stacks_on_top_of_base_rule_f` 专门验证这一叠加行为（F判定确定性错误后被C的OCR降级修饰，`layer_notes` 里两条依据都保留）。

其余启发式兜底规则（弥补LLM未自报 `category` 或 `issue_type` 判断不准的漏报场景，均在 `config.py` 里可调，宁可漏判不复杂化）：规则A额外识别书名号《》、≥10字的成对引号、文言虚词密度；规则B额外识别人名职务/机构名关键词+年份格式的"改写型"建议。**这两条兜底能力是设计铁律第2、3条"系统层兜底、不能只信LLM自报"的直接体现**——`tests/test_classifier.py::test_rule_a_book_title_heuristic_overrides_llm_miss` 就是验证"LLM没自报quotation，系统仍强制保护"的核心断言。

跨块去重（`postprocess.py::_dedup`）按 `(block_index, 归一化original_text)` 分组，保留更保守的一条（保守度：引文类>存疑待核实>风格可选>确定性错误）；`located=False` 的条目没有可靠 `block_index`，不参与去重、原样全部保留。排序直接按 `block_index` 升序（`block_index` 本身是解析阶段按阅读顺序分配的全局序号，天然满足"页码升序,同页按block_index"，未定位条目排最后）。`stats` 字段名（`total_issues`/`count_confirmed`/`count_doubtful`/`count_quotation`/`count_optional`/`high_priority_count`）与 `db/database.py` 里 `records` 表列名逐一对齐，供 `core/workflow/`/`core/exporter.py` 直接写库。

调试用 `tools/preview_classify.py <file> [--max-chunks N]`（全链路，耗额度）或 `tools/preview_classify.py <file> --from-json <path>`（复用 `tools/run_proofread.py --save-json` 存的 `RawIssue` 数据离线反复调规则，不耗额度，仍需原文件路径以便重新 `parse_document` 拿 `ParsedDocument` 供规则C查OCR置信度）。

## 视觉无实质改动的建议过滤 + 解析乱码控制字符过滤（不是归层规则，在 classify_issue 之前整条丢弃）

现象：真实使用中发现三类"假问题"——① `issue_type="错别字与拼写"`，建议是"应改为『X』"这类确定性改写格式，但『X』和原文**字面完全相同**（LLM把本该用"存疑"措辞的判断误报成了确定性建议，却又没能力给出真实改动）；② 原文里混入了PDF解析产生的**Unicode兼容变体字符**（如康熙部首"⽉"⽽非标准汉字"月"，`U+2F49` vs `U+6708`），肉眼完全看不出区别，但LLM把这个编码层面的差异当成"错别字"报了出来；③ 原文里混入了PDF字体解析产生的**控制字符乱码**（如私有区符号被误解析成SOH等控制码，`original_text="总第\x01\x01...期\x01"`），这类文本本身就是解析垃圾，没有可核实的价值。三类问题都不在 `prompt/proofread_rules.md` 定义的十类检查维度里（那里没有一条是关于Unicode编码/解析正确性的），是LLM自己加戏加出来的判断，且其自证逻辑站不住脚——真实追问过一次，AI一开始坚持"必须修正"，被追问到底后又承认"如果只用于印刷确实可以不改"，说明它自己也清楚这类判断的严重性被夸大了。

**②按"码位区间"判定，不维护"变体字→汉字"映射表**。Unicode的"Kangxi Radicals"区块（`U+2F00~2FDF`）大多有NFKC兼容分解，但"CJK Radicals Supplement"区块（`U+2E80~2EF3`）**整块都没有**，`unicodedata.normalize("NFKC", "⻔")` 原样返回"⻔"，NFKC对它完全无效。

早期做法是为后者维护一张人工核实过的映射表（13个字符）。**这条路被真实数据证伪：表永远追不上下一份文档**——同一份刊物换一次校对，LLM报到了表外的`⻣`(骨)、`⻰`(龙)，一次放出38条假错别字，一轮71条问题里50条源于此，且这类字符散布在整句里、LLM会引用整段来报，原文长度和噪声量成倍上升。

现在改为 `_diff_is_only_cjk_variants`：用 `difflib` 对齐"旧→新"，**只要求差异位置上旧侧字符的码位落在 `_CJK_VARIANT_RANGES` 内**，不关心它具体对应哪个汉字。不依赖表，所以任意新文档、任意没见过的变体字符都拦得住。三个配套约束缺一不可：

- **必须校验"变体字符的规范形式确实等于建议里的字"**（`_variant_char_matches`），不能只看它落在变体区就放行。真实反例：`数⼦化`→`数字化` 里 `⼦`(康熙部首"子")被误用成了`字`，这是**真错别字**，不校验就会被当字形变体丢掉。
- **CJK部首补充区无从校验，按"该区字符本就是某汉字的部首形式"放行**——这是不维护映射表付出的代价。
- **康熙部首区的NFKC分解结果一律是繁体字形**（`⼾`→`戶`，而简体正文里它是`户`），靠 `_KANGXI_TRADITIONAL_FOLDINGS` 折回简体。**这张表和"部首→汉字"映射表性质不同，边界是封闭的**：康熙部首区固定214个字符，其中简繁有别的就这么些，一次收全即可；且漏收只会少丢一条（保守方向），不会误删。

**只认等长替换**：出现增删说明改的是内容本身（`面对们`→`面对面` 是真错别字、`⸺`→`——` 是真的破折号字符不对），必须放行给人工判断。

**suggestion的措辞方式还会影响能不能抽出"新旧文本对"来比较**：常见的是"应改为『整段新文本』"（`_REPLACEMENT_SUGGESTION_RE`，和 `original_text` 整体比较），但真实数据里还出现了"『旧片段』应改为『新片段』"这种只描述 `original_text` 里某一个字/词该换的措辞（如 `original_text="归⺟扣⾮净利润"`，`suggestion='"⺟"应改为"母"。'`）——如果只有整段比较逻辑，会因为『⺟』（1字）和 `original_text`（7字）长度不一致误判成"不是零改动"而漏判。`postprocess.py::_extract_replacement_pair` 因此优先尝试 `_FRAGMENT_REPLACEMENT_RE`（片段式，抽出的『旧』还要求确实在 `original_text` 里出现过，避免措辞不规范时抽到不相关文本），抽不到才退回整段式，两种都抽不到才判定"这条issue无法判定新旧对比，不参与零改动判定"。`_is_visually_no_op_suggestion`/`modifier_rules.py` 里空格差异降级（见下方规则J）共用这同一个抽取函数。

判定（`postprocess.py::_is_visually_no_op_suggestion`，在 `classify_issues` 里于 `classify_issue` **之前**对 `RawIssue` 列表整体过滤，不是叠加在某条已归层结果上的修饰规则）：`_extract_replacement_pair` 抽出"旧→新"文本对后，三条判据取或——① 都过 `_normalize_lookalike`（纯NFKC）后相同；② `_diff_is_only_cjk_variants` 判定差异全在字形变体上；③ `_fragment_is_only_cjk_variants` 在原文里找到与建议等长、只差字形变体的窗口。`_has_stray_control_chars` 独立检查 `original_text` 是否含 `\t\n\r` 之外的Unicode `Cc`类控制字符，命中即无条件丢弃（不依赖suggestion内容——控制字符本身就证明这段"原文"是解析垃圾）。

**第③条针对的是这类假问题最常见的形态**：LLM引用一整行原文当 `original_text`，却只在建议里写要改的那个词（原文`能⼒，直接决定了企业的市场竞争⼒。尤其是从事⼤`、建议`应改为"能力"`）。这种措辞既不符合 `_FRAGMENT_REPLACEMENT_RE` 的"『旧』应改为『新』"格式，整段比对时长度又对不上，前两条都接不住。

**`_REPLACEMENT_TO_END_RE` 为什么要贪婪匹配到建议末尾**：LLM转述整段原文时会把原文自带的引号一起带上（`应改为"走进…深入"全员自主改善"的现场…"`），非贪婪规则在第一个内嵌引号处就截断，抽出残缺的新文本，长度对不上原文，零改动判定随即失效。贪婪版要求引号收在建议末尾（允许尾随句号），避免在"改为『X』，因为『Y』"这类后面还有引用的措辞里抽过头。**"应为"这个措辞只加进片段式正则、不加进整段式**：真实数据里"应为"后面常跟引号外的补充说明（`应为'进行智能制造整体规划以及推进落地'，'何'字多余`——真正的意图是删"何"字），整段式套用会把删字建议误判成零改动丢掉。

**处理方式是直接丢弃，不是降级**：与规则C/D/G/H/I/J"降级为存疑待核实、保留可审计性"的一般惯例不同，这是用户明确要求的特例——这类issue已经确认是零改动的假问题/解析垃圾，不存在"人工核实"的价值。`classify_issues` 返回的 `warnings` 里会分别记"丢弃N条视觉无实质改动的建议"/"丢弃N条解析产生乱码字符的问题"，与"跨块去重丢弃N条重复问题"是同一模式，供 `_render_stats` 的"提示信息"面板展示，不是悄无声息地消失。

测试见 `tests/test_classifier.py` "视觉无实质改动的建议过滤" 一节：字面完全相同丢弃、Kangxi Radicals区块Unicode兼容变体（真实用 `unicodedata.normalize` 验证过康熙部首"⽉"确实归一化等于"月"）丢弃、CJK Radicals Supplement区块无NFKC分解的部首替代字（"⻔"→"门"）丢弃、片段式措辞在更长original_text里的零改动丢弃、控制字符乱码丢弃、真实改写不误伤、"存疑"类引用原文不误伤七条用例。

**改动验证方式（真实数据回放）**：把 `data/app.db` 里同一份刊物三次校对记录（record 45/46/47）的全部 issue 重新过一遍过滤器——record 47（改动前漏出的那次）71条丢21条，而**record 45/46 丢弃数为0**，证明新判据只拦到了原先漏网的字形变体假错误，没有误伤历史上已经正常保留的结果。调规则时建议照此回放，比只看单元测试更能暴露误伤。

## 规则G：知识时效性误判豁免

现象：LLM可能仅因为文中某个年份超出了它的训练数据覆盖范围就产生怀疑（"这个日期看起来太新/我没见过"），但这种怀疑**只针对"这个时间点本身是否存在"，不针对"该时间点发生的事是否属实"**——是模型知识时效性局限造成的误判，不是真正的事实性错误，且这类问题在真实文档里出现频率不低，值得单独处理。

判定条件（`modifier_rules.py::_apply_recency_downgrade`，作为第三个"修饰"叠加在OCR降级、未定位封顶之后）：`issue_type`/`category` 落在事实类范围内 **且** `reason`/`suggestion` 命中 `config.RECENCY_DOUBT_KEYWORDS`（"训练数据""知识截止""较新"等怀疑"时间太新"的措辞，不是泛泛的事实怀疑）**且** 原文里最大的年份落在 `[config.LLM_KNOWLEDGE_CUTOFF_YEAR, +config.KNOWLEDGE_CUTOFF_GRACE_YEARS]` 区间内（当前配置即 2026~2028）——超过宽限期视为真的离谱，不豁免，保留原判定供人工核实。测试用例里的年份一律写成 `CUTOFF + GRACE_YEARS` 跟着配置走，不要写死年数：写死的话宽限期一调小，"窗口内"的用例会跑到窗口外，而"不该触发"的几条会变成"因为超窗口才没触发"，即使规则逻辑写错也照样通过。命中后**不是直接从结果里剔除**，而是强制降级为"存疑待核实"+最低优先级（`PRIORITY_LOW`），并在 `suggestion` 前缀说明"疑似因AI知识时效性产生的误判，建议直接忽略"、`layer_notes` 记录判定依据——选择降级而非剔除，是为了保留可审计性：用户在界面上一眼就能判断要不要忽略，即使关键词误伤了真正的事实性错误，信息也不会凭空消失。`config.LLM_KNOWLEDGE_CUTOFF_YEAR` 是按当前配置模型（`qwen3.6-plus`）拍脑袋定的初值，需要按真实知识截止时间校正；`RECENCY_DOUBT_KEYWORDS` 同样是初版关键词表，宁可漏判不复杂化，后续按真实LLM措辞调整。测试见 `tests/test_classifier.py` "规则G" 一节四条用例（容忍窗口内降级、超出宽限期不豁免、无"时效性"措辞不触发、非事实类issue_type不触发）。

## 规则H：分块(chunk)边界截断误判豁免

与规则G成因不同，不要混淆。现象与规则G表面相似（都是"看起来不完整/存疑"），但根因完全不同：真实文档里一段用悬挂缩进排版的列表条目（如"分类标签、\n浏览量、发布时间..."）被PyMuPDF在提取阶段拆成了两个独立 `ParsedBlock`（这本身是`core/parser/native_pdf.py`的一处已知不精确之处，几何上很难用一般规则可靠合并回一个block，见 [core/parser/CLAUDE.md](../parser/CLAUDE.md)），又恰好被 `core/chunker/` 的贪心装填算法切在两个不同的chunk里——前一个block变成chunk K正文的最后一块，后一个block变成chunk K+1正文的第一块。`config.OVERLAP_BLOCKS` 的"重叠区"设计只让chunk K+1向后携带chunk K的尾部内容供理解上文，从未设计"向前预览下一chunk"，所以LLM校对chunk K时，看到的正文原文就在"...分类标签、"处硬生生截断，将"分块导致看不到后续"误判成"内容不完整、标点缺失"——这是LLM基于它能看到的（不完整的）内容做出的合理判断，不是它凭空捏造错误，因此不能靠改prompt单方面指望LLM"猜到"自己看漏了内容，需要系统层能确定性地知道"这个block在这个位置就是被截断了"。

判定条件（`modifier_rules.py::_apply_chunk_boundary_downgrade`，作为第四个"修饰"叠加在规则G之后）：issue所在 `block_index` 是它所在chunk **正文**部分（不含重叠区）的最后一个block **且** 该chunk不是全文档最后一个chunk（`_compute_chunk_tail_blocks` 用 `ChunkedDocument.chunks[i].block_indices[-1]` 算出这个集合，全文档最后一个chunk天然排除——它后面确实没有更多内容了，不存在"看不到后续"的问题）**且** 该block原始文本本身不以句末标点（`。！？`）收尾（正常收尾的段落即使恰好卡在chunk边界也不该被怀疑）**且** 被flag的 `original_text` 原样是该block文本的结尾片段（`block.text.endswith(original_text)`，排除"block确实在chunk尾但issue其实出在block中间别处"的情况）。只在基础归层已判定为"确定性错误"时才降级（存疑/引文/风格本身已经比确定性错误宽松，无需再降）——和规则C/D的"降级不剔除"哲学一致，命中后强制改判"存疑待核实"+最低优先级，`suggestion` 前缀说明"疑似因文档分块处理...被分块边界截断"，`layer_notes` 记录判定依据，保留可审计性。

**为什么不在解析阶段（`core/parser/native_pdf.py`）直接合并这类被拆开的block**：诊断这次真实案例时发现，被拆开的两个block之间的行间距（约12pt）和同一悬挂缩进段落里"真正的新条目"之间的行间距几乎完全相同（同一份文档实测都在12.2~13.0pt），唯一能可靠区分"这是同一段的换行"还是"这是新条目"的信号是横坐标缩进（延续行统一缩进到x0=111，新条目统一从x0=90起，且新条目行首带项目符号字符）——这是这份文档排版软件生成PDF的具体几何特征，换一份用不同软件/不同悬挂缩进量生成的PDF未必成立，贸然把这套坐标启发式写进 `native_pdf.py` 会带来新的误合并风险（比如把两个本来独立的短条目错误粘成一段），且完全不覆盖OCR(B类)/Word(C类)两条通道（它们根本没有可比较的坐标信息）。相比之下，规则H命中的"block是chunk正文尾块"是从 `ChunkedDocument` 直接读出的确定性结构事实，不依赖任何长文档排版的几何假设，能覆盖"任意两个内容相关但被拆成不同block、又恰好卡在chunk边界"的更一般情况，不只是这一种悬挂缩进列表的样式。测试见 `tests/test_classifier.py` "规则H" 一节六条用例（真实场景复现降级、正常句末标点收尾不豁免、全文档最后一个chunk不豁免、block非chunk尾块不豁免、flag文本不在block结尾不豁免、不传chunked时规则不生效）；另有脚本对真实文档（`data/uploads/` 里的"后台管理员使用手册"样本，`record_id=8/9` 报告的原始issue）直接复现验证：`block_index=75` 命中规则H，`layer` 从"确定性错误"改判为"存疑待核实"，`priority` 从"中"改判为"低"。

## 规则I：PDF换行符被LLM转写成空格的解析伪影豁免

和规则H成因不同（都涉及"PDF换行/分块导致的解析伪影"这个大主题，但触发机制完全不是一回事，不要混淆）：规则H是**分块(chunk)边界**截断导致LLM看不到后续内容；规则I是**block内部**——`core/parser/native_pdf.py` 把同一个PyMuPDF block内的多个PDF行用换行符(`\n`)拼接（详见 [core/parser/CLAUDE.md](../parser/CLAUDE.md)"block内多行拼接必须用换行符"一节），但PDF按页宽自动换行完全不认汉字词语边界，经常把一个双字词从中间断开（如"教师"被拆成上一行末尾的"教"、下一行开头的"师"），`block.text` 里就会出现"...教\n师..."这种词语中间夹着换行符的情况。系统提示词要求 `original_text` "必须逐字取自正文"，但真实案例显示LLM读到这类跨行词语时会把 `\n` 转写/归一化成一个空格再引用（而不是照原样保留 `\n`，或者干脆去掉 `\n` 直接拼接），导致 `original_text` 里出现一个源文档里其实并不存在的空格，进而被当成"多余空格/错别字"上报（真实案例：`original_text="教务管理教 师"`，`suggestion="应删除空格"`，但原文里这个位置根本没有空格，只有一个被拼接掉的换行符）。

判定条件（`modifier_rules.py::_apply_linewrap_space_artifact_downgrade`，插在未定位封顶(规则D)之后、知识时效性豁免(规则G)之前）：`state.layer` 是"确定性错误"（更宽松的层级不需要再降）**且** `original_text` 含空格 **且** `original_text`（带空格原样）不是 `block.text` 的子串（证明这个空格不是原文/解析结果真实携带的）**且** `original_text` 去掉所有空格后是 `block.text` 去掉所有换行符后的子串（证明"去掉这些空格/换行符差异，内容确实原样来自这个block"）。命中后强制改判"存疑待核实"+最低优先级，`suggestion` 前缀说明"疑似因PDF内部排版换行被误转写产生，原文中并无此空格"，`layer_notes` 记录判定依据，与规则C/D/H"降级不剔除、保留可审计性"的哲学一致。

**为什么整体去除后子串匹配，而不是精确定位单个空格对应的 `\n` 位置再逐一替换验证**：真实案例验证过，LLM转写时插入空格的具体字符位置和 `block.text` 里 `\n` 的真实位置可能相差一个字符——例如上面"教务管理教 师"这个真实例子，`block.text` 里 `\n` 实际在"教务管理"和"教师无组织权限"之间（即"理"和第二个"教"之间），但LLM转写出的空格却落在"教"和"师"之间（晚了一个字符）；这说明LLM不是逐字符做"\n→空格"的精确单点替换，更像是识别出这是同一个词语被换行拆开后，按自己对这个词语的理解重新组织了一遍再输出——如果按"逐个空格找对应\n位置做精确替换验证"的思路实现，这个真实案例会因为位置差一位而被判定为"不匹配"，从而漏判。改成"整体去除空格/去除换行符后做子串匹配"，就不依赖任何精确位置对应关系，只要求"内容确实来自原文、多出来的字符只是空格"这个更宽松也更符合真实LLM行为的条件。

**为什么不要求 `issue_type` 必须是"错别字与拼写"**：检测条件本身（整体子串匹配）已经足够精确、误伤风险很低，额外用 `issue_type` 收窄反而可能漏掉LLM把同一现象误归类到"标点符号问题"等其他类型的场景，这条规则不区分 `mode`（深度/精简都生效）——它修的是"LLM转写出了原文里根本不存在的内容"这个正确性问题，不是"该按多严格的标准校对"这类容忍度问题，跟精简/深度模式的定位是两回事。测试见 `tests/test_classifier.py` "规则I" 一节。

## 规则J：版式错乱措辞 / 空格差异兜底降级

现象：即使前面的丢弃过滤器（视觉无实质改动/控制字符乱码）已经拦掉了一部分假问题，真实数据（`data/app.db` 35号记录，99条被判"确定性错误"的issue，抽样核实后逐一分类统计）显示仍有残留，且能归成两类稳定的模式：① LLM自己在 `reason`/`suggestion` 里承认"此处排版错乱""跨行错位""乱码"（PDF多栏排版/表格/图注被解析打乱语序、内容拼接错行）——LLM是老实交代了"我也没看懂这段被打乱的版面"，这不是语言本身的确定性错误；② `suggestion` 建议的替换内容和 `original_text` 的唯一差异是空格数量（如 `"2 0 2 5年9⽉1 1⽇"` → `"2025年9月11日"`，数字/日期被拆开插入了空格），空格大概率是PDF跨行拼接、装饰性字间距等排版原因产生的解析伪影，原文本身是否真的多/少这个空格，无法仅凭文本内容判断，达不到"确定性错误"的把握。两种情况共同点：都不是"一眼就能看出"的确定性错误，必须至少降级为存疑，不能留在错误类——但也不能像视觉无实质改动那样直接丢弃，因为不能100%确定原文真的没有这处问题（保留可审计性，让用户自己核实）。

判定条件（`modifier_rules.py::_apply_layout_and_space_artifact_downgrade`，插在规则I之后、规则G之前）：`state.layer == config.LAYER_CONFIRMED`（更宽松的层级不需要再降）**且**（`reason`/`suggestion` 命中 `config.LAYOUT_ARTIFACT_KEYWORDS`（"排版错乱""跨行""错行""错位""乱码""段落顺序""图片位置"，真实数据里LLM原话摘出）**或者** `_extract_replacement_pair`（与视觉无实质改动过滤共用的抽取函数）抽出的"旧→新"文本对里，至少一侧确实含空格，且两者都过 `_strip_whitespace(_normalize_lookalike(...))` 后完全相同）。命中后强制改判"存疑待核实"+最低优先级，`suggestion` 前缀说明原因，`layer_notes` 记录判定依据，与规则C/D/G/H"降级不剔除、保留可审计性"的哲学一致。

**空格差异比较为什么要先套 `_normalize_lookalike` 再去空格，而不是直接比较原始文本去空格**：真实案例里空格差异经常和部首替代字**同时**出现在同一条issue里（`"2 0 2 5年9⽉1 1⽇"` 里"⽉"既是部首替代字又混着空格差异）——如果只对原始文本去空格再比较，会因为"⽉"≠"月"（字符本身不相等）判定"不匹配"而漏判这条本该降级的issue。两次归一化叠加使用（先部首替代修正，再去空格），才能同时覆盖"纯空格差异"和"空格+部首替代字混合"两种真实出现过的场景。

测试见 `tests/test_classifier.py` "规则J" 一节：版式错乱措辞降级、空格差异+部首替代字混合降级、真实拼写错误（"Linxu"→"Linux"）不误伤三条用例。

**已知的残留场景（未覆盖，接受漏判）**：同一份真实文档里还观察到两类更难通用识别的假问题，本次没有针对性处理——① 极少数原文段落被拆分/重排后不只是空格差异，而是**字符顺序整体打乱**（如封面标题"岁末盘点"被解析成"岁 末点 盘"，不是简单加/减空格，去空格比较后内容也对不上）；② 某个特定字体把标点符号（句号/逗号）错误映射成了一个生僻汉字"盓"，在这份文档里反复出现（5处），但这是这份文档专属的字体子集化bug特征，不是可以泛化到其他文档的通用模式，没有像部首替代表那样单独处理。这两类目前仍会落在"确定性错误"层，人工核实时需要留意。另外调试时还发现一个和本次改动无关但同批数据暴露出的现有问题：`base_rules.py::_rule_default` 对 `confidence=="high"` 的issue无条件判"确定性错误"，不检查 `suggestion` 是否已经以"存疑"开头（`_rule_factual` 的medium/low分支有这个检查，`_rule_default` 没有）——真实数据里出现过LLM自报高置信度、但suggestion原文写着"存疑，建议人工核实：..."的矛盾案例，这条issue因此被错误分到了错误类；这是 `_rule_default` 自身的既有逻辑缺口，不属于本节任何一条新规则的范围，未在本次改动中处理。

## 精简模式下语法结构问题(规则2)统一按风格可选处理

精简模式服务的是"不需要出版级严格校对"的普通/技术文档场景，容忍度本来就比深度模式高，但"语法结构问题"（对应 `proofread_rules.md` 规则2）在这类文档里命中率不低，且很多命中其实是"怎么写都通"的润色，不是真正影响理解的病句——典型信号是LLM给出的建议里出现"改为A**或**B"这种平级备选写法（如"开始结束时间"→"改为'开始、结束时间'或'开始和结束时间'"），这说明没有唯一正确写法，纯粹是文字风格选择，继续走 `_rule_default` 兜底判成"确定性错误/存疑待核实"（`base_rules.py::_rule_default`）过于严格。

判定条件（`base_rules.py::_rule_simplified_grammar_as_style`，插在基础规则 A/B 之后、E 之前）：`mode == config.PROOFREAD_MODE_SIMPLIFIED` **且** `raw.issue_type == "语法结构问题"`，命中即直接归为"风格可选"+最低优先级，不再看 `confidence`。放在 A/B 之后是为了保证**引文保护、事实置信度降级这两条设计铁律不因精简模式被弱化**——即使在精简模式下，同一条issue如果先命中了quotation/factual的文本特征，仍会被规则A/B拦下，不会流到这条规则。深度模式（`mode` 不传或为 `PROOFREAD_MODE_DEEP`）完全不受影响，规则2依旧走原有的confidence判定路径。

`mode` 参数是这条规则新增才从 `classify_issues()`/`classify_issue()` 打通到 `_BASE_RULES` 里的（其余四条基础规则原先只依赖 `raw` 一个参数，签名统一加宽为 `(raw, mode)` 后三条不使用该参数，纯粹是为了让 `_BASE_RULES` 元组里所有规则保持统一签名，方便 for 循环调用，和 `modifier_rules.py` 的既有约定一致）。测试见 `tests/test_classifier.py` "精简模式语法结构降级" 一节（深度模式不受影响的回归用例、精简模式下不分confidence都归风格可选、精简模式下quotation/factual仍优先于此规则生效）。

## 精简模式下规则1里"汉字冒充标点符号"这类零歧义问题按风格可选处理

和上面"精简模式语法结构降级"成因不同，不要混为一谈：一条真实issue——原文"管理控台一角色权限"，`issue_type` 是"错别字与拼写"，建议"将汉字'一'改为破折号'——'或短横线'-'"——质疑这类问题为什么要判成错别字。分析后发现：这类问题的本质是"用错了字形近似的**汉字**去代替本该用的**标点符号**"（数词"一"和破折号"—"/短横线"-"笔画/字形接近，容易在录入时误用），和"的/地/得"这类**可能真正改变语义或引起误解**的错别字不是一回事——"一"冒充破折号纯粹是排版惯例问题，读者对内容的理解**完全不受影响**，不管用哪个字符/符号，"管理控制台—角色权限"这句话的意思都是一样的。而"的/地/得"用错有时候真的会改变句子的语法角色或造成歧义。二者都被LLM归进了"错别字与拼写"这同一个 `issue_type`，但严格程度不该一刀切。

判定条件（`base_rules.py::_rule_simplified_typo_as_punctuation`，插在"精简模式语法结构降级"之后、规则E风格之前，和它是否谁先跑互不影响——两条规则的 `issue_type` 判断条件互斥）：`mode == config.PROOFREAD_MODE_SIMPLIFIED` **且** `raw.issue_type == "错别字与拼写"` **且** 建议/理由文字里命中 `config.SIMPLIFIED_TYPO_PUNCTUATION_KEYWORDS`（"破折号""连接号""短横线""分隔号""间隔号"，即建议是"把某个汉字改成某种连接类标点"这个模式）。命中即直接归为"风格可选"+最低优先级，不再看confidence。**没有走通用的文本特征启发式（比如识别"一"这个字本身），而是用建议措辞反推**——因为直接识别"原文里出现了'一'"太宽泛、极易误伤（"一"是最常见的汉字之一，绝大多数出现场景和标点毫无关系），而"LLM建议把某个字改成破折号/连接号"这个措辞模式本身就已经是"这是一次汉字冒充标点"的强信号，不需要再对原文本身做启发式判断。宁可漏判（真的有这类问题但LLM没用这几个关键词描述）也不误伤真正的错别字。

深度模式不受影响；引文保护/事实置信度降级（规则A/B）仍排在此规则之前，命中时优先生效，不会被这条规则弱化——和"精简模式语法结构降级"遵循同一条设计原则。测试见 `tests/test_classifier.py` "精简模式'汉字冒充标点'降级" 一节。

人工反馈的规避不在本模块——历史拒绝记录交给LLM做语义总结、注入校对提示词的"补充规则四：历史反馈规避"（见 `prompt/proofread_system.md`），让LLM在生成建议这一步就主动规避曾被拒绝的问题模式，不由分类器事后改判，详见 `core/feedback_rules.py` 模块docstring。`core/classifier` 因此不接受任何反馈相关参数，`_MODIFIER_RULES` 里所有规则统一签名 `(raw, block, tail_blocks, state)`，保持"纯规则逻辑，不调用LLM、不查库"。
