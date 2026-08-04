# ui/ 关键点

Streamlit 展示层。`app.py` 只剩入口脚本本身（`set_page_config` → 品牌/样式注入 →
`init_db` → 署名 → 任务闸门 → 侧边栏 → 路由），所有渲染逻辑在这个包里。

- `theme.py` —— 四层语义色板 + 两个页面级一次性注入函数（品牌标识、卡片色条CSS）。
- `cards.py` —— 问题卡与统计指标，标准校对/历史记录/原稿比对三处共用同一份。
- `actions.py` —— 非渲染的共用副作用：上传文件落盘管理、导出后触发规则重算。
- `tasks.py` —— 署名选择器 + 任务选择/创建那一屏。
- `page_standard.py` / `page_compare.py` / `page_history.py` / `page_feedback.py` ——
  四个功能页，彼此不互相引用。

依赖严格单向：`theme ← cards ← page_*`，`actions ← page_standard/page_compare`。

**本包内任何模块都不许在 import 时执行 `st.*`**，只定义函数和常量——`st.set_page_config`
必须是整个应用的第一个 Streamlit 调用，它在 `app.py` 里。各模块自建
`logger = logging.getLogger(__name__)`，`logging.basicConfig` 在 `app.py` 里配置 root
logger，子 logger 一并落到 `data/app.log`。

## 布局与色彩设计要点

全局主题（`.streamlit/config.toml`）把品牌色从Streamlit默认红换成冷色调墨蓝，让红色
在界面里只保留"确定性错误"这一种语义。侧边栏顶部放品牌标识（`theme.inject_sidebar_brand`），
取代之前主区那个和页面标题重复的巨型 `st.title`；侧边栏顺序为
品牌→当前署名→当前任务→功能入口（署名在选任务之前就要能填，创建任务时要记 created_by）。

四层分类（`config.LAYER_*`）配了一套语义色 `theme.LAYER_COLOR`，贯穿指标区数字、问题卡片
的左侧色条与标题。指标区六个数字用一整行HTML flex渲染（不用 `st.metric` 混排），保证
各列标签/数字基线对齐；总数上方先给一句自然语言总结再列数字。

问题卡片**不给整卡上底色**（明确要求"背景不上色"），层级/状态只体现在卡片左侧那条4px
色条上：`theme.inject_card_styles()` 在页面顶层注入一次CSS，靠 `st.container(border=True,
key=...)`（Streamlit 1.32+起容器带 `st-key-<key>` 稳定class）按属性选择器给色条上色。
`cards._render_issue_card` 的 `card_key` 待处理态用层级slug、已采纳/已拒绝态用固定前缀
（`accepted`/`rejected`），因此"层级色条"和"终态色条（绿/红）"只需要7条CSS规则；
`card_key` 随 `status_info["status"]` 变化，rerun 后色条随之切换，不需要额外状态管理。
原稿比对产出的issue的 `layer` 是另一套取值（`DIFF_LAYER_*`），用 `theme.OTHER_LAYER_SLUG`
中性灰兜底避免 KeyError。

原文/建议**分两块干净展示，不做字符级diff**：`suggestion` 是自由文本说明（"应改为…"/
"存疑，建议人工核实…"/"原文照录…"），不是和 `original_text` 平行的"改后文本"，硬做
diff只会得到乱码。因此各带一个小灰标签、正文都用黑字，靠标签而不是颜色区分原文与建议；
动态内容一律 `html.escape` 后再拼进HTML。

"归层依据"expander 只在 `LAYER_DOUBTFUL`/`LAYER_QUOTATION`（`theme.LAYERS_WITH_NOTES`）
两层展示——这两层的判断依赖置信度/引用识别，编辑需要看依据才能决定要不要采纳；
`LAYER_CONFIRMED`/`LAYER_OPTIONAL` 判断相对直接，不展示以免信息过载。

采纳/拒绝之后卡片标题追加一个绿/红状态标签，按钮从"采纳/拒绝"换成单个"撤销"，点击
调用同一个 `workflow.set_issue_status` 把状态改回"待处理"，不新增函数；撤销"已拒绝"
不会撤销已经写进 `feedback` 表的反馈学习记录，两者是独立的历史留痕。

问题卡片列表按 `cards.render_issue_cards` 两条一行摆放（`st.columns(2)`），和任务选择页
的任务卡是同一个网格模式；标准校对结果、历史记录详情页、原稿比对结果三处都调这一个
函数，不各写一套摆放逻辑。只有**建议**内容预留固定两行高度（`min-height`+`line-height`，
短文本也占满这份高度）——原文是被标记的原句，长度本来就稳定不需要预留；建议是自由
文本说明，长短差异大，才是同一行两张卡参差不齐的来源。**这个两行预留是硬要求，压
卡片高度不能从这里扣**，建议文字字号略调小只是减少超出两行的概率。压高度改从别处
拿，且不做省略号截断（建议文字是校对结论，截断会丢内容）：`theme.inject_card_styles` 把
卡片 padding 从 Streamlit 默认的 1rem 收紧到 0.5rem/0.8rem，容器用 `gap="xxsmall"`
收紧内部元素间距，批注输入框挤进采纳/拒绝（或撤销）按钮那一行的剩余空间、不再单独占
一整行（`label_visibility="collapsed"` 省掉的标签文字用 placeholder 补上）。

页面不使用emoji：优先级用文字表达，层级差异靠色条+标题色表达。

## 任务闸门与署名要点

**流程是线性的：先选任务，再选功能。** `st.session_state["task_id"]` 没有指向一个
仍然存在的任务时（`get_task` 返回 None 即视为没有），`app.py` 只渲染
`tasks.render_task_selection()` 那一屏并 `st.stop()`——侧边栏的功能入口 radio 根本不会
被创建，四个页面一个都进不去。因此除历史数据外，库里不可能再出现 `task_id` 为 NULL 的
record，`db/models.py::create_record` 把 `task_id` 设成必传参数即可保证这条约束（表结构
上它仍可空，原因见 `db/database.py` 建表语句上方注释）。

这个空状态实际上只在全新空库首次运行时出现一次：迁移会把既有记录归入"历史归档"
任务（`config.LEGACY_TASK_NAME`），`task_id` 又常驻 session_state，所以正常使用中
一进来就已经在某个任务里。

任务状态（`config.TASK_STATUSES`）就地在任务列表每行的 selectbox 上改，不另开管理
页；列表默认只列"激活"的，勾选"显示已解决的任务"才会看到已解决的。状态不自动推进
（"导出即完成"这类推断在一个任务多轮校对的场景下一定会误判）。

**"已关闭"在前端任何地方都不展示**：selectbox 里仍然可以把任务选成"已关闭"（这是
唯一能写入这个状态的入口），但一旦写入，该任务下一次 rerun 起就从任务选择页、
历史记录页"改归属任务"下拉里彻底消失——不受"显示已解决"复选框影响，UI上没有任何
开关能让它重新出现。想再看到或改回激活/已解决，只能直接改数据库；这是一个刻意的
读写不对称设计，"已关闭"因此更接近"归档"而不是"设置一个可逆状态"。

任务一多时列表本身在 `st.container(height=420)` 里滚动，不撑开整个页面（新建任务
表单固定在列表下方，不会被顶出屏幕）；卡片按 `st.columns(2)` 两个一行摆放（各占半宽，
`tasks._render_task_card` 渲染单张），同样的高度能容纳的任务数因此翻倍，需要滚动的场景更少。

**任务名不做唯一性校验**：`task_id` 才是真正的标识，重名不影响任何功能，卡片上的
创建日期/记录数已经够分辨。但输入的名字和现有任务撞了大概率是手滑，"新建任务"表单
在填完名字那一刻就现查现比对弹一条黄色提示；比对范围是"前端看得到的任务"（排除
已关闭的——那些本来就不展示，撞上了用户也看不出哪来的重复）。

撞名时点"创建并进入"**不会一次点击就创建**：第一次点击只把当前名字记进
`st.session_state["task_dup_ack_name"]`，不建任务——这是因为"算出警告"和"创建成功
后rerun跳进新任务"如果落在同一次脚本执行里，警告刚画出来页面就已经跳走，用户
几乎看不到（真实反馈过的问题）。名字不变时再点一次才真正创建；名字改了（不再撞名，
或撞了另一个名字）这个标记自动失效，按新状态重新走一遍。不撞名的正常情况仍是一次
点击直接创建+跳转，不受影响。

侧边栏"切换任务"按钮走 `tasks.switch_task()`：清空 `st.session_state` 里除
`tasks._KEEP_ON_TASK_SWITCH`（署名及其widget状态）以外的所有key。换任务=换工作上下文，
上一个任务的校对结果/待保存结果不该残留；但署名是"我是谁"，跟在哪个任务里无关，
不该因为换任务就要求重选一次。

**署名不是账号**：全部数据都在同一个 `config.DB_PATH` 里，任何人都看得到全部任务与
记录，署名只回答"这条记录是谁做的"，写进 `records.author`。同一轮校对必然由同一个人
跑完并审完，所以 issues 表不另存一份处置人。候选来源是库里出现过的署名
（`get_authors()`，records.author ∪ tasks.created_by），用下拉而非自由文本框——
多人共用时手输必然出现"张三"和"张 三"这种同人不同名。没填署名不阻断校对，只在侧边栏
给一句提示，`author` 落库为 NULL。

## 标准校对页要点

Streamlit 每次交互都会重跑整个脚本，全部靠 st.session_state（file_id/
classified_result/record_id/issue_ids/issue_status）管住生命周期：换文件
（按文件内容md5哈希判定）才清缓存重新校对；采纳/拒绝按钮触发的 rerun 只读
缓存、不重跑 workflow.run_standard_proofread。没有用 st.cache_*——校对流程
有副作用（写库、耗真实API额度），语义上不适合按输入哈希缓存的机制。

上传的文件先落盘到 config.UPLOADS_DIR（时间戳前缀避免覆盖）再交给
parse_document（该函数吃路径不吃文件对象）。落盘后即用即弃，不进 records 表、
后续也不会再被读取，每次写入后 `actions.prune_uploads_dir` 只保留最近
config.UPLOADS_RETENTION_COUNT 个文件，避免上传目录无限堆积。问题卡按钮 key 用
f"accept_{issue_id}"/f"reject_{issue_id}"，全局唯一。异常兜底：
UnsupportedFormatError/NoTextLayerError/LLMCallError/LLMResponseError 分别
给可读提示，兜底 except Exception 防止未预期异常崩页面；出错时不写库、不
缓存结果，允许重试。

st.file_uploader 在页面切走再切回来后，浏览器出于安全限制无法恢复之前选中
的文件，uploaded_file 会变回 None——这不代表用户想清空已校对结果，所以只
有在 uploaded_file 真的拿到新文件时才判断是否换文件/清缓存；uploaded_file
为 None 时直接往下走，看有没有已缓存的结果可以展示。

换文件判断按内容md5哈希（`actions.file_id`），"开始校对"按钮只在 classified_result
为 None 时渲染，所以同一份文件（哈希不变）校对完之后这个按钮不会再出现。
"开始校对"的解析→校对→落库逻辑因此抽成 `_execute_proofread(uploaded_file, mode)`，
供结果展示区顶部"重新校对本文件"按钮复用：uploaded_file 还在
（函数局部变量，没离开过页面）就直接原地重跑；已经因切页丢失（浏览器安全
限制，没有文件字节可用）时才退回 `_reset_session_state()`+提示重新上传。

## 追问区域要点

`cards._render_issue_card` 在采纳/拒绝按钮下方加了"追问"expander（历史用
st.chat_message 渲染，新问题用 text_input+按钮提交，answer_followup 抛出
的 LLMCallError 用 st.error 兜住）。追问历史全程从数据库现读，不进
session_state，和导出模块"数据一律从库读"是同一个原则。

## 导出Excel按钮要点

点击调 core.exporter.export_issues_to_excel(record_id)，异常
用 st.error 兜住；成功后 st.success 提示路径 + st.download_button 提供下载
（mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"）。

## 批注输入框（批注与状态解耦）要点

紧贴采纳/拒绝按钮的"批注（可选，采纳/拒绝/待处理都可以写）"，
用 st.text_input(..., on_change=_save_note) 实现——不需要额外的"保存"按钮，
失焦/回车即通过 on_change 回调写库，批注值本身就靠该 widget 自身 key 对应
的 st.session_state 在 rerun 间保持，不需要在 issue_status 缓存字典里额外
镜像一份。详见 core/workflow/CLAUDE.md"批注与状态解耦"一节。

## 历史记录详情页要点

核心设计：完全复用 `cards._render_issue_card`，不为历史记录页另写一套卡片UI。这个
函数一直按"属性访问"（issue.priority/issue.page_location/…）编写，参数在
实时校对流程里是内存中的 ClassifiedIssue；历史记录页的数据来源是
get_issues(record_id) 返回的字典（DB行），两者接口不兼容。没有为此重写
`_render_issue_card` 或改成字典访问（改了会牵动所有既有调用点和测试），而是
用 `cards.row_to_issue_view(row: dict)`，靠 types.SimpleNamespace 把DB字典行包
成同样支持属性访问的对象，实时流程和历史记录页因此共用同一份卡片渲染逻辑，
包括采纳/拒绝按钮、批注输入框、追问expander——全部原样可用，因为这些交互
本来就是直接写库的，不依赖是从哪个页面触发的。

layer_notes（归层依据）历史记录页展示不出来，是数据本身没有，不是遗漏：
issues 表没有 layer_notes 列（ClassifiedIssue.layer_notes 只在校对当次运行
时存在于内存里，persist_result 从未把它落库）。`row_to_issue_view` 对这个
字段填的是一句说明文字，不是留空更不是编造假数据。

两处状态同步的坑，处理方式：

1. `_render_issue_card` 用
   st.session_state["issue_status"].setdefault(issue_id, {"status": "待处理"})
   取显示用的状态——这个 setdefault 是给实时流程设计的（issue刚生成时确实
   是"待处理"）。如果历史记录页不做任何处理直接复用，会把DB里明明"已采纳"
   的历史issue，在还没被点击过的这个新会话里错误显示成"待处理"。修复：
   `page_history._render_history_detail` 在渲染问题卡之前，先用 get_issues 查到的
   DB当前值无条件覆写 st.session_state["issue_status"][issue_id]（不是 setdefault，
   就是直接赋值），每次进页面/切换记录/点完按钮触发 rerun 后都会重新执行
   这一步，保证展示的状态永远是DB真实值。
2. 批注输入框 st.text_input(key=f"note_{issue_id}", ...) 同理：Streamlit
   组件的初始值来自 st.session_state[key]，历史issue的批注只存在DB里，这
   个会话从没设置过对应的 note_{issue_id} key，不预填就会显示空白，看起来
   像"批注丢了"。修复：`_render_history_detail` 在第一次遇到某个 issue_id 时
   （if note_key not in st.session_state）用DB的 note 值预填——用条件预填
   而不是像状态那样无条件覆写，是为了不打断用户正在输入但还没触发
   on_change（失焦/回车）的编辑内容。

`cards.render_stats` 接受 (stats: dict, warnings: list) 两个原始值，不是 ClassifiedResult
对象——db.models.get_records() 返回的字典字段名本就和 ClassifiedResult.stats 一一
对应，直接传历史record字典即可；历史记录页 warnings 传空列表（records 表没存解析/
分块警告）。文档名/校对模式的副标题由调用方用 `cards.render_doc_subtitle` 在 st.header 下
单独渲染（那里才拿得到 doc_name），不进 `render_stats`。

`render_history(current_task)` 是四个页面里唯一带参数的：它要在 caption 里显示当前
任务名，而当前任务是 `app.py` 闸门算出来的模块级变量。其余跨函数状态全走
session_state，不需要参数传递。

导出Excel按钮在历史记录页复用 core.exporter.export_issues_to_excel，与标准
校对页实现一致；key= 加上 record_id 后缀
（history_export_{record_id}/history_download_{record_id}）避免和标准校对
页的同名按钮/未来可能的多记录场景冲突。

## 反馈学习管理要点

同一类被人工判定"判错了"的问题（点击"拒绝"）会在后续校对里反复出现，靠
`cards._render_issue_card` 的拒绝分支解决：记 `feedback.record_rejection(issue,
issue_id, record_id)` 把这条issue存进 `feedback` 表——
`_render_issue_card` 是实时校对流程和历史记录页共用的同一份逻辑，这一行改动
自动覆盖两个入口。历史反馈整批交给LLM做语义总结，产出的规则直接注入校对LLM
的系统提示词，让它在生成建议这一步就主动规避，不由分类器事后改判，详见
`core/feedback_rules.py` 模块docstring。

**规则重算（`regenerate_rejection_rules`，一次真实LLM调用）的触发时机不放在
"拒绝"里**：早期版本每次点"拒绝"都同步重算，连续拒绝会次次触发几秒级LLM调用，
既卡（"拒绝"要等好几秒才响应）又费额度。现在"拒绝"只快速写库、秒响应；重算改到
两个低频的收尾/主动时机：① 标准校对页、原稿比对页"导出Excel"成功后
（`actions.regenerate_rules_after_export`，"这一轮审校完成"的自然收尾，用 st.spinner
给出等待提示）——历史记录页的导出**不**触发，那不是刚审完一批新反馈的场景；
② 反馈页"重新生成规则"按钮手动触发。重算失败只记 `logger`，不影响导出/拒绝
这些主操作（反馈已落库，只是规则集合没刷新）。

事实性错误类问题不参与规则总结——`core/feedback_rules.py::regenerate_rejection_rules`
在喂给LLM之前就按 `config.FEEDBACK_EXEMPT_ISSUE_TYPES` 过滤掉，这是设计铁律
"事实性内容必须始终保留人工复核机会"的延伸。这类问题的拒绝仍会被记录（管理
页"原始反馈记录"可见），只是不会沉淀成规则。

`page_feedback.render_feedback_management` 分两块：顶部"当前生效的反馈
规则"直接展示 `get_feedback_rules()` 读到的已总结规则（纯DB读取，不调用
LLM），并提供"重新生成规则"按钮供用户在删除/调整原始反馈后主动刷新；下方
"原始反馈记录"是按时间倒序的扁平列表，每条支持"撤销"（删除该条反馈记录，
下次重新生成规则时不再参与总结）。

## 原稿比对要点

`page_compare.render_document_comparison` 结构上跟标准校对流平行（两个 st.file_uploader +
"开始比对"按钮 + workflow.run_document_comparison + workflow.persist_comparison_result），
但**结果展示阶段完全复用历史记录页那一套**，不是照抄标准校对流：比对结果落库后，
直接用 get_issues(record_id) 现读 + `cards.row_to_issue_view` + `cards._render_issue_card`
渲染（跟 `page_history._render_history_detail` 一模一样的流程），而不是像标准校对流那样
把内存中的 ClassifiedResult 存进 session_state 再渲染。原因：core.comparer.compare_documents
返回的是 list[dict]，落库之后这些差异条目在 issues 表里跟标准校对的 issue 长得
完全一样（page_location/original_text/issue_type/priority/layer/suggestion 等
字段），没必要为它单独维护一份内存态或另写一套卡片UI——采纳/拒绝/批注/追问/导出
Excel 全部原样可用。

`issue_status`（渲染用的状态缓存）是全局共享的 session_state 字典，键是数据库
自增的 issue_id（标准校对/历史记录/原稿比对三个页面之间不会撞号），所以渲染前
都要用 get_issues 的DB当前值无条件刷新一遍，跟 `_render_history_detail` 处理"两处
状态同步的坑"是同一个原因、同一套写法。

原稿限定只收 Word（type=["docx"]），排版稿收 PDF/Word（type=["pdf","docx"]），
对应框架文档"输入：原稿Word + 排版稿PDF/Word"这条。

## 日志落地与落库失败兜底

**日志**：core/llm_client.py、core/proofreader、
core/classifier/postprocess.py 里的 logger.warning/error 调用要真正落到
data/app.log，需要在 `app.py` 顶部配置 logging.basicConfig(filename=
config.LOG_PATH, ...)；`ui/` 各模块的 except 分支（落库失败、导出失败等）统一
用 logger.exception(...) 记录完整堆栈，不是只有 st.error 给用户看一句话、
事后无法复盘。

**落库失败不丢结果**：workflow.persist_result(...) 之前已经花了真实API额度
跑完LLM校对，写库失败（磁盘满/库被锁）不该让这份结果跟着丢。`_execute_proofread`
拿到 result/parsed 后先无条件存进 session_state["pending_result"/
"pending_parsed"]（在任何落库尝试之前），落库封装进 `_try_persist_pending()`，
失败时只 st.error+记日志、不清空 pending 状态；`render_standard_proofread`
检测到 pending_result 存在但 classified_result 还是 None 时，展示"重试保存"
按钮（复用 `_try_persist_pending`，不重新调用LLM）而不是回到上传界面。原稿比
对（run_document_comparison）没有逐块LLM调用、重跑成本低得多，这里只加了
try/except+日志，没有做同样的 pending 状态机，是有意的范围取舍。

**"全灭"时的提示更醒目**：total_issues==0 且 warnings 非空时，`cards.render_stats`
顶部加一条 st.error 提示"这可能不是文档没有问题"，同时把提示信息 expander
强制展开（expanded=True）——总问题数为0时和"文档真的没问题"长得一模一样，
不能指望用户主动点开一个默认折叠的 expander 才发现。

## 未覆盖范围

边界场景（文档损坏/加密PDF的具体报错是否够清楚、SQLite真实并发写冲突、超大
文件在解析/分块阶段的表现）仍未专门写故障注入测试验证，不是没做，是没有系
统性验收过。
