# core/classifier/ 关键点

`classify_issues(result: ProofreadResult, parsed: ParsedDocument, chunked: ChunkedDocument | None = None, mode: str = config.PROOFREAD_MODE_DEEP, learned_feedback: list = ()) -> ClassifiedResult`是唯一对外入口：对每条 `RawIssue` 做系统层强制归层校验，不信任 LLM 自报的 `category`/`confidence`，产出 `ClassifiedIssue`（带 `layer`/`priority`/`layer_notes`，并保留 `llm_category`/`llm_confidence`/`original_suggestion` 供追问溯源）。纯规则逻辑，不调用LLM、不查库。`chunked` 是可选参数，不传时（如离线用 `--from-json` 数据反复调规则）规则H不生效，其余规则不受影响。`learned_feedback`（阶段12新增）是 `core.feedback.load_learned_feedback()` 的产出，由调用方（`core/workflow/run.py`）一次性读库后以参数注入，驱动规则J；不传时该规则不生效。

## 目录结构

- `_types.py`：`ClassifiedIssue`/`ClassifiedResult`/`_ClassificationState`。
- `heuristics.py`：引文/事实文本特征启发式（`_has_quotation_feature`/`_has_factual_feature`），基础规则A/B的漏报兜底能力落点，规则J的事实类豁免也复用 `_has_factual_feature`。
- `base_rules.py`：基础归层规则A/B/E/F（`_rule_quotation`/`_rule_factual`/`_rule_style`/`_rule_default`，`_BASE_RULES` 元组，互斥、先命中先生效）。
- `modifier_rules.py`：修饰规则C/D/I/G/H/J+优先级覆盖，用 `_MODIFIER_RULES` 显式有序注册表声明执行顺序——想知道"规则G/H谁先跑"看这一个列表就够了，不用通读函数体；加新规则时往列表插一条，并写清楚为什么插在这个位置（是否依赖前面某条规则已改过layer/priority）。
- `postprocess.py`：跨块去重/排序/统计（`_dedup`/`_sort`/`_compute_stats`）。
- `__init__.py`：`classify_issue()`/`classify_issues()` 编排入口，对外只暴露 `ClassifiedIssue`/`ClassifiedResult`/`classify_issue`/`classify_issues`（`__all__`）。

## 归层结构：基础规则 + 修饰叠加两层，不是七条规则简单互斥

1. **基础归层**（互斥，先命中先生效）：规则A引文保护 → 规则B事实置信度 → **精简模式语法结构降级**（补丁，见下）→ **精简模式"汉字冒充标点"降级**（补丁，见下）→ 规则E风格 → 规则F默认兜底（必命中）。
2. **修饰叠加**（在基础归层结果之上，各自独立判断是否命中）：规则C——block的 `ocr_confidence` 低于 `config.OCR_CONF_DOWNGRADE_THRESHOLD`（默认0.90）且 `issue_type` 属于"错别字与拼写"/"标点符号问题"时，把已判定的"确定性错误"降级为"存疑待核实"（OCR可能认错字，不代表原文真错）；规则D——`located=False` 时同样把"确定性错误"封顶降级为"存疑待核实"；规则I——`original_text` 里的空格若被证实是PDF内部换行符被LLM转写产生的伪影（原文里并无此空格），同样封顶降级（详见下方"规则I"一节）；规则J——同一类问题被人工反复拒绝达到阈值后，自动降级为"风格可选"（详见下方"规则J"一节；**注意这条规则和C/D/G/H/I不同，它不是"封顶到存疑待核实"，而是会一路降到风格可选层**）。
3. 最后叠加优先级覆盖：`issue_type` 属于"政治敏感性表述"/"民族与地名规范"，无论前面落在哪一层，`priority` 强制改为'高'。

判定原因：提示词原文里规则C/D的处理措辞是"**原**layer若为确定性错误→降级"、"**最高只能到**存疑待核实"，这类表述预设"已经有一个layer存在"，是修饰而非独立分支——按字面顺序当六条互斥规则逐条测试会导致C/D的命中条件（OCR置信度、located字段）永远无法与A/B/E已经命中的情况共同生效。`tests/test_stage5.py::test_rule_c_stacks_on_top_of_base_rule_f` 专门验证这一叠加行为（F判定确定性错误后被C的OCR降级修饰，`layer_notes` 里两条依据都保留）。

其余启发式兜底规则（弥补LLM未自报 `category` 或 `issue_type` 判断不准的漏报场景，均在 `config.py` 里可调，宁可漏判不复杂化）：规则A额外识别书名号《》、≥10字的成对引号、文言虚词密度；规则B额外识别人名职务/机构名关键词+年份格式的"改写型"建议。**这两条兜底能力是设计铁律第2、3条"系统层兜底、不能只信LLM自报"的直接体现**——`tests/test_stage5.py::test_rule_a_book_title_heuristic_overrides_llm_miss` 就是验证"LLM没自报quotation，系统仍强制保护"的核心断言。

跨块去重（`postprocess.py::_dedup`）按 `(block_index, 归一化original_text)` 分组，保留更保守的一条（保守度：引文类>存疑待核实>风格可选>确定性错误）；`located=False` 的条目没有可靠 `block_index`，不参与去重、原样全部保留。排序直接按 `block_index` 升序（`block_index` 本身是解析阶段按阅读顺序分配的全局序号，天然满足"页码升序,同页按block_index"，未定位条目排最后）。`stats` 字段名（`total_issues`/`count_confirmed`/`count_doubtful`/`count_quotation`/`count_optional`/`high_priority_count`）与 `db/database.py` 里 `records` 表列名逐一对齐，供 `core/workflow/`/`core/exporter.py` 直接写库。

调试用 `tools/preview_classify.py <file> [--max-chunks N]`（全链路，耗额度）或 `tools/preview_classify.py <file> --from-json <path>`（复用 `tools/run_proofread.py --save-json` 存的 `RawIssue` 数据离线反复调规则，不耗额度，仍需原文件路径以便重新 `parse_document` 拿 `ParsedDocument` 供规则C查OCR置信度）。

## 规则G：知识时效性误判豁免

现象：LLM可能仅因为文中某个年份超出了它的训练数据覆盖范围就产生怀疑（"这个日期看起来太新/我没见过"），但这种怀疑**只针对"这个时间点本身是否存在"，不针对"该时间点发生的事是否属实"**——是模型知识时效性局限造成的误判，不是真正的事实性错误，且这类问题在真实文档里出现频率不低，值得单独处理。

判定条件（`modifier_rules.py::_apply_recency_downgrade`，作为第三个"修饰"叠加在OCR降级、未定位封顶之后）：`issue_type`/`category` 落在事实类范围内 **且** `reason`/`suggestion` 命中 `config.RECENCY_DOUBT_KEYWORDS`（"训练数据""知识截止""较新"等怀疑"时间太新"的措辞，不是泛泛的事实怀疑）**且** 原文里最大的年份落在 `[config.LLM_KNOWLEDGE_CUTOFF_YEAR, +config.KNOWLEDGE_CUTOFF_GRACE_YEARS]` 区间内——超过宽限期（默认10年）视为真的离谱，不豁免，保留原判定供人工核实。命中后**不是直接从结果里剔除**，而是强制降级为"存疑待核实"+最低优先级（`PRIORITY_LOW`），并在 `suggestion` 前缀说明"疑似因AI知识时效性产生的误判，建议直接忽略"、`layer_notes` 记录判定依据——选择降级而非剔除，是为了保留可审计性：用户在界面上一眼就能判断要不要忽略，即使关键词误伤了真正的事实性错误，信息也不会凭空消失。`config.LLM_KNOWLEDGE_CUTOFF_YEAR` 是按当前配置模型（`qwen3.6-plus`）拍脑袋定的初值，需要按真实知识截止时间校正；`RECENCY_DOUBT_KEYWORDS` 同样是初版关键词表，宁可漏判不复杂化，后续按真实LLM措辞调整。测试见 `tests/test_stage5.py` "规则G" 一节四条用例（容忍窗口内降级、超出宽限期不豁免、无"时效性"措辞不触发、非事实类issue_type不触发）。

## 规则H：分块(chunk)边界截断误判豁免

与规则G成因不同，不要混淆。现象与规则G表面相似（都是"看起来不完整/存疑"），但根因完全不同：真实文档里一段用悬挂缩进排版的列表条目（如"分类标签、\n浏览量、发布时间..."）被PyMuPDF在提取阶段拆成了两个独立 `ParsedBlock`（这本身是`core/parser/native_pdf.py`的一处已知不精确之处，几何上很难用一般规则可靠合并回一个block，见 [core/parser/CLAUDE.md](../parser/CLAUDE.md)），又恰好被 `core/chunker/` 的贪心装填算法切在两个不同的chunk里——前一个block变成chunk K正文的最后一块，后一个block变成chunk K+1正文的第一块。`config.OVERLAP_BLOCKS` 的"重叠区"设计只让chunk K+1向后携带chunk K的尾部内容供理解上文，从未设计"向前预览下一chunk"，所以LLM校对chunk K时，看到的正文原文就在"...分类标签、"处硬生生截断，将"分块导致看不到后续"误判成"内容不完整、标点缺失"——这是LLM基于它能看到的（不完整的）内容做出的合理判断，不是它凭空捏造错误，因此不能靠改prompt单方面指望LLM"猜到"自己看漏了内容，需要系统层能确定性地知道"这个block在这个位置就是被截断了"。

判定条件（`modifier_rules.py::_apply_chunk_boundary_downgrade`，作为第四个"修饰"叠加在规则G之后）：issue所在 `block_index` 是它所在chunk **正文**部分（不含重叠区）的最后一个block **且** 该chunk不是全文档最后一个chunk（`_compute_chunk_tail_blocks` 用 `ChunkedDocument.chunks[i].block_indices[-1]` 算出这个集合，全文档最后一个chunk天然排除——它后面确实没有更多内容了，不存在"看不到后续"的问题）**且** 该block原始文本本身不以句末标点（`。！？`）收尾（正常收尾的段落即使恰好卡在chunk边界也不该被怀疑）**且** 被flag的 `original_text` 原样是该block文本的结尾片段（`block.text.endswith(original_text)`，排除"block确实在chunk尾但issue其实出在block中间别处"的情况）。只在基础归层已判定为"确定性错误"时才降级（存疑/引文/风格本身已经比确定性错误宽松，无需再降）——和规则C/D的"降级不剔除"哲学一致，命中后强制改判"存疑待核实"+最低优先级，`suggestion` 前缀说明"疑似因文档分块处理...被分块边界截断"，`layer_notes` 记录判定依据，保留可审计性。

**为什么不在解析阶段（`core/parser/native_pdf.py`）直接合并这类被拆开的block**：诊断这次真实案例时发现，被拆开的两个block之间的行间距（约12pt）和同一悬挂缩进段落里"真正的新条目"之间的行间距几乎完全相同（同一份文档实测都在12.2~13.0pt），唯一能可靠区分"这是同一段的换行"还是"这是新条目"的信号是横坐标缩进（延续行统一缩进到x0=111，新条目统一从x0=90起，且新条目行首带项目符号字符）——这是这份文档排版软件生成PDF的具体几何特征，换一份用不同软件/不同悬挂缩进量生成的PDF未必成立，贸然把这套坐标启发式写进 `native_pdf.py` 会带来新的误合并风险（比如把两个本来独立的短条目错误粘成一段），且完全不覆盖OCR(B类)/Word(C类)两条通道（它们根本没有可比较的坐标信息）。相比之下，规则H命中的"block是chunk正文尾块"是从 `ChunkedDocument` 直接读出的确定性结构事实，不依赖任何长文档排版的几何假设，能覆盖"任意两个内容相关但被拆成不同block、又恰好卡在chunk边界"的更一般情况，不只是这一种悬挂缩进列表的样式。测试见 `tests/test_stage5.py` "规则H" 一节六条用例（真实场景复现降级、正常句末标点收尾不豁免、全文档最后一个chunk不豁免、block非chunk尾块不豁免、flag文本不在block结尾不豁免、不传chunked时规则不生效）；另有脚本对真实文档（`data/uploads/` 里的"后台管理员使用手册"样本，`record_id=8/9` 报告的原始issue）直接复现验证：`block_index=75` 命中规则H，`layer` 从"确定性错误"改判为"存疑待核实"，`priority` 从"中"改判为"低"。

## 规则I：PDF换行符被LLM转写成空格的解析伪影豁免

真实使用中发现后追加，和规则H成因不同（都涉及"PDF换行/分块导致的解析伪影"这个大主题，但触发机制完全不是一回事，不要混淆）：规则H是**分块(chunk)边界**截断导致LLM看不到后续内容；规则I是**block内部**——`core/parser/native_pdf.py` 把同一个PyMuPDF block内的多个PDF行用换行符(`\n`)拼接（详见 [core/parser/CLAUDE.md](../parser/CLAUDE.md)"block内多行拼接必须用换行符"一节），但PDF按页宽自动换行完全不认汉字词语边界，经常把一个双字词从中间断开（如"教师"被拆成上一行末尾的"教"、下一行开头的"师"），`block.text` 里就会出现"...教\n师..."这种词语中间夹着换行符的情况。系统提示词要求 `original_text` "必须逐字取自正文"，但真实案例显示LLM读到这类跨行词语时会把 `\n` 转写/归一化成一个空格再引用（而不是照原样保留 `\n`，或者干脆去掉 `\n` 直接拼接），导致 `original_text` 里出现一个源文档里其实并不存在的空格，进而被当成"多余空格/错别字"上报（真实案例：`original_text="教务管理教 师"`，`suggestion="应删除空格"`，但原文里这个位置根本没有空格，只有一个被拼接掉的换行符）。

判定条件（`modifier_rules.py::_apply_linewrap_space_artifact_downgrade`，插在未定位封顶(规则D)之后、知识时效性豁免(规则G)之前）：`state.layer` 是"确定性错误"（更宽松的层级不需要再降）**且** `original_text` 含空格 **且** `original_text`（带空格原样）不是 `block.text` 的子串（证明这个空格不是原文/解析结果真实携带的）**且** `original_text` 去掉所有空格后是 `block.text` 去掉所有换行符后的子串（证明"去掉这些空格/换行符差异，内容确实原样来自这个block"）。命中后强制改判"存疑待核实"+最低优先级，`suggestion` 前缀说明"疑似因PDF内部排版换行被误转写产生，原文中并无此空格"，`layer_notes` 记录判定依据，与规则C/D/H"降级不剔除、保留可审计性"的哲学一致。

**为什么整体去除后子串匹配，而不是精确定位单个空格对应的 `\n` 位置再逐一替换验证**：真实案例验证过，LLM转写时插入空格的具体字符位置和 `block.text` 里 `\n` 的真实位置可能相差一个字符——例如上面"教务管理教 师"这个真实例子，`block.text` 里 `\n` 实际在"教务管理"和"教师无组织权限"之间（即"理"和第二个"教"之间），但LLM转写出的空格却落在"教"和"师"之间（晚了一个字符）；这说明LLM不是逐字符做"\n→空格"的精确单点替换，更像是识别出这是同一个词语被换行拆开后，按自己对这个词语的理解重新组织了一遍再输出——如果按"逐个空格找对应\n位置做精确替换验证"的思路实现，这个真实案例会因为位置差一位而被判定为"不匹配"，从而漏判。改成"整体去除空格/去除换行符后做子串匹配"，就不依赖任何精确位置对应关系，只要求"内容确实来自原文、多出来的字符只是空格"这个更宽松也更符合真实LLM行为的条件。

**为什么不要求 `issue_type` 必须是"错别字与拼写"**：检测条件本身（整体子串匹配）已经足够精确、误伤风险很低，额外用 `issue_type` 收窄反而可能漏掉LLM把同一现象误归类到"标点符号问题"等其他类型的场景，这条规则不区分 `mode`（深度/精简都生效）——它修的是"LLM转写出了原文里根本不存在的内容"这个正确性问题，不是"该按多严格的标准校对"这类容忍度问题，跟精简/深度模式的定位是两回事。测试见 `tests/test_stage5.py` "规则I" 一节。

## 补丁：精简模式下语法结构问题(规则2)统一按风格可选处理

真实使用中发现后追加，非精简/深度模式功能原始设计的一部分。起因：精简模式服务的是"不需要出版级严格校对"的普通/技术文档场景，容忍度本来就比深度模式高，但"语法结构问题"（对应 `proofread_rules.md` 规则2）在这类文档里命中率不低，且很多命中其实是"怎么写都通"的润色，不是真正影响理解的病句——典型信号是LLM给出的建议里出现"改为A**或**B"这种平级备选写法（如"开始结束时间"→"改为'开始、结束时间'或'开始和结束时间'"），这说明没有唯一正确写法，纯粹是文字风格选择，继续走 `_rule_default` 兜底判成"确定性错误/存疑待核实"（`base_rules.py::_rule_default`）过于严格。

判定条件（`base_rules.py::_rule_simplified_grammar_as_style`，插在基础规则 A/B 之后、E 之前）：`mode == config.PROOFREAD_MODE_SIMPLIFIED` **且** `raw.issue_type == "语法结构问题"`，命中即直接归为"风格可选"+最低优先级，不再看 `confidence`。放在 A/B 之后是为了保证**引文保护、事实置信度降级这两条设计铁律不因精简模式被弱化**——即使在精简模式下，同一条issue如果先命中了quotation/factual的文本特征，仍会被规则A/B拦下，不会流到这条规则。深度模式（`mode` 不传或为 `PROOFREAD_MODE_DEEP`）完全不受影响，规则2依旧走原有的confidence判定路径。

`mode` 参数是这条规则新增才从 `classify_issues()`/`classify_issue()` 打通到 `_BASE_RULES` 里的（其余四条基础规则原先只依赖 `raw` 一个参数，签名统一加宽为 `(raw, mode)` 后三条不使用该参数，纯粹是为了让 `_BASE_RULES` 元组里所有规则保持统一签名，方便 for 循环调用，和 `modifier_rules.py` 的既有约定一致）。测试见 `tests/test_stage5.py` "精简模式语法结构降级" 一节（深度模式不受影响的回归用例、精简模式下不分confidence都归风格可选、精简模式下quotation/factual仍优先于此规则生效）。

## 补丁：精简模式下规则1里"汉字冒充标点符号"这类零歧义问题按风格可选处理

真实使用中发现后追加，和上面"精简模式语法结构降级"是同一批反馈里的第二个问题，但成因不同，不要混为一谈。起因：用户看到一条真实issue——原文"管理控台一角色权限"，`issue_type` 是"错别字与拼写"，建议"将汉字'一'改为破折号'——'或短横线'-'"——质疑这类问题为什么要判成错别字。分析后发现：这类问题的本质是"用错了字形近似的**汉字**去代替本该用的**标点符号**"（数词"一"和破折号"—"/短横线"-"笔画/字形接近，容易在录入时误用），和"的/地/得"这类**可能真正改变语义或引起误解**的错别字不是一回事——"一"冒充破折号纯粹是排版惯例问题，读者对内容的理解**完全不受影响**，不管用哪个字符/符号，"管理控制台—角色权限"这句话的意思都是一样的。而"的/地/得"用错有时候真的会改变句子的语法角色或造成歧义。二者都被LLM归进了"错别字与拼写"这同一个 `issue_type`，但严格程度不该一刀切。

判定条件（`base_rules.py::_rule_simplified_typo_as_punctuation`，插在"精简模式语法结构降级"之后、规则E风格之前，和它是否谁先跑互不影响——两条规则的 `issue_type` 判断条件互斥）：`mode == config.PROOFREAD_MODE_SIMPLIFIED` **且** `raw.issue_type == "错别字与拼写"` **且** 建议/理由文字里命中 `config.SIMPLIFIED_TYPO_PUNCTUATION_KEYWORDS`（"破折号""连接号""短横线""分隔号""间隔号"，即建议是"把某个汉字改成某种连接类标点"这个模式）。命中即直接归为"风格可选"+最低优先级，不再看confidence。**没有走通用的文本特征启发式（比如识别"一"这个字本身），而是用建议措辞反推**——因为直接识别"原文里出现了'一'"太宽泛、极易误伤（"一"是最常见的汉字之一，绝大多数出现场景和标点毫无关系），而"LLM建议把某个字改成破折号/连接号"这个措辞模式本身就已经是"这是一次汉字冒充标点"的强信号，不需要再对原文本身做启发式判断。宁可漏判（真的有这类问题但LLM没用这几个关键词描述）也不误伤真正的错别字。

深度模式不受影响；引文保护/事实置信度降级（规则A/B）仍排在此规则之前，命中时优先生效，不会被这条规则弱化——和"精简模式语法结构降级"遵循同一条设计原则。测试见 `tests/test_stage5.py` "精简模式'汉字冒充标点'降级" 一节。

## 规则J：人工反馈学习自动降级

真实使用中发现后追加（阶段12）。起因：用户实测发现，同一类被人工判定"判错了"的问题（点击"拒绝"）会在后续校对（同一文档的其他位置、后续上传的同类文档）里反复出现，需要重新拒绝一遍，助手没有任何"记忆"。真实例子：原文"管理控台一组织权限一用户管理"，`issue_type`=错别字与拼写，`suggestion`="将'一'改为'-'或'>'等规范的路径分隔符"——这是没有意义的改动，用户每次都会拒绝，但换一份完全不同的文档、原文完全不同，只要同样把"一"当路径分隔符使用，这条issue又会重新冒出来。

判定条件（`modifier_rules.py::_apply_learned_feedback_downgrade`，插在分块边界截断豁免(规则H)之后、优先级覆盖之前）：`state.layer` 不是 `LAYER_QUOTATION`/`LAYER_OPTIONAL`（引文类是独立保护层级，语义与"风格可选"不同，不应被这条规则改写；已经是风格可选不需要重复处理）**且** 不是事实类问题（`_is_factual_issue`：`category=="factual"` 或 `issue_type=="常识与事实性错误"` 或命中 `_has_factual_feature`——三个信号和规则B判定"是不是事实类"的口径完全一致，不能只查前两个，否则会漏掉"LLM没自报factual、但命中了人名/职务/机构/年份特征启发式"这一类）**且** `core.feedback.count_similar_rejections(issue_type, original_text, suggestion, reason, learned_feedback)` 算出的历史命中次数达到 `config.FEEDBACK_REJECTION_THRESHOLD`（用户指定初值3）。

**匹配信号（补丁，用户实测后要求扩展）**：`count_similar_rejections`（`core/feedback.py`）做三档确定性字符串匹配，不引入向量/语义模型：①同 `issue_type` 下 `original_text` 精确重复；②`suggestion` 归一化后用 `difflib.SequenceMatcher.ratio()` 算相似度达到 `config.FEEDBACK_SIMILARITY_THRESHOLD`；③`reason` 归一化后同样算相似度达到该阈值——②③任一档命中都算，不要求同时命中。

**归一化**（`core/feedback.py::_normalize_variable_parts`）：把引号包裹的具体内容、数字统一替换成占位符再比较。起因（真实案例）：文档里编号1~7的列表项都因为"全角句号应改成半角"被人工拒绝过（`suggestion` 形如`应改为"N.某项"或"N. 某项"`，`N`和具体项目文字每条都不同），但编号10再出现同一种问题时，`suggestion` 整句字面相似度只有约0.48（阈值0.70/0.60都够不到）——不同的具体编号/文字内容把"改写模板相同"这个真正该识别的信号淹没了。归一化后两条 `suggestion` 都变成`应改为Q或Q`，相似度变成1.0，能正确识别为同一类；真正无关的内容归一化后 ratio 仍接近0（如"将'一'改为路径分隔符"vs"建议调整语序使句子更通顺"，归一化后仍是0.0），不会带来明显误伤。

**为什么现在也用 `reason`，且和 `suggestion` 同等对待（OR，不分主次）**：早期版本判定信号只用 `suggestion`、不用 `reason`，理由是"`reason` 措辞更随意/模板化，容易在完全无关的问题之间偶然撞相似"——但归一化本身已经去掉了大部分"偶然撞相似"的噪音来源（具体数字/具体文字这类最容易造成误判的可变部分），`reason` 和 `suggestion` 面临的风险不再有本质差异，用户明确要求把 `reason` 也纳入判定。`suggestion` 在实时校对流程和历史记录页两种 `issue` 对象上都完整可用；`reason` 只有实时流程当次内存里的 `ClassifiedIssue` 才有，历史记录页复用的 `_row_to_issue_view` 包装对象没有这个属性——`count_similar_rejections` 对 `reason` 为空的一侧直接跳过这档信号（不当空字符串比较），不会因此产生虚假匹配。

**阈值从0.70调低到0.60**（`config.FEEDBACK_SIMILARITY_THRESHOLD`，改名前叫 `FEEDBACK_SUGGESTION_SIMILARITY_THRESHOLD`，因为现在不止用于 `suggestion`）：归一化已经把最容易造成误判的可变内容剥离，剩下的字面差异更能反映"是不是同一种改写模板"，不需要原来那么高的门槛。真实数据验证：日期空格类问题（`suggestion` 措辞略有出入）归一化后 ratio 约0.615，0.70阈值下漏判、0.60阈值下能命中；数字单位间空格类问题归一化后 ratio 约0.759，两个阈值下都能命中；真正无关内容归一化后 ratio 仍接近0，调低阈值不会带来明显误伤。

事实类问题为什么永久不参与自动降级：这是项目根 `CLAUDE.md` 设计铁律"涉及人名/职务/历史事实的修改建议…禁止确定性结论"的延伸，已用 AskUserQuestion 与用户确认——事实性内容必须始终保留人工复核机会，不能因为被反复拒绝就被系统自动弱化保护。这类问题的拒绝仍然会被 `core.feedback.record_rejection` 记录（用于管理页查看/审计），只是不会驱动这条规则触发。

放在 `_MODIFIER_RULES` 里其余降级规则(C/D/I/G/H)之后、优先级覆盖之前：这条规则要看的是"系统层其它规则都处理完之后的最终层级"，人工反馈的降级判断不应该被其他规则的处理顺序打断。**架构上需要注意的一点**：这是第一条会把 `layer` 一路降到 `LAYER_OPTIONAL` 的 modifier 规则——C/D/G/H/I 都只把 `LAYER_CONFIRMED` 封顶降到 `LAYER_DOUBTFUL`（"封顶降级"，从未产出 `LAYER_OPTIONAL`，这此前只有 `base_rules.py` 里两条精简模式补丁才会做），规则J是 modifier 层级第一次跳出这个既有模式，以后阅读 `modifier_rules.py` 不要想当然认为"修饰规则只会封顶到存疑待核实"。

分类器本身不查库：`learned_feedback` 由 `core/workflow/run.py` 在每次校对开始时调用 `core.feedback.load_learned_feedback(db_path)` 一次性读取，以参数形式注入 `classify_issues`/`classify_issue`，`core/classifier` 内部只导入 `core.feedback` 里的纯函数 `count_similar_rejections` 和数据类 `LearnedFeedback`，不导入任何DB读写函数——保持"纯规则逻辑，不调用LLM/不查库"的设计定位。

测试见 `tests/test_stage5.py` "规则J：人工反馈学习自动降级" 一节（低于阈值不降级、`original_text` 精确匹配命中降级、`suggestion` 相似度命中降级、**归一化后跨编号命中降级**（真实"1~7.某项"→"10.发布成绩"场景）、事实类问题即使远超阈值也不降级、引文层不受影响、已是风格可选不重复处理、`classify_issues()` 顶层 `learned_feedback` 参数透传）；`core/feedback.py` 自身的记录/查询/匹配逻辑（含归一化、`reason` 相似度匹配）测试见 `tests/test_stage12.py`。
