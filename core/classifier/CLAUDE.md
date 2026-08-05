# core/classifier/ 关键点

`classify_issues(result: ProofreadResult, parsed: ParsedDocument, chunked: ChunkedDocument | None = None, mode: str = config.PROOFREAD_MODE_DEEP) -> ClassifiedResult`是唯一对外入口：对每条 `RawIssue` 做系统层强制归层校验，不信任 LLM 自报的 `category`/`confidence`，产出 `ClassifiedIssue`（带 `layer`/`priority`/`layer_notes`，并保留 `llm_category`/`llm_confidence`/`original_suggestion` 供追问溯源）。纯规则逻辑，不调用LLM、不查库。`chunked` 是可选参数，不传时（如离线用 `--from-json` 数据反复调规则）规则H不生效，其余规则不受影响。

## 目录结构

- `_types.py`：`ClassifiedIssue`/`ClassifiedResult`/`_ClassificationState`。
- `heuristics.py`：引文/事实文本特征启发式（`_has_quotation_feature`/`_has_factual_feature`），基础规则A/B的漏报兜底能力落点。
- `base_rules.py`：基础归层规则A/B/E/F（`_rule_quotation`/`_rule_factual`/`_rule_style`/`_rule_default`，`_BASE_RULES` 元组，互斥、先命中先生效）。
- `modifier_rules.py`：修饰规则C/D+优先级覆盖（用 `_MODIFIER_RULES` 显式有序注册表声明执行顺序），以及 `is_artifact_misjudgment` 及其三条判定谓词（知识边界/换行符转写/分块边界，判定为"我们自己造成的误判"后整条丢弃，不参与归层）——想知道"规则G/H谁先跑"看这一个列表就够了，不用通读函数体；加新规则时往列表插一条，并写清楚为什么插在这个位置（是否依赖前面某条规则已改过layer/priority）。
- `postprocess.py`：跨块去重/排序/统计/归层前的四类整条丢弃过滤——视觉无实质改动、解析乱码控制字符、版式错乱或纯空格差异、LLM自陈不该报告（`_dedup`/`_sort`/`_compute_stats`/`_filter_visually_no_op`，最后一个详见下方专门几节）。
- `__init__.py`：`classify_issue()`/`classify_issues()` 编排入口，对外只暴露 `ClassifiedIssue`/`ClassifiedResult`/`classify_issue`/`classify_issues`（`__all__`）。

## 归层结构：基础规则 + 修饰叠加两层，不是七条规则简单互斥

1. **基础归层**（互斥，先命中先生效）：规则A引文保护 → 规则B事实置信度 → **精简模式语法结构降级**（见下）→ **精简模式"汉字冒充标点"降级**（见下）→ 规则E风格 → 规则F默认兜底（必命中）。
2. **修饰叠加**（在基础归层结果之上，各自独立判断是否命中）：规则C——block的 `ocr_confidence` 低于 `config.OCR_CONF_DOWNGRADE_THRESHOLD`（默认0.90）且 `issue_type` 属于"错别字与拼写"/"标点符号问题"时，把已判定的"确定性错误"降级为"存疑待核实"（OCR可能认错字，不代表原文真错）；规则D——`located=False` 时同样把"确定性错误"封顶降级为"存疑待核实"。**规则G/H/I 不在这一层**——它们判定的是"我们自己造成的误判"，命中即整条丢弃，见下方专门一节。
3. 最后叠加优先级覆盖：`issue_type` 属于"政治敏感性表述"/"民族与地名规范"，无论前面落在哪一层，`priority` 强制改为'高'。

判定原因：提示词原文里规则C/D的处理措辞是"**原**layer若为确定性错误→降级"、"**最高只能到**存疑待核实"，这类表述预设"已经有一个layer存在"，是修饰而非独立分支——按字面顺序当六条互斥规则逐条测试会导致C/D的命中条件（OCR置信度、located字段）永远无法与A/B/E已经命中的情况共同生效。`tests/test_classifier.py::test_rule_c_stacks_on_top_of_base_rule_f` 专门验证这一叠加行为（F判定确定性错误后被C的OCR降级修饰，`layer_notes` 里两条依据都保留）。

其余启发式兜底规则（弥补LLM未自报 `category` 或 `issue_type` 判断不准的漏报场景，均在 `config.py` 里可调，宁可漏判不复杂化）：规则A额外识别≥10字的成对引号、文言虚词密度；规则B额外识别人名职务/机构名关键词+年份格式的"改写型"建议。**这两条兜底能力是设计铁律第2、3条"系统层兜底、不能只信LLM自报"的直接体现**——`tests/test_classifier.py::test_rule_a_long_quote_heuristic_overrides_llm_miss` 就是验证"LLM没自报quotation，系统仍强制保护"的核心断言。

**规则A不认书名号《》，这是一条刻意的缺席**：书名号标的是"作品名"而非"引文"，而中文正文里最高频的书名号用法是列举自家课程/文件标题——那是本方的原创内容，里面的错就是真错。把 `data/app.db` 里全部引文类issue离线回放过一遍：305条中255条**只**靠书名号命中，其中239条LLM想改的位置压根不在《》里面（序号与书名号之间的多余点、零宽字符、公示文件年份写错），剩下16条落在《》内部的又全是部首编码伪影（`⾯向`→`面向`）——真引文一条没保住，却把239条真问题压成了"原文照录，不建议改动"整层吞掉。想按"改动位置是否落在《》内部"收窄也不成立：那16条证明《》内部命中的同样是伪影。书名被LLM擅改的场景改由 `category=quotation` 与 `issue_type=引用与成语准确性` 两条信号兜底，接受漏判。防线测试 `test_rule_a_book_title_alone_is_not_quotation`。

跨块去重（`postprocess.py::_dedup`）按 `(block_index, 归一化original_text)` 分组，保留更保守的一条（保守度：引文类>存疑待核实>风格可选>确定性错误）；`located=False` 的条目没有可靠 `block_index`，不参与去重、原样全部保留。排序直接按 `block_index` 升序（`block_index` 本身是解析阶段按阅读顺序分配的全局序号，天然满足"页码升序,同页按block_index"，未定位条目排最后）。`stats` 字段名（`total_issues`/`count_confirmed`/`count_doubtful`/`count_quotation`/`count_optional`/`high_priority_count`）与 `db/database.py` 里 `records` 表列名逐一对齐，供 `core/workflow/`/`core/exporter.py` 直接写库。

调试用 `tools/preview_classify.py <file> [--max-chunks N]`（全链路，耗额度）或 `tools/preview_classify.py <file> --from-json <path>`（复用 `tools/run_proofread.py --save-json` 存的 `RawIssue` 数据离线反复调规则，不耗额度，仍需原文件路径以便重新 `parse_document` 拿 `ParsedDocument` 供规则C查OCR置信度）。

## 视觉无实质改动的建议过滤 + 解析乱码控制字符过滤（不是归层规则，在 classify_issue 之前整条丢弃）

现象：真实使用中发现三类"假问题"——① `issue_type="错别字与拼写"`，建议是"应改为『X』"这类确定性改写格式，但『X』和原文**字面完全相同**（LLM把本该用"存疑"措辞的判断误报成了确定性建议，却又没能力给出真实改动）；② 原文里混入了PDF解析产生的**Unicode兼容变体字符**（如康熙部首"⽉"⽽非标准汉字"月"，`U+2F49` vs `U+6708`），肉眼完全看不出区别，但LLM把这个编码层面的差异当成"错别字"报了出来；③ 原文里混入了PDF字体解析产生的**控制字符乱码**（如私有区符号被误解析成SOH等控制码，`original_text="总第\x01\x01...期\x01"`），这类文本本身就是解析垃圾，没有可核实的价值。三类问题都不在 `prompt/proofread_rules.md` 定义的十类检查维度里（那里没有一条是关于Unicode编码/解析正确性的），是LLM自己加戏加出来的判断，且其自证逻辑站不住脚——真实追问过一次，AI一开始坚持"必须修正"，被追问到底后又承认"如果只用于印刷确实可以不改"，说明它自己也清楚这类判断的严重性被夸大了。

**②按"码位区间"判定，不维护"变体字→汉字"映射表**。Unicode的"Kangxi Radicals"区块（`U+2F00~2FDF`）大多有NFKC兼容分解，但"CJK Radicals Supplement"区块（`U+2E80~2EF3`）**整块都没有**，`unicodedata.normalize("NFKC", "⻔")` 原样返回"⻔"，NFKC对它完全无效。

**人工核实过的映射表这条路被真实数据证伪：表永远追不上下一份文档**——一张13个字符的表，同一份刊物换一次校对，LLM就报到了表外的`⻣`(骨)、`⻰`(龙)，一次放出38条假错别字，一轮71条问题里50条源于此，且这类字符散布在整句里、LLM会引用整段来报，原文长度和噪声量成倍上升。

判据因此是 `_diff_is_only_cjk_variants`：用 `difflib` 对齐"旧→新"，**只要求差异位置上旧侧字符的码位落在 `_CJK_VARIANT_RANGES` 内**，不关心它具体对应哪个汉字。不依赖表，所以任意新文档、任意没见过的变体字符都拦得住。三个配套约束缺一不可：

- **必须校验"变体字符的规范形式确实等于建议里的字"**（`_variant_char_matches`），不能只看它落在变体区就放行。真实反例：`数⼦化`→`数字化` 里 `⼦`(康熙部首"子")被误用成了`字`，这是**真错别字**，不校验就会被当字形变体丢掉。
- **CJK部首补充区无从校验，按"该区字符本就是某汉字的部首形式"放行**——这是不维护映射表付出的代价。
- **康熙部首区的NFKC分解结果一律是繁体字形**（`⼾`→`戶`，而简体正文里它是`户`），靠 `_KANGXI_TRADITIONAL_FOLDINGS` 折回简体。**这张表和"部首→汉字"映射表性质不同，边界是封闭的**：康熙部首区固定214个字符，其中简繁有别的就这么些，一次收全即可；且漏收只会少丢一条（保守方向），不会误删。

**只认等长替换**：出现增删说明改的是内容本身（`面对们`→`面对面` 是真错别字、`⸺`→`——` 是真的破折号字符不对），必须放行给人工判断。

**suggestion的措辞方式还会影响能不能抽出"新旧文本对"来比较**：常见的是"应改为『整段新文本』"（`_REPLACEMENT_SUGGESTION_RE`，和 `original_text` 整体比较），但真实数据里还出现了"『旧片段』应改为『新片段』"这种只描述 `original_text` 里某一个字/词该换的措辞（如 `original_text="归⺟扣⾮净利润"`，`suggestion='"⺟"应改为"母"。'`）——如果只有整段比较逻辑，会因为『⺟』（1字）和 `original_text`（7字）长度不一致误判成"不是零改动"而漏判。`postprocess.py::_extract_replacement_pair` 因此优先尝试 `_FRAGMENT_REPLACEMENT_RE`（片段式，抽出的『旧』还要求确实在 `original_text` 里出现过，避免措辞不规范时抽到不相关文本），抽不到才退回整段式，两种都抽不到才判定"这条issue无法判定新旧对比，不参与零改动判定"。`_is_visually_no_op_suggestion` 与 `_is_layout_or_space_artifact`（见下方"版式错乱措辞 / 建议与原文只差空格"一节）共用这同一个抽取函数。

**三种措辞模式都抽不到时，兜底把 `suggestion.strip()` 整体当候选新文本，不返回None**：真实案例——`suggestion` 有时不套"应改为『X』"这层话术，字段内容就是修正后的文本本身（`original_text="AI 助力PMC实战进阶"`、`suggestion="AI助力PMC实战进阶"`，没有任何引号/动词包裹）。这种"裸"措辞是LLM输出格式的又一种变体，每冒出一种新变体就为它加一条正则是在追着LLM的措辞打地鼠，所以"这算不算零改动/纯空格差异"完全交给后续判据（NFKC归一化相同、CJK变体diff、去空格比较）决定，`_extract_replacement_pair` 不去识别"这段文本是不是裸替换文本"。兜底之所以安全，是因为真正的描述性建议（如"建议删除多余的'的'字"）内容和 `original_text` 本来就相差悬殊，天然通不过这几条判据。

判定（`postprocess.py::_is_visually_no_op_suggestion`，在 `classify_issues` 里于 `classify_issue` **之前**对 `RawIssue` 列表整体过滤，不是叠加在某条已归层结果上的修饰规则）：`_extract_replacement_pair` 抽出"旧→新"文本对后，三条判据取或——① 都过 `_normalize_lookalike`（纯NFKC）后相同；② `_diff_is_only_cjk_variants` 判定差异全在字形变体上；③ `_fragment_is_only_cjk_variants` 在原文里找到与建议等长、只差字形变体的窗口。`_has_stray_control_chars` 独立检查 `original_text` 是否含 `\t\n\r` 之外的Unicode `Cc`类控制字符，命中即无条件丢弃（不依赖suggestion内容——控制字符本身就证明这段"原文"是解析垃圾）。

**第③条针对的是这类假问题最常见的形态**：LLM引用一整行原文当 `original_text`，却只在建议里写要改的那个词（原文`能⼒，直接决定了企业的市场竞争⼒。尤其是从事⼤`、建议`应改为"能力"`）。这种措辞既不符合 `_FRAGMENT_REPLACEMENT_RE` 的"『旧』应改为『新』"格式，整段比对时长度又对不上，前两条都接不住。

**`_REPLACEMENT_TO_END_RE` 为什么要贪婪匹配到建议末尾**：LLM转述整段原文时会把原文自带的引号一起带上（`应改为"走进…深入"全员自主改善"的现场…"`），非贪婪规则在第一个内嵌引号处就截断，抽出残缺的新文本，长度对不上原文，零改动判定随即失效。贪婪版要求引号收在建议末尾（允许尾随句号），避免在"改为『X』，因为『Y』"这类后面还有引用的措辞里抽过头。**"应为"这个措辞只加进片段式正则、不加进整段式**：真实数据里"应为"后面常跟引号外的补充说明（`应为'进行智能制造整体规划以及推进落地'，'何'字多余`——真正的意图是删"何"字），整段式套用会把删字建议误判成零改动丢掉。

**处理方式是直接丢弃，不是降级**：与规则C/D"降级为存疑待核实、保留可审计性"的一般惯例不同，这是用户明确要求的特例——这类issue已经确认是零改动的假问题/解析垃圾，不存在"人工核实"的价值。`classify_issues` 返回的 `warnings` 里会分别记"丢弃N条视觉无实质改动的建议"/"丢弃N条解析产生乱码字符的问题"，与"跨块去重丢弃N条重复问题"是同一模式，供 `ui/cards.py::render_stats` 的"提示信息"面板展示，不是悄无声息地消失。

测试见 `tests/test_classifier.py` "视觉无实质改动的建议过滤" 一节：字面完全相同丢弃、Kangxi Radicals区块Unicode兼容变体（真实用 `unicodedata.normalize` 验证过康熙部首"⽉"确实归一化等于"月"）丢弃、CJK Radicals Supplement区块无NFKC分解的部首替代字（"⻔"→"门"）丢弃、片段式措辞在更长original_text里的零改动丢弃、控制字符乱码丢弃、真实改写不误伤、"存疑"类引用原文不误伤、裸措辞（无"应改为"包裹）零改动丢弃、裸措辞不误伤描述性建议九条用例。

**改规则时用真实数据回放验证，比只看单元测试更能暴露误伤**：把 `data/app.db` 里同一份刊物三次校对记录（record 45/46/47）的全部 issue 重新过一遍过滤器。当前判据下 record 47（字形变体假错误集中的那次）71条丢21条，record 45/46 丢弃数为0——后者是误伤的标尺，它一旦不为0就说明判据放宽过头了。

**这套过滤只检查 `raw.suggestion`，覆盖不到规则A(`_rule_quotation`)展示给用户的 `raw.reason`**——真实案例：命中引文保护、"原文照录不建议改动"，但LLM把两个疑点写进同一句reason里，"编号与标题之间的标点格式不统一，应为'4.'"是真疑点，"'⼊表'应为'入表'"纯粹是部首编码伪影（⼊是"入"的康熙部首变体），`_filter_visually_no_op` 只看 `raw.suggestion` 整条是否零改动，从未检查过 `raw.reason`，这类噪声就原样展示成"疑点供参考"，误导核实方向。`_rule_quotation` 因此单独调用 `_strip_visually_no_op_fragments`（`postprocess.py`）对 `raw.reason` 做片段级剔除，不是整条丢弃——两个疑点混在同一句话里，整条丢会连真疑点一起丢，只能挑出零改动的那一段删掉（连同前面的"，且"/"、且"连接词一起删，避免留下悬空残句）；reason整句都是零改动时会退化成空字符串，此时 fallback 成不带冒号的"原文照录，不建议改动。"，不留"疑点供参考："空尾巴。判据复用 `_normalize_lookalike`/`_diff_is_only_cjk_variants`，与上面的整条丢弃逻辑标准一致。测试见 `tests/test_classifier.py::test_rule_a_strips_cjk_variant_fragment_from_reason_but_keeps_real_doubt`/`test_rule_a_reason_entirely_cjk_variant_falls_back_to_bare_suggestion`。

## "我们自己造成的误判"一律丢弃：知识边界 / 换行符转写 / 分块边界

三类的共同点：**问题不在文档里，在我们这条流水线上**。判定谓词在 `modifier_rules.py`，由
`__init__.py::classify_issues` 通过 `is_artifact_misjudgment(raw, block, tail_blocks)` 在归层
**之前**整条丢弃，丢弃数进 `warnings`（"丢弃N条解析/分块/知识边界造成的误判"）。

**是丢弃、不是降级，这是用户的明确要求**：降级会在建议前贴一句"疑似……建议核实后再处理"，那句
话本身就是噪声——用户看到的仍是一条要处理的问题，而它百分之百不是原文的错。判定挪到归层之前，
所以不再看 layer，引文类/风格类也一并丢；不违反引文保护铁律——那条要的是"不对引文提改动建议"，
这里是把一条压根不存在的问题整个删掉，比"原文照录"更保守。

**代价要知道**：不再有"降级保留可审计性、让用户自己判断"这条退路，三条判据任何一条误伤，信息就
真的没了。所以每条判据都刻意收得很窄（见下），宁可漏判。

### `_is_recency_misjudgment`：LLM 只是因为年份超出训练数据而怀疑

这种怀疑**只针对"这个时间点本身存不存在"，不针对"该时间点发生的事是否属实"**。三个条件同时成立
才算：`issue_type`/`category` 落在事实类范围内 **且** `reason`/`suggestion` 命中
`config.RECENCY_DOUBT_KEYWORDS`（"训练数据""知识截止""较新"这类怀疑"时间太新"的措辞，不是泛泛的
事实怀疑）**且** 原文里最大的年份落在 `[LLM_KNOWLEDGE_CUTOFF_YEAR, +KNOWLEDGE_CUTOFF_GRACE_YEARS]`
区间内——超出宽限期视为真的离谱（相差几十年），不算这一类。

测试里的年份一律写成 `CUTOFF + GRACE_YEARS` 跟着配置走，**不要写死年数**：写死的话宽限期一调小，
"窗口内"的用例会跑到窗口外，而"不该触发"的几条会变成"因为超窗口才没触发"，规则逻辑写错也照样通过。
`config.LLM_KNOWLEDGE_CUTOFF_YEAR` 是按当前配置模型拍脑袋定的初值，需按真实知识截止时间校正。

### `_is_linewrap_space_artifact`：PDF 换行符被 LLM 转写成了空格

`native_pdf.py` 把 block 内多行用 `
` 拼接，而 PDF 按页宽换行不认汉字词边界，一个双字词常被从
中间断开（"教师"→"教
师"）。提示词要求 `original_text` 逐字取自正文，但真实案例显示 LLM 读到这类
跨行词会把 `
` 转写成一个空格再引用，于是 `original_text` 里出现一个源文档里并不存在的空格，被
当成"多余空格"上报（`original_text="教务管理教 师"`、`suggestion="应删除空格"`）。

判据：`original_text` 含空格 **且** 带空格原样不是 `block.text` 的子串 **且** 去掉所有空格后是
`block.text` 去掉所有换行符后的子串。

**整体去除后再子串匹配，不逐个空格去找对应的 `
` 位置**：真实案例里 LLM 插入空格的位置和 `
` 的
真实位置能差一个字符（"教务管理教 师" 的 `
` 实际在"理"和"教"之间，空格却落在"教"和"师"之间），
它不是在做逐字符替换，更像是按对这个词的理解重新组织了一遍再输出。要求逐位置精确对应会漏判。
**不要求 `issue_type` 必须是"错别字与拼写"**：子串匹配本身已经足够精确，收窄反而会漏掉 LLM 把同一
现象归到"标点符号问题"的场景。

### `_is_chunk_boundary_truncation`：LLM 在分块边界看不到后续内容

`config.OVERLAP_BLOCKS` 的重叠区只让后一块向前携带上一块的尾部供理解上文，**从未设计"预览下一块"**。
落在"所在 chunk 正文最后一个 block"里的内容，在 LLM 的视野里就是到此为止——它据此判断"这句话没说完"
是**合理的**，不是凭空捏造，所以不能靠改提示词指望它猜到自己看漏了内容，只能由系统层用确定性的结构
事实来识别。

判据：`block_index` 在 `tail_blocks` 里（`_compute_chunk_tail_blocks` 算出，全文档最后一个 chunk
天然排除——它后面确实没有内容了）**且** block 原文不以句末标点收尾 **且** 被 flag 的 `original_text`
原样是该 block 的结尾片段（排除"block 确实在 chunk 尾、但问题其实出在 block 中间别处"）。

**为什么不在解析阶段把这类 block 合并回去**：诊断真实案例时量过，被拆开的两个 block 之间的行间距
（约12pt）和同一悬挂缩进段落里"真正的新条目"之间几乎完全相同（同一份文档实测都在 12.2~13.0pt），
唯一可靠的区分信号是横坐标缩进——那是这份文档排版软件的具体几何特征，换个软件未必成立，而且完全
不覆盖 OCR/Word 两条通道。这里用的"是不是 chunk 正文尾块"是从 `ChunkedDocument` 直接读出的结构
事实，不依赖任何排版几何假设，还能覆盖"任意两个内容相关但被拆成不同 block、又恰好卡在 chunk 边界"
的更一般情况。

`chunked` 不传时 `tail_blocks` 为空集，这一条自动不生效（离线用 RawIssue JSON 调规则的调试场景），
其余两条不受影响。

测试见 `tests/test_classifier.py` "规则G/规则I/规则H" 三节。**规则I 的用例直接钉 `_is_linewrap_space_artifact`
本身，不走端到端**：端到端跑的话这条会被更靠前的"纯空格差异"过滤器先接住（两条都丢，但断言 warnings
会指向错的那一条），测不到"整体去除后子串匹配"这个关键点。

## 版式错乱措辞 / 建议与原文只差空格 → 整条丢弃

现象：即使前面的丢弃过滤器（视觉无实质改动/控制字符乱码）已经拦掉一部分假问题，真实数据
（`data/app.db` 35号记录，99条被判"确定性错误"的issue，抽样核实后逐一分类统计）显示仍有两类
稳定的残留：① LLM 自己在 `reason`/`suggestion` 里承认"此处排版错乱""跨行错位""乱码"——它是在
说"我也没看懂这段被解析打乱的版面"，不是在报语言错误；② `suggestion` 与 `original_text` 的唯一
差异是空格数量（`"2 0 2 5年9⽉1 1⽇"` → `"2025年9月11日"`），空格来自PDF跨行拼接、装饰性字间距
等排版原因。

判定在 `postprocess.py::_is_layout_or_space_artifact`，由 `_filter_visually_no_op` 在归层
**之前**调用，**整条丢弃**：命中 `config.LAYOUT_ARTIFACT_KEYWORDS`（关键词是从真实输出里摘的
原话），或 `_extract_replacement_pair`（与视觉无实质改动过滤共用）抽出的"旧→新"里至少一侧含
空格、且两者过 `_strip_whitespace(_normalize_lookalike(...))` 后完全相同。

**是丢弃、不是降级，这是用户的明确要求**：降级会留下一句"建议对照原文核实后再处理"，那本身
就是噪声——用户看到的仍是一条要处理的问题，而它百分之百不是原文的错。三个连带后果要知道：

- **引文类/风格类不豁免**：丢弃发生在归层之前，没有 layer 可看。不违反引文保护铁律——那条要的是"不对引文提改动建议"，而这里是把
  一条压根不存在的问题整个删掉，比"原文照录，不建议改动"更保守。
- **版式错乱那一支是关键词匹配，比另外两类宽**，真错误若被 LLM 描述成"跨行/错位"会一并丢掉。
  这是接受的代价，不再有"降级保留可审计性"这条退路。
- 丢弃数进 `warnings`（"丢弃N条版式错乱/纯空格差异的解析伪影"），在界面"提示信息"面板可见，
  不是悄无声息地消失。

**空格差异比较为什么要先套 `_normalize_lookalike` 再去空格**：真实案例里空格差异经常和部首替代字
**同时**出现在同一条issue里（`"2 0 2 5年9⽉1 1⽇"` 里 `⽉` 既是部首替代字又混着空格差异）——只对
原始文本去空格再比较，会因为 `⽉`≠`月` 判定"不匹配"而漏判。

**与规则I 不要混淆**：规则I 处理的是 `original_text` 里多出一个原文没有的空格（PDF 换行符被 LLM
转写成空格），靠与 `block.text` 做子串匹配证实，仍是降级；这里处理的是 `suggestion` 与
`original_text` 之间只差空格，不看 block。

测试见 `tests/test_classifier.py` "版式错乱措辞 / 建议与原文只差空格 → 整条丢弃" 一节五条用例
（版式错乱措辞丢弃、空格差异+部首替代字混合丢弃、真拼写错误不误伤、裸措辞且未定位仍丢弃、
引文类也照丢），全部做过反向验证。

**已知的残留场景（未覆盖，接受漏判）**：同一份真实文档里还有两类更难通用识别的假问题——①
原文段落被拆分/重排后不只是空格差异，而是**字符顺序整体打乱**（如封面标题"岁末盘点"被解析成
"岁 末点 盘"，去空格比较后内容也对不上）；② 某个特定字体把句号/逗号错误映射成生僻汉字"盓"
（这份文档里5处），是字体子集化bug的专属特征，不能泛化。另有一个与本节无关的既有缺口：
`base_rules.py::_rule_default` 对 `confidence=="high"` 无条件判"确定性错误"，不像 `_rule_factual`
那样检查 `suggestion` 是否已以"存疑"开头，真实数据里出现过"高置信度 + suggestion写着存疑"的
矛盾案例被错分到错误类。

## 精简模式下语法结构问题(规则2)统一按风格可选处理

精简模式服务的是"不需要出版级严格校对"的普通/技术文档场景，容忍度本来就比深度模式高，但"语法结构问题"（对应 `proofread_rules.md` 规则2）在这类文档里命中率不低，且很多命中其实是"怎么写都通"的润色，不是真正影响理解的病句——典型信号是LLM给出的建议里出现"改为A**或**B"这种平级备选写法（如"开始结束时间"→"改为'开始、结束时间'或'开始和结束时间'"），这说明没有唯一正确写法，纯粹是文字风格选择，继续走 `_rule_default` 兜底判成"确定性错误/存疑待核实"（`base_rules.py::_rule_default`）过于严格。

判定条件（`base_rules.py::_rule_simplified_grammar_as_style`，插在基础规则 A/B 之后、E 之前）：`mode == config.PROOFREAD_MODE_SIMPLIFIED` **且** `raw.issue_type == "语法结构问题"`，命中即直接归为"风格可选"+最低优先级，不再看 `confidence`。放在 A/B 之后是为了保证**引文保护、事实置信度降级这两条设计铁律不因精简模式被弱化**——即使在精简模式下，同一条issue如果先命中了quotation/factual的文本特征，仍会被规则A/B拦下，不会流到这条规则。深度模式（`mode` 不传或为 `PROOFREAD_MODE_DEEP`）完全不受影响，规则2依旧走原有的confidence判定路径。

`_BASE_RULES` 里所有规则统一签名 `(raw, mode)`，其中三条并不使用 `mode`——统一签名纯粹是为了 for 循环能一视同仁地调用，和 `modifier_rules.py` 的约定一致。测试见 `tests/test_classifier.py` "精简模式语法结构降级" 一节（深度模式不受影响的回归用例、精简模式下不分confidence都归风格可选、精简模式下quotation/factual仍优先于此规则生效）。

## 精简模式下规则1里"汉字冒充标点符号"这类零歧义问题按风格可选处理

和上面"精简模式语法结构降级"成因不同，不要混为一谈：一条真实issue——原文"管理控台一角色权限"，`issue_type` 是"错别字与拼写"，建议"将汉字'一'改为破折号'——'或短横线'-'"——质疑这类问题为什么要判成错别字。分析后发现：这类问题的本质是"用错了字形近似的**汉字**去代替本该用的**标点符号**"（数词"一"和破折号"—"/短横线"-"笔画/字形接近，容易在录入时误用），和"的/地/得"这类**可能真正改变语义或引起误解**的错别字不是一回事——"一"冒充破折号纯粹是排版惯例问题，读者对内容的理解**完全不受影响**，不管用哪个字符/符号，"管理控制台—角色权限"这句话的意思都是一样的。而"的/地/得"用错有时候真的会改变句子的语法角色或造成歧义。二者都被LLM归进了"错别字与拼写"这同一个 `issue_type`，但严格程度不该一刀切。

判定条件（`base_rules.py::_rule_simplified_typo_as_punctuation`，插在"精简模式语法结构降级"之后、规则E风格之前，和它是否谁先跑互不影响——两条规则的 `issue_type` 判断条件互斥）：`mode == config.PROOFREAD_MODE_SIMPLIFIED` **且** `raw.issue_type == "错别字与拼写"` **且** 建议/理由文字里命中 `config.SIMPLIFIED_TYPO_PUNCTUATION_KEYWORDS`（"破折号""连接号""短横线""分隔号""间隔号"，即建议是"把某个汉字改成某种连接类标点"这个模式）。命中即直接归为"风格可选"+最低优先级，不再看confidence。**没有走通用的文本特征启发式（比如识别"一"这个字本身），而是用建议措辞反推**——因为直接识别"原文里出现了'一'"太宽泛、极易误伤（"一"是最常见的汉字之一，绝大多数出现场景和标点毫无关系），而"LLM建议把某个字改成破折号/连接号"这个措辞模式本身就已经是"这是一次汉字冒充标点"的强信号，不需要再对原文本身做启发式判断。宁可漏判（真的有这类问题但LLM没用这几个关键词描述）也不误伤真正的错别字。

深度模式不受影响；引文保护/事实置信度降级（规则A/B）仍排在此规则之前，命中时优先生效，不会被这条规则弱化——和"精简模式语法结构降级"遵循同一条设计原则。测试见 `tests/test_classifier.py` "精简模式'汉字冒充标点'降级" 一节。

## LLM自陈"按规避规则本来就不该报" → 整条丢弃

人工反馈的规避主体不在本模块（见下一节），但有一个必须由代码层兜住的失败模式：提示词"补充规则三：历史反馈规避"要求命中规避规则的内容**根本不要输出**，而真实数据里LLM会照样输出一条issue，把"我为什么不该报它"写成建议正文（`suggestion='"预测 点检"因换行被拆分，根据历史反馈规避规则第三条，此类因换行导致的词语拆分不应报告为问题。'`）。这类条目的建议与 `original_text` 相差悬殊，`_is_visually_no_op_suggestion`/`_is_layout_or_space_artifact` 两条判据全都接不住，`category`/`confidence` 又是LLM按常规填的，一路落到 `_rule_default` 判成"确定性错误·中"——**编辑看到的仍是一条要处理的问题，规避等于没生效**。

判定在 `postprocess.py::_is_self_declared_non_issue`，由 `_filter_visually_no_op` 排在四类丢弃的**最前面**（LLM自己都判定这不是问题，不必再走后面几条更贵的判据），`reason`+`suggestion` 拼起来命中 `config.NON_ISSUE_SELF_DECLARATION_KEYWORDS` 即整条丢弃，丢弃数进 warnings（"丢弃N条LLM自陈不该报告的问题"）。

**关键词只收"报告/作为问题"这个模式和"历史反馈规避"，不收"无需修改""不建议改动"这类宽泛说法**：引文类issue的固定 `suggestion` 就是"原文照录，不建议改动"（提示词补充规则一强制），把"别改"类措辞收进来会把整个引文层连坐丢光，直接违反引文保护铁律。测试 `test_self_declared_filter_does_not_touch_quotation_boilerplate` 就是这条防线。

**这是提示词层与代码层的双保险，两处都要在**：`prompt/proofread_system.md` 的补充规则三同时被收紧为"不得出现在输出数组里，禁止为它输出一个条目去说明为什么不该报告"——提示词负责从源头少产出，代码层负责LLM不听话时兜底，与引文保护/事实置信度"系统层兜底校验，不能只信LLM自报"的设计铁律一致。

## 人工反馈规避的主体不在本模块

历史拒绝记录交给LLM做语义总结、注入校对提示词的"补充规则三：历史反馈规避"（见 `prompt/proofread_system.md`），让LLM在生成建议这一步就主动规避曾被拒绝的问题模式，不由分类器按反馈内容事后改判（上一节那条丢弃只认LLM的自陈措辞，不读反馈库）。详见 `core/feedback_rules.py` 模块docstring。`core/classifier` 因此不接受任何反馈相关参数，`_MODIFIER_RULES` 里所有规则统一签名 `(raw, block, tail_blocks, state)`，保持"纯规则逻辑，不调用LLM、不查库"。
