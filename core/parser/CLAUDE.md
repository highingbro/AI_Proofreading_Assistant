# core/parser/ 关键点

统一解析三类输入，PDF 按有无文字层分两条路径：

- **A类：有文字层PDF**（`native_pdf.py`）—— PyMuPDF 按坐标提取文本块，手写分栏检测 + 阅读顺序还原。分栏算法在 `_columns.py`（设计理由写在该文件顶部 docstring）、阈值是 `config.py` 里的 `COLUMN_A_*` 一组，改之前先看下方"分栏检测"一节和那些注释解释的误判场景。
- **B类：无文字层扫描PDF**（`ocr_pdf.py`）—— 渲染为图片后用 PaddleOCR（版面检测 + 文本识别独立模型组合）+ paddlex 的 XY-Cut 算法还原阅读顺序。
- **C类：Word**（`docx_parser.py`）—— python-docx 按段落顺序读取。先 try `docx.Document(path)`，撞上 `KeyError` 才退化到 `_repair_dangling_relationships` 修复后重试，详见下方"docx断链关系记录"一节。

`_columns.py` 存放A类通道的分栏检测（分层占用率剖面）；`_common.py` 存放B类通道的分栏判定（`_find_extent_gap`/`_split_region`/`_detect_column_split`）以及 A/B 共用的位置描述拼装 `_source_location`——两套分栏算法并存的原因见下方"分栏检测"一节；`_cjk_variants.py` 存放A类通道专用的字形变体字符（康熙部首等）源头归一化逻辑，详见下方"字形变体字符在源头归一化"一节；`_types.py` 存放公共数据结构（`ParsedBlock`/`ParsedDocument`）与异常，避免子模块互相反向依赖 `__init__.py` 造成循环导入；`__init__.py` 是唯一对外入口 `parse_document`，内部做 A/B 两类逐页结果的合并编排（`_parse_pdf`/`_majority_layout_mode`）。

测试里 monkeypatch `_get_layout_pipeline`/`_run_structure` 等私有函数时要 `from core.parser import ocr_pdf as parser`——这些函数定义在 `ocr_pdf.py` 里，不是包顶层（见 `tests/test_parser.py`）。

`config.PADDLE_DEVICE = "auto"`：运行时自动选 GPU/CPU。GPU 推理快一到两个数量级，且 CPU 路径下有已知的 MKL-DNN+PIR 算子兼容问题（细节见 `config.py` 里 `PADDLE_DEVICE` 上方注释和 `ocr_pdf.py` 中对应处理）——排查 OCR 相关问题时留意这点。

调试解析效果用 `tools/preview_parse.py`，不必启动 Streamlit。

## Windows 下的编码坑（调试OCR脚本时必看）

**把OCR相关调试脚本输出重定向到文件时，先执行一次 `chcp 65001`**：PaddleOCR/PaddleX初始化时会调用一个Windows子进程（ccache/模型缓存查找之类），其自身输出走的是系统默认代码页（中文Windows下是GBK），不受Python `-X utf8` 影响；如果不切代码页，重定向出的文件会出现UTF-8正文夹杂GBK字节的混合编码，单一编码的查看器打开要么局部乱码要么整份文件被带偏。`chcp 65001` 让该子进程也按UTF-8输出，从源头解决，不是靠后处理"转换"能修好的。**这条乱码只出现在这一行诊断信息上，真正的校对内容（问题列表、建议等）全程是正常UTF-8文本，不受影响**——因为乱码源头是这一个子进程调用，不是校对流程本身。

**`chcp 65001` 必须在真正的Windows控制台（cmd.exe/PowerShell）里执行才有效，Git Bash（MinTTY伪终端）里执行是静默失效的**：MinTTY不是真正的Windows控制台，其内部跑 `chcp` 会报 `command not found`（`chcp.com` 虽在 `PATH` 里但MinTTY环境下无法按控制台方式解析执行），若用 `chcp 65001 >/dev/null 2>&1` 这种写法会把这个失败连同其错误一起吞掉，看起来像是执行了，实际上什么也没发生，乱码依旧存在。已实测验证：同一段代码，PowerShell里执行 `chcp 65001` 后乱码消失（该行诊断信息变成正常英文 `INFO: Could not find files for the given pattern(s).`），Git Bash里执行同样命令乱码依旧——**调试脚本涉及OCR/PaddleX且要重定向输出到文件时，用 PowerShell 工具而不是 Bash 工具**。

**另一条独立的乱码成因（别和上面这条混淆）：直接用 `python.exe script.py`（不带 `-X utf8`）跑脚本，且该次调用的 stdout 被工具harness捕获（相当于重定向到管道，不是交互式控制台），Python 自身的 `print()`/`logging` 输出会整体按系统ANSI代码页（中文Windows是GBK）编码，即使之前执行过 `chcp 65001` 也没用**——`chcp` 只改变控制台的活动代码页，Python 在 stdout 非真实控制台（被管道/重定向捕获）时不会去查询它，而是用 `locale.getpreferredencoding()`（读系统区域设置，不受 `chcp` 影响）。这次踩到的表现是**整个脚本的输出（不只PaddleX那一行）全部乱码**，比上面那条更严重，但本质是同一类"代码页不一致"问题的另一处触发点。修复方式：调用时显式加 `-X utf8` 参数（如 `python.exe -X utf8 script.py`），别只靠 `chcp`。之前"未受影响"的调试脚本之所以正常，是因为当时的调用方式已经带了 `-X utf8`；这不是自动生效的，每次新写调试脚本调用命令时要记得加。**这纯粹是终端显示/日志可读性问题，不影响程序实际行为**——本次验证中受影响的脚本所有 `assert` 都正常通过、写入SQLite的数据事后用 `-X utf8` 重新读取核对完全正常（不受这个显示层问题影响），因为数据库存取全程走的是Python内存里的unicode字符串，从未经过这层控制台编码。

## docx断链关系记录（`.rels` Target 指向 zip 中不存在的部件）

真实文档诊断（一份从PDF转换来的docx白皮书）发现：`docx.Document(path)` 打开时抛出裸 `KeyError: "There is no item named 'NULL' in the archive"`——`word/_rels/document.xml.rels` 里有一条 `<Relationship Type=".../image" Target="../NULL"/>`，某张图片的关系记录Target被生成工具写成了字面意义上的"NULL"，压缩包里根本没有这个部件。

**根因**：`python-docx` 打开文件时会急切读取所有关系记录指向的部件，不区分是不是图片、用不用得上；一条Target指向不存在部件的"断链"记录就会让整份文件读不出来。Word本身对这种情况很宽容——那张图就是不显示，正文照常打开，不会弹出明显的损坏提示，所以人工用Word打开完全看不出问题，只有我们这边报错。

**修复**：`docx_parser.py::parse_docx` 先按正常路径 `docx.Document(str(path))` 打开；撞上 `KeyError` 才退化到 `_repair_dangling_relationships(path)`——在原始zip层面解析每个 `.rels` 文件里的 `Relationship`，把 `Target` 解析成zip内的实际成员名（`posixpath` 纯字符串处理 `../` 相对路径，不碰文件系统），解析后在zip里找不到对应成员的记录直接移除（`TargetMode="External"` 的超链接类关系不算断链，跳过）；有改动才在内存里（`io.BytesIO`，不落临时文件）重新打包一份修复后的zip，交给 `docx.Document()` 重新打开。`parse_docx` 只读取段落文字，不涉及图片/媒体部件，摘掉断链关系记录对解析结果没有任何影响。

**范围**：只处理"关系记录指向的部件在zip里确实不存在"这一种断链场景，不是通用的docx修复/校验工具；如果 `KeyError` 是其他原因导致的，修复重试依然会失败，异常照常往上抛，不比修复前更差。

测试见 `tests/test_parser.py::test_parse_docx_repairs_dangling_relationship_and_still_extracts_text`（构造真实带断链关系记录的docx，验证修复前python-docx确实打不开、修复后能正常提取正文）和 `::test_repair_dangling_relationships_returns_original_path_when_no_dangling_refs`（没有断链时不做多余的zip重写）。

## docx段落文字被 `<w:sdt>` 内容控件包裹时会被静默丢字

真实文档诊断（一份经WPS审阅工具处理过的docx，标签 `tag="reviewtag_"`）发现：段落中间一截文字
（"业务类型：企业"）被包在 `<w:sdt><w:sdtContent>...</w:sdtContent></w:sdt>`（Word/WPS的"内容
控件"，常见于第三方审阅/校对工具给某段文字打标记时留下的壳）里，`parse_docx` 用的
`python-docx` `Paragraph.text` 读出来的正文这一截整体消失，且没有任何异常或警告——原句"目前
公司有四种业务类型：企业业务、厂商业务、政府业务、咨询业务"被读成"目前公司有四种业务、厂商
业务、政府业务、咨询业务"（三项却称"四种"）。这份被截断的文字送进LLM校对后，LLM精确复现了
原文真实缺失的那部分内容作为"修改建议"，看起来像是一次成功的校对，实际上原文根本没有这个问题
——报出来的"错误"是我们自己解析层丢字导致的伪影，不是文档真的有问题。

**根因**：`python-docx` 的 `Paragraph.text` 委托给 oxml层 `CT_P.text`，其实现是
`"".join(e.text for e in self.xpath("w:r | w:hyperlink"))`——这条xpath不带 `.//`，只认
`<w:p>` 的**直接子元素**，`<w:r>` 被套进 `<w:sdt>` 之类的壳之后就不再是直接子元素，被静默跳过。
这不是python-docx的bug，是它对"运行时（runtime）内容控件"这类结构未作特殊处理的已知行为边界。

**修复**：`docx_parser.py::_paragraph_full_text` 改用 `para._p.iter()` 遍历段落下**所有后代
元素**（不限层级，document order），只挑 `w:t`/`w:tab`/`w:br`/`w:cr`/`w:noBreakHyphen` 这几
类文本语义元素翻译成对应字符拼接——不止修 `w:sdt` 这一种壳，`w:ins`（追踪的插入修订）等任何
"文字被包了一层非 `w:r` 直接子元素"的情况一并覆盖。`w:delText`（追踪修订里被删除的文字）不在
识别范围内，本来就不该算作当前正文；这里也没有处理更复杂的"显示/隐藏修订标记"语义，只解决
"文字整体消失"这一种问题。

测试见 `tests/test_parser.py::test_parse_docx_reads_text_wrapped_in_content_control`（构造
段落文字被 `<w:sdt>` 拦腰截断的docx，先验证 `python-docx` 原生 `Paragraph.text` 确实丢字，
再验证修复后能读全）。

## 分栏检测（A类走 `_columns.py`，B类走 `_common.py`，两套并存）

**为什么分栏检测值得这么多代码**：真实文档诊断（`预览版 8-9月 电子版-2026e-works活动计划`、`数字化企业期刊79期/82期` 三份期刊）发现，"排版导致的假错误"绝大多数不是字符识别问题，而是**栏数判错以后 `_order_native_page` 把不同栏的块按 y 坐标交错排列，`core/chunker/` 在block接缝处把不相邻的文字粘成了原文根本不存在的"词"**，LLM 再对着这个不存在的词报错别字/漏字。实测预览版第11页被判两栏时，阅读顺序里出现"系的搭建方法。课规划、实施与应用"、"进行需PLM系统规划与实施"这类跨栏粘接的病句；判对四栏后它们是"字化的需求、咨询规划、实施与应用"、"给出PLM系统规划与实施的关键"，本来就是连贯的。

**A类的算法在 `_columns.py`，设计理由全部写在该文件顶部 docstring**（占用率剖面为什么优于二值覆盖图、为什么必须分层、为什么 CV 只排序不否决、为什么宁可判少不凭空补线），改那块逻辑前先读它。阈值是 `config.py` 里的 `COLUMN_A_*` 一组，每个都标了两侧实测余量。这里只记跟本模块其余部分的接口关系：

- `_order_native_page` 拿到的是"从左到右的全部分栏线"（0/1/3 条 = 单栏/两栏/四栏），用 `bisect` 按分栏线定位块属于第几栏，跨栏块按 y 位置与各栏交替合并。**`mode` 仍只有 `'single'`/`'double'` 两个取值**，`'double'` 表示"分栏"而不特指两栏，栏数体现在每个块的 `column` 字段（两栏是 `'left'`/`'right'`，三栏以上是 `'col1'`..`'colN'`，`_source_location` 相应输出"第N页第k栏"）——给 `mode` 加取值会让 `_majority_layout_mode` 把"两栏页+四栏页混排"的正常刊物判成 `'mixed'`，连带改变全文档的位置描述表述。
- **`COLUMN_A_*` 与不带 `A` 的 `COLUMN_*` 是两套，别混用**：后者服务 B 类（`_common.py::_detect_column_split`），其中 `COLUMN_SPANNING_BLOCK_WIDTH_RATIO`(0.7) 尤其容易被顺手改——A 类要的是 0.6，所以另立了 `COLUMN_A_SPANNING_WIDTH_RATIO`，改共用那个会静默改变 B 类行为。

**B类为什么不一起换成新算法**：B 类的阅读顺序由 paddlex 的 `sort_by_xycut` 独立算出，**不看分栏检测结果**；`_detect_column_split` 的返回值只用来决定 `mode` 和给块打 `column` 标签，而 `column` 的唯一消费者是 `_source_location`——只影响"左栏/右栏"这类位置描述措辞，不影响送审内容，也就不解决任何假错误。而 B 类一页只有 3~23 个块（A 类 90~110），占用率剖面在这种稀疏度下实测会让 `sample_single_column` 有页面从 1 栏变 2 栏（核过坐标是版权页的真空隙、未必是误判，但"连对错都要额外目视核"本身就说明不划算）。收益纯属措辞精度、风险要逐页核，所以维持现状。**改这里时 `git diff` 应显示 `ocr_pdf.py` 零改动**；`test_ocr_single/double_column` 是这条边界的守门用例。

**当前效果与已知残留**（134 页目视核定 GT：旧算法对 88 → 现在 129，四栏收益 36 页，零回归；另 221 页单栏文档零误判）：**文章开篇页仍会漏检四栏**——图多字少、跨栏大标题正好压在栏1|栏2 缝上时（79期p14、82期p8/p9/p25/p26），整页降级为两栏。这是有意接受的取舍，判少了＝维持交错、不产生新的串文，判多了（如整页表格被拆栏）才会把正文切碎，所以算法在缺一条真缝时一律降级到栏数更少的假设、**不做"按栏距外推补齐缺失边界"**。所以"四栏页数 5→26"这个表面数字里，真实召回率是低于它的。

**另一类残留（与栏数无关）**：这类期刊里 PyMuPDF 的一个 block 就是一个视觉行，`core/chunker/` 在block之间要插 `\n`（原因见 [core/chunker/CLAUDE.md](../chunker/CLAUDE.md)），所以LLM看到的仍是"是最根本的成\n功要素。"这种词中断行。比起粘出错词已经轻得多（换行是纯文本里极常见的形态），暂未处理；真要治，需要在解析阶段把同栏连续续写行合并成段落block，那会改变 `block_index` 颗粒度，影响面另说。

**排查与回归**：

- 新文档分栏出问题，**第一步跑 `tools/probe_columns.py` 看占用率分布，不要直接调阈值**——阈值标定自同一家排版的 3 份期刊，`COLUMN_A_GAP_MAX_OCCUPANCY_RATIO` 与 `COLUMN_A_SPANNING_WIDTH_RATIO` 的上界还是由同一页（唯一的整页大表格样本）决定的，过拟合风险最高就是这两个。`--render` 能把分栏线叠加到页面图上目视核对，`--levels` 打印分层结构每层的中间量。
- 改完用 `tools/compare_columns.py --all` 过验收线（回归 0 / 四栏收益 ≥5），它把两套边界分别喂给真正的 `_order_native_page`，比的是**块接缝处的相邻字对**而不只是栏数——那才是病灶的最小可观测单位。全部实测数据与两侧余量在 `tools/column_probe_data.md`。
- 单元测试见 `tests/test_parser.py`"A类分栏检测"一节 12 条纯几何用例，都做过反向验证。其中三条是防"顺手改回去"的：`test_columns_overflow_lines_do_not_close_gap`（占用率剖面取代二值覆盖图的核心机制）、`test_columns_spanning_title_uses_half_region_scale`（通栏门限必须按半区内容范围宽算，不是几何区域宽）、`test_content_x_range_uses_all_blocks`（内容范围**不**剔通栏块，与直觉相反）。**构造几何覆盖不了的那条防线要知道**：真实那页大表格靠 τ=0.06 挡住（0.08 就被拆栏），但它的占用率分布无法用构造数据复刻，只由 `compare_columns.py` 的真实文档对比覆盖。

## 双栏页显示期刊自身页码，不是PDF物理页码

期刊/活动手册这类双栏文档通常自带印刷页码，跟PDF物理页码经常对不上（比如封面/目录不计页码，PDF第5页对应期刊第3页）——编辑核对问题要翻回纸质刊物，PDF页码没有意义。`ParsedBlock.doc_page` 记录该页从页眉/页脚提取到的期刊页码文本（如`"12"`），`_common.py::_source_location` 在 `mode=="double"` 时优先用它拼成`"文档第X页"`，提取不到（比如目录、封面这类本身没有页码的页）才退回`"PDF第N页"`——用`"PDF"`前缀跟正常提取到的期刊页码区分开，不让编辑误以为PDF页码就是期刊页码。单栏文档不受影响，沿用`"第N页"`：普通Word转的报告类文档PDF页码本来就等于文档页码，不存在需要提取的问题。gating 只看这一页是不是双栏，跟走A类还是B类通道无关，两条通道各自独立提取：

- **A类**（`native_pdf.py::_strip_headers_footers`）：页眉/页脚区域里命中 `_NUMERIC_ZONE_RE`（纯数字/罗马数字/页码范围形状）的文本，本来就要被剔除、不当正文送审，顺手记下来作为该页的 `doc_page` 候选——不是新增判断，只是把已有判断的副作用利用起来。一页内多个候选取第一个。
- **B类**（`ocr_pdf.py::_run_structure`）：版面检测模型自己会把页码区域打上 `"number"` 标签（跟页眉/页脚同一批 `_DROP_LABELS`），不需要像A类那样靠正则猜形状——直接读该标签区域的OCR文本。同样是本来就要丢弃的区域，丢弃前顺手记下文字。

两条通道都不做"是否真的像页码"的额外校验：A类靠 `_NUMERIC_ZONE_RE` 的形状要求，B类靠版面模型自己的语义标签，信号已经足够强，没有必要再叠一层验证。

测试见 `tests/test_parser.py`"双栏页期刊页码提取"一节（`_strip_headers_footers` 提取/不提取两种情况、`_run_structure` 有无`"number"`标签两种情况）和"`_source_location`"一节（双栏有/无doc_page、单栏不受影响三条用例）。全链路透传（`RawIssue.doc_page` → `ClassifiedIssue.doc_page` → `issues.doc_page` 列 → Excel"文档页码"列）见 `core/exporter.py` 模块docstring。

## 跨页对开版面：PyMuPDF把左右两页同一水平线上的文字聚成一个block

真实文档诊断（`预览版 8-9月 电子版-2026e-works活动计划` 第15页，一个物理页印着"30 29"两个
页码的对开版面，左右两页各两栏共四栏）发现：PyMuPDF 自己的 block 聚类会把**左页和右页
同一水平线上毫不相干的两块文字**聚成同一个 block——纵向恰好重叠、横向相距约700pt。实测
这一页有两个这样的块（页宽仅1009）：

| 文本 | bbox宽度 | 实际是什么 |
|---|---|---|
| `企业内训 企业内训` | 898.2 | 左页页眉 + 右页页眉 |
| `联系⽅式 造运营系统建设思路；…` | 777.3 | 右页最右的联系方式框标题 + 左页栏1正文 |

**注意这跟阅读顺序无关**：这一页的跨页续写顺序本来就是对的（上一页末尾"…从底层逻辑上
探索⽣产制"后面确实接着本页栏1的"造运营系统建设思路"），唯一的污染是"联系方式"四个字被
塞进了那一行。诊断时不要被"看起来像跨页顺序错了"带偏。

这类误聚块在两个彼此独立的地方造成问题，各有各的防线，**不能互相替代**：

**一、拼接符选错，把两页文字粘成一句串文。** 这两行y轴重叠100%，会被判成"同一视觉行被
PyMuPDF误拆"（该判定本是为项目符号场景加的，见下一节），用**空格**拼成一句串文喂给
LLM，LLM据此报出"应改为『探索生产制造运营系统建设思路』（删除『联系方』）"这类并不存在的
错误。防线是一道横向闸门 `config.NATIVE_SAME_ROW_MAX_GAP_HEIGHT_RATIO`(3.0)——两个line
的水平间隙不得超过较小那个line自身高度的3倍（行高≈字号≈一个汉字宽度，这个倍数约等于
"最多隔几个字"）。真实数据支撑：项目符号误拆场景间隙只有**1.2倍**行高，本场景是**61倍**，
阈值取3两边都留出充分余量。用行高当尺子而不是绝对pt值，对不同字号/渲染尺度自适应。

**二、777pt 的 bbox 污染分栏判定。** 这两个超宽块的**中心点**（504和433）都落进左半区，
若拿半区里全部块算内容范围，范围会被撑成 `[44.5, 953.4]`（横跨整页），子栏检测必然失败、
四栏退回两栏。防线在 `_columns.py`：**每一层都是先按该层内容范围宽度剔掉通栏块、再用剩余
块的范围算剖面**，这种块在任何一层都够宽、必被剔除。实测这一页现在检出三条分栏线
`[253.4, 506.7, 758.8]`（旧算法只有 `[507.1]`）。

**上面第一条修好并不能顺带修第二条**：`_lines_share_same_row` 只决定 block 内多行的**拼接符**
是空格还是换行，**不改变 block 的划分和 bbox**，那个777pt宽的 bbox 照样会污染半区范围。

**这一页的四栏检测现在成立了**（右半页不对称——栏3是正文、栏4是只有4行短文本的联系方式框
——旧算法的两侧字符数平衡校验必然挡掉它，新算法完全不看字符数，见"分栏检测"一节）。仍未做
的两件相关事：**A类通道没有跨页拆分**（`_maybe_split_spread` 只用于B类OCR通道），概念上把
对开页拆成两个逻辑页最干净（`doc_page` 提取到 `"30 29"` 两个页码就是现成信号），但它消不掉
PyMuPDF 的跨页聚类（聚类发生在提取阶段，早于拆页）；**装饰性信息框（联系方式/二维码等）会
被当正文栏送审**，这是独立于分栏检测的另一个问题。

测试见 `tests/test_parser.py::test_native_pdf_spread_far_apart_lines_join_with_newline`
（坐标取自真实文档，验证y轴100%重叠但横向隔61倍行高时用换行拼接）和
`::test_columns_spread_merged_block_does_not_break_detection`（加一个中心点落在左半区的
误聚块，四栏检测仍成立）。两条都做过反向验证（退回修复前确认测试真会红）：拼接结果
`联系方式 造运营…` → `联系方式\n造运营…`，分栏线数 `3` → `1`。

## PyMuPDF把同一视觉行误拆成两个line时，拼接要用空格而不是换行符

真实文档诊断（`武昌首义学院...用户使用手册`系列PDF）发现一类新的解析伪影：项目符号用符号字体（如Wingdings，字符落在Unicode私有使用区，如``）跟正文之间隔一段水平缩进，PyMuPDF的行聚类算法因为字体切换+水平间隙，把明明在同一条水平线上的"符号+正文"拆成了两个独立的`line`对象。`native_pdf.py::_extract_native_page_raw`原本对block内每个line无条件用`\n`拼接（见下一节"block内多行拼接必须用换行符"），这里就会把本来同一行的"➢ 系统管理员享有学生的功能…"拆成"➢\n系统管理员享有学生的功能…"两行喂给LLM，LLM看到孤立的符号+换行，分不清这是"两个独立格式单元的边界"还是普通换行，于是误判成"项目符号应换行置于下一段首"这类不存在的格式问题——这份文档里这类block有上百个，会产生大量重复的低价值issue，且被错误归类成"错别字与拼写"（进最高置信度的确定性错误层）。

**诊断方法**：直接用PyMuPDF `get_text("dict")`读被误拆的两个line的bbox，发现y轴（上下边界）几乎完全重叠（真实数据：一个是[698.5, 710.1]，另一个是[698.9, 709.4]，后者完全落在前者区间内），而真正纵向上不同的两行y轴不会有这种重叠——这是可靠的判别信号，不需要专门识别"这是不是项目符号"，对任何被PyMuPDF误拆的同视觉行内容都通用。

**修复**：`native_pdf.py::_lines_share_same_row(bbox_a, bbox_b)`算重叠长度占较小line自身高度的比例，达到`config.NATIVE_SAME_ROW_OVERLAP_MIN_RATIO`（0.5，真实数据是100% vs 0%，阈值留了充分余量）判定为同一视觉行，拼接时用空格；否则维持原来的`\n`拼接。只改了`_extract_native_page_raw`内部的拼接逻辑，不新增`ParsedBlock`、不改变`block_index`颗粒度，下游全部透明。

**范围**：只修了A类原生PDF通道（PyMuPDF自己的line聚类）。B类OCR通道（`ocr_pdf.py::_run_structure`）的"行"来自PaddleOCR文本检测框按坐标分配进版面区域，是另一套机制，理论上可能有类似的"同一视觉行被检测成多个文本框"问题，但没有真实数据验证过是否存在，没有一并处理——等真遇到再解决，不臆测。

测试见 `tests/test_parser.py::test_native_pdf_same_row_misplit_lines_join_with_space`（构造y轴高度重叠的两个line，验证用空格拼接）和 `::test_native_pdf_barely_overlapping_lines_still_join_with_newline`（构造y轴轻微擦边重叠但远低于阈值的两个line，验证仍用换行符拼接，不会误伤真正的换行）。

## block内多行拼接必须用换行符 `\n`

**两处原实现都会把本该独立的行糊成一句读不通的病句甚至无分隔乱码，进而让LLM把"解析伪影"误判成"错别字/语法问题"来报**：

1. **A类原生PDF**（`native_pdf.py::_extract_native_page_raw`）：原来是 `" ".join(lines_text)`，同一个PyMuPDF检测出的block内如果包含标题+正文、列表项等多个独立行，会被空格拼成一句连续的话（例如"目前的局限性："后紧跟下一行内容，冒号后面直接接下一句，读起来像病句）。
2. **B类OCR**（`ocr_pdf.py::_run_structure`）：原来是 `"".join(l["text"] for l in lines)`，**完全没有分隔符**，比A类问题更严重——版权页/名单类内容（多个字段各占一行，如"主编：XXX""执行主编：XXX"）会被直接粘成一整段无法辨读的乱码。

修复：两处都改成 `"\n".join(...)`，只在同一个 `ParsedBlock` 的 `text` 字段内部保留换行标记，**不新增 `ParsedBlock`、不改变 `block_index` 颗粒度**——曾考虑"每次换行就拆成新block"的方案，但放弃了：那样会把因页宽自动换行、本属于同一段话的正常长段落也拆碎成多个人工block，而分块（`core/chunker/`）、跨块去重（`core/classifier/`）、追问上下文窗口（`core/workflow/persist.py`）全部依赖 `block_index` 当前的颗粒度，改动面会大很多。回归测试见 `tests/test_parser.py::test_native_pdf_multiline_block_join_uses_newline`（伪造 `get_text("dict")` 返回值隔离测试拼接逻辑）和 `::test_ocr_region_multiline_join_uses_newline`（monkeypatch掉版面检测/OCR两个模型，避免真实推理）。

## 表格结构识别方案：不重建行列结构，`table`类区域整体不送审

`ocr_pdf.py::_run_structure` 对版面检测标出的 `table` 类型区域，**不调用任何表格结构识别模型**，只按坐标把区域内文字拉平成 flat-join 文本存进 `ParsedBlock.text`；真正"不送去校对"的过滤发生在 `core/chunker/`（`_build_fill_units` 整体跳过 `block_type=="table"` 的block，不生成送审内容），不在本模块——`ParsedBlock.text` 本身仍保留这份拉平文本，供未来展示/导出等场景使用。

**为什么不重建表格行列结构**：曾尝试过独立调用 `paddleocr.TableRecognitionPipelineV2` 重建行列结构，两条证据表明这条路线不成立：
1. 命中率接近零——单图测试能成功，但集成进整页渲染裁剪出的子图后同一视觉内容反而识别失败，且随DPI变化不单调，多次真实文档验证从未观察到真正生效。
2. 更根本的问题是即使识别成功也解决不了病灶：真实校对诊断发现，"确定性错误"层里77%的问题定位在 `block_type=="table"` 的区域，而这些区域绝大多数根本不是需要校对的正文，是文档里插入的**说明性UI截图**（如菜单结构对比图）——截图里的内容压根不该被当作作者撰写的文字去校对，无论表格结构识别得多准都治标不治本。

**这不是说table类区域里不可能有真实文档正文**（比如真正的数据表格、财务报表等）——如果以后真的遇到需要校对的表格化正文，"整体跳过"的规则会连带漏掉它，这是有意识接受的取舍（宁可漏判不复杂化）。真要支持，需要先能区分"表格化的正文"和"截图/示意图"两种情况，目前没有可靠信号能做这个区分（LayoutDetection只输出"table"这一个标签，不区分语义），留给以后真的遇到这种文档时再解决。测试见 `tests/test_parser.py::test_table_region_keeps_flat_joined_text`（确认table区域产出flat-join文本，不触发结构识别）和 `tests/test_chunker.py`（table类block不进入任何chunk的回归用例）。

## 字形变体字符在源头归一化（`_cjk_variants.py`）

真实文档诊断（`预览版 8-9月 电子版-2026e-works活动计划`）发现：这份PDF的字体ToUnicode
CMap有缺陷，PyMuPDF按坐标提取出的正文汉字有2122处（19页刊物里）落在Unicode"康熙部首"
（U+2F00~2FD5）/"CJK部首补充"（U+2E80~2EF3）等区块的码位上——肉眼和标准汉字毫无区别，
但码位不同，LLM看到会产生各种困惑（把变体字符本身当错别字报出来只是最直接的一种，
更隐蔽的是把原文本来重复出现的短语误判成"排版错乱要去重"、或对着满屏陌生码位的段落
瞎猜续写）。在改这里之前，`core/classifier/postprocess.py` 已经有一套**反应式**防线
（判断"LLM给出的改写建议和原文相比是不是只差这类变体"，命中就丢弃/降级/剔除reason里
的无实质片段），但那套逻辑只能拦住"表现成具体某种措辞"的困惑，每冒出一种新的表现
形式（如上面"误判成排版错乱"）就要再堵一个洞，属于治标不治本。

**改为在解析阶段（A类原生PDF通道）直接归一化，LLM和后续所有环节压根不会再看到这些
变体字符**：`native_pdf.py::_extract_native_page_raw` 拼出block文本后立即调用
`_cjk_variants.py::normalize_cjk_variants`。真实文档回归验证：2122次出现，2021次
（95.2%）被直接消除，剩余101次是故意不处理的"CJK部首补充"区块里没有安全归一化目标
的纯偏旁部首字符（如"人字旁⺅"，从未独立成字），交给分类器那层的反应式防线继续兜底。

**只做"目标字符可确定"的安全归一化，不做整个"CJK部首补充"区块的归一化**：康熙部首/
CJK兼容汉字两个区块有Unicode官方NFKC分解数据，程序化算出映射表（比人工誊写更不容易
出错，`_build_nfkc_variant_map` 直接查 `unicodedata`）；但CJK部首补充区块完全没有
NFKC分解（这个区块定义上是"字典部首索引用的纯部首形式"，不是"规范汉字的兼容变体"），
没有官方数据能程序化推导目标字符，贸然指定一个目标字符替换属于臆测，有把内容改错的
风险——这正是 `postprocess.py` 里"部首→汉字"映射表被弃用的同一个教训，不重蹈覆辙。
`_RADICAL_SUPPLEMENT_FOLD` 因此只收录了真实文档验证过的14个字符，且每一个都是靠
Unicode官方字符名明确写出"C-SIMPLIFIED/J-SIMPLIFIED <某个独立汉字>"（如`⻔`=
`CJK RADICAL C-SIMPLIFIED GATE`→`门`）才收录，不是凭字形目测——这个边界依据是权威
数据，不是经验判断，未来遇到新字符可以用同样的方法（查 `unicodedata.name()`）安全
扩表，不像早期那张视觉目测的表"用得越久越可能出错"。

**这套归一化表和 `core/classifier/postprocess.py::_KANGXI_TRADITIONAL_FOLDINGS` 记录
的是同一份Unicode事实（康熙部首NFKC分解结果是繁体字形，需折回简体），但两处独立维护，
不是疏忽**：本模块是"源头主动归一化"，分类器那套是"反应式兜底"，覆盖本模块之外的
场景（本模块没收录的CJK部首补充字符、理论上未来其他解析通道产生的类似伪影）——
职责不同，不应该为了共享24行数据让两个关注点不同的模块产生强耦合。

只影响A类原生PDF通道：B类OCR通道的文字来自PaddleOCR识别模型输出，不经过PDF字体
ToUnicode映射，不存在这类伪影；C类Word通道同理。

测试见 `tests/test_parser.py`"CJK变体字符源头归一化"一节：康熙部首直接NFKC、康熙部首
繁体折回、CJK部首补充已知安全条目、CJK部首补充未收录字符原样保留、普通文本不受影响、
`_extract_native_page_raw` 实际调用链路五/六条用例。

## 尝试：OCR文字识别模型换档（真实A/B测试后否决，未采用）

`系统管理员.pdf` 诊断出的另一类假错误——"字符本身被识别错"（如"个人信息"被识别成"学入记息"，和表格结构无关，是识别阶段就已经认错了）——曾设想两个缓解手段：**换更大档的文字识别模型**、**跳过整页渲染直取PDF内原生嵌入图片**。前者已实现机制并做过真实A/B测试，结论是否决；后者在实现前用真实文档核对假设就被推翻，未实现。两条都如实记录在这里，避免以后重新踩同样的坑。

**换模型（已实现机制，默认关闭）**：`_get_ocr_pipeline()` 支持 `config.PADDLEOCR_DET_MODEL`/`PADDLEOCR_REC_MODEL` 覆盖默认识别模型，非 `None` 时透传给 `PaddleOCR(text_detection_model_name=..., text_recognition_model_name=...)`。动机：查明 paddleocr 3.7.0 对 `lang="ch"` 不显式指定模型名时默认加载 **PP-OCRv6_medium_det/rec**（v6目前只有tiny/small/medium三档，无server档）；本机 `~/.paddlex/official_models` 已缓存上一代最大档 **PP-OCRv5_server_det/rec**（零下载成本），设想"档位更大=更准"。

**真实A/B测试（`系统管理员.pdf` 页1，同一页分别用两种模型重新解析，直接对比同一字段的识别结果）推翻了这个设想**：该页有个4栏并排菜单对比表（无边框，按坐标拉平），"个人信息""我的收藏夹"等词各重复出现4次，天然是同图同字的多份样本，适合直接对比：

| 字段（4次重复） | PP-OCRv6_medium（库默认） | PP-OCRv5_server |
|---|---|---|
| 个人信息 | 3对1错（"学入记息"） | 3对1错（"学人记息"，错法不同但同样错） |
| 我的收藏夹 | 2对2错 | **1对3错**（多错出"我的收照尖"） |

"个人信息"字段两者战平；"我的收藏夹"字段 v5_server 明显更差。**没有任何证据支持"档位更大更准"这个直觉**，反而有具体证据显示它在这份真实文档上更差。`config.py` 里两个常量因此保留为 `None`（沿用库默认），机制留着但不启用——如果以后想重新评估，样本量至少要覆盖更多页/更多字段，不能只看一页的个别字段就下结论（这次的两页对比本身样本也不算大，只是"没证据支持切换"这个否定结论相对稳固，因为两组数据方向一致——没有一个字段是v5_server明显更好的）。

**原生嵌入图片直取（未实现，前提假设被推翻）**：设想是"每页就是一张贴进去的截图，直接提取原生嵌入图比渲染整页再裁剪画质更高"。用真实文档核对这个前提时发现是错的：`系统管理员.pdf` 每页并不是单张大图，而是**由8~11张小尺寸嵌入图（图标/头像/局部截图碎片）+ 上百个矢量绘制元素（边框、背景色块）组合渲染出的页面**（实测页1：9张嵌入图，最大单张只覆盖页面面积27.8%，全部嵌入图加起来也只覆盖40.4%；同时有445个矢量绘图元素）——这类文档本质是设计工具/浏览器"打印为PDF"导出的UI快照，不是扫描照片。渲染整页（把矢量+位图合成到一张图）是唯一能得到完整页面内容的办法，没有"更高画质的原生图"可以抄近道——`_render_page_image` 现有的整页渲染逻辑对这类文档是必要的，不是可优化项，未做任何改动。
