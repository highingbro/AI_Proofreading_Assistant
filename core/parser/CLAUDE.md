# core/parser/ 关键点

统一解析三类输入，PDF 按有无文字层分两条路径：

- **A类：有文字层PDF**（`native_pdf.py`）—— PyMuPDF 按坐标提取文本块，手写双栏分栏检测 + 阅读顺序还原。分栏判定逻辑见 `config.py` 中 `COLUMN_GAP_BAND` / `COLUMN_MIN_GAP_WIDTH_RATIO` / `COLUMN_BALANCE_MIN_RATIO` 三个常量的注释，改之前先看那段注释解释的误判场景。
- **B类：无文字层扫描PDF**（`ocr_pdf.py`）—— 渲染为图片后用 PaddleOCR（版面检测 + 文本识别独立模型组合）+ paddlex 的 XY-Cut 算法还原阅读顺序。
- **C类：Word**（`docx_parser.py`）—— python-docx 按段落顺序读取。先 try `docx.Document(path)`，撞上 `KeyError` 才退化到 `_repair_dangling_relationships` 修复后重试，详见下方"docx断链关系记录"一节。

`_common.py` 存放 A/B 两条通道共用的坐标/分栏判定工具（`_find_extent_gap`/`_detect_column_split`/`_source_location`）；`_types.py` 存放公共数据结构（`ParsedBlock`/`ParsedDocument`）与异常，避免子模块互相反向依赖 `__init__.py` 造成循环导入；`__init__.py` 是唯一对外入口 `parse_document`，内部做 A/B 两类逐页结果的合并编排（`_parse_pdf`/`_majority_layout_mode`）。

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
