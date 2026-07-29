"""Streamlit 入口（标准校对流+Excel导出+追问+历史记录详情页+反馈学习管理+原稿比对）。

## 布局与色彩设计要点

全局主题（`.streamlit/config.toml`）把品牌色从Streamlit默认红换成冷色调墨蓝，让红色
在界面里只保留"确定性错误"这一种语义。侧边栏顶部放品牌标识（`_inject_sidebar_brand`），
取代之前主区那个和页面标题重复的巨型 `st.title`；侧边栏顺序为
品牌→当前署名→当前任务→功能入口（署名在选任务之前就要能填，创建任务时要记 created_by）。

四层分类（`config.LAYER_*`）配了一套语义色 `_LAYER_COLOR`，贯穿指标区数字、问题卡片
的左侧色条与标题。指标区六个数字用一整行HTML flex渲染（不用 `st.metric` 混排），保证
各列标签/数字基线对齐；总数上方先给一句自然语言总结再列数字。

问题卡片**不给整卡上底色**（明确要求"背景不上色"），层级/状态只体现在卡片左侧那条4px
色条上：`_inject_card_styles()` 在页面顶层注入一次CSS，靠 `st.container(border=True,
key=...)`（Streamlit 1.32+起容器带 `st-key-<key>` 稳定class）按属性选择器给色条上色。
`_render_issue_card` 的 `card_key` 待处理态用层级slug、已采纳/已拒绝态用固定前缀
（`accepted`/`rejected`），因此"层级色条"和"终态色条（绿/红）"只需要7条CSS规则；
`card_key` 随 `status_info["status"]` 变化，rerun 后色条随之切换，不需要额外状态管理。
原稿比对产出的issue的 `layer` 是另一套取值（`DIFF_LAYER_*`），用 `_OTHER_LAYER_SLUG`
中性灰兜底避免 KeyError。

原文/建议**分两块干净展示，不做字符级diff**：`suggestion` 是自由文本说明（"应改为…"/
"存疑，建议人工核实…"/"原文照录…"），不是和 `original_text` 平行的"改后文本"，硬做
diff只会得到乱码。因此各带一个小灰标签、正文都用黑字，靠标签而不是颜色区分原文与建议；
动态内容一律 `html.escape` 后再拼进HTML。

"归层依据"expander 只在 `LAYER_DOUBTFUL`/`LAYER_QUOTATION`（`_LAYERS_WITH_NOTES`）
两层展示——这两层的判断依赖置信度/引用识别，编辑需要看依据才能决定要不要采纳；
`LAYER_CONFIRMED`/`LAYER_OPTIONAL` 判断相对直接，不展示以免信息过载。

采纳/拒绝之后卡片标题追加一个绿/红状态标签，按钮从"采纳/拒绝"换成单个"撤销"，点击
调用同一个 `workflow.set_issue_status` 把状态改回"待处理"，不新增函数；撤销"已拒绝"
不会撤销已经写进 `feedback` 表的反馈学习记录，两者是独立的历史留痕。

问题卡片列表按 `_render_issue_cards` 两条一行摆放（`st.columns(2)`），和任务选择页
的任务卡是同一个网格模式；标准校对结果、历史记录详情页、原稿比对结果三处都调这一个
函数，不各写一套摆放逻辑。只有**建议**内容预留固定两行高度（`min-height`+`line-height`，
短文本也占满这份高度）——原文是被标记的原句，长度本来就稳定不需要预留；建议是自由
文本说明，长短差异大，才是同一行两张卡参差不齐的来源。**这个两行预留是硬要求，压
卡片高度不能从这里扣**，建议文字字号略调小只是减少超出两行的概率。压高度改从别处
拿，且不做省略号截断（建议文字是校对结论，截断会丢内容）：`_inject_card_styles` 把
卡片 padding 从 Streamlit 默认的 1rem 收紧到 0.5rem/0.8rem，容器用 `gap="xxsmall"`
收紧内部元素间距，批注输入框挤进采纳/拒绝（或撤销）按钮那一行的剩余空间、不再单独占
一整行（`label_visibility="collapsed"` 省掉的标签文字用 placeholder 补上）。

页面不使用emoji：优先级用文字表达，层级差异靠色条+标题色表达。

## 任务闸门与署名要点

**流程是线性的：先选任务，再选功能。** `st.session_state["task_id"]` 没有指向一个
仍然存在的任务时（`get_task` 返回 None 即视为没有），只渲染 `_render_task_selection()`
那一屏并 `st.stop()`——侧边栏的功能入口 radio 根本不会被创建，四个页面一个都进不去。
因此除历史数据外，库里不可能再出现 `task_id` 为 NULL 的 record，`db/models.py::
create_record` 把 `task_id` 设成必传参数即可保证这条约束（表结构上它仍可空，原因见
`db/database.py` 建表语句上方注释）。

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
`_render_task_card` 渲染单张），同样的高度能容纳的任务数因此翻倍，需要滚动的场景更少。

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

侧边栏"切换任务"按钮走 `_switch_task()`：清空 `st.session_state` 里除
`_KEEP_ON_TASK_SWITCH`（署名及其widget状态）以外的所有key。换任务=换工作上下文，
上一个任务的校对结果/待保存结果不该残留；但署名是"我是谁"，跟在哪个任务里无关，
不该因为换任务就要求重选一次。

**署名不是账号**：全部数据都在同一个 `config.DB_PATH` 里，任何人都看得到全部任务与
记录，署名只回答"这条记录是谁做的"，写进 `records.author`。同一轮校对必然由同一个人
跑完并审完，所以 issues 表不另存一份处置人。候选来源是库里出现过的署名
（`get_authors()`，records.author ∪ tasks.created_by），用下拉而非自由文本框——
多人共用时手输必然出现"张三"和"张 三"这种同人不同名。没填署名不阻断校对，只在侧边栏
给一句提示，`author` 落库为 NULL。

## 标准校对分支要点

Streamlit 每次交互都会重跑整个脚本，全部靠 st.session_state（file_id/
classified_result/record_id/issue_ids/issue_status）管住生命周期：换文件
（按文件内容md5哈希判定）才清缓存重新校对；采纳/拒绝按钮触发的 rerun 只读
缓存、不重跑 workflow.run_standard_proofread。没有用 st.cache_*——校对流程
有副作用（写库、耗真实API额度），语义上不适合按输入哈希缓存的机制。

上传的文件先落盘到 config.UPLOADS_DIR（时间戳前缀避免覆盖）再交给
parse_document（该函数吃路径不吃文件对象）。落盘后即用即弃，不进 records 表、
后续也不会再被读取，每次写入后 _prune_uploads_dir 只保留最近
config.UPLOADS_RETENTION_COUNT 个文件，避免上传目录无限堆积。问题卡按钮 key 用
f"accept_{issue_id}"/f"reject_{issue_id}"，全局唯一。异常兜底：
UnsupportedFormatError/NoTextLayerError/LLMCallError/LLMResponseError 分别
给可读提示，兜底 except Exception 防止未预期异常崩页面；出错时不写库、不
缓存结果，允许重试。

st.file_uploader 在页面切走再切回来后，浏览器出于安全限制无法恢复之前选中
的文件，uploaded_file 会变回 None——这不代表用户想清空已校对结果，所以只
有在 uploaded_file 真的拿到新文件时才判断是否换文件/清缓存；uploaded_file
为 None 时直接往下走，看有没有已缓存的结果可以展示。

换文件判断按内容md5哈希（_file_id），"开始校对"按钮只在 classified_result
为 None 时渲染，所以同一份文件（哈希不变）校对完之后这个按钮不会再出现。
"开始校对"的解析→校对→落库逻辑因此抽成 _execute_proofread(uploaded_file,
mode)，供结果展示区顶部"重新校对本文件"按钮复用：uploaded_file 还在
（函数局部变量，没离开过页面）就直接原地重跑；已经因切页丢失（浏览器安全
限制，没有文件字节可用）时才退回 _reset_session_state()+提示重新上传。

## 追问区域要点

_render_issue_card 在采纳/拒绝按钮下方加了"追问"expander（历史用
st.chat_message 渲染，新问题用 text_input+按钮提交，answer_followup 抛出
的 LLMCallError 用 st.error 兜住）。追问历史全程从数据库现读，不进
session_state，和导出模块"数据一律从库读"是同一个原则。

## 导出Excel按钮要点

点击调 core.exporter.export_issues_to_excel(record_id)，异常
用 st.error 兜住；成功后 st.success 提示路径 + st.download_button 提供下载
（mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"）。

## 批注输入框（批注与状态解耦）要点

紧贴采纳/拒绝按钮下方独立一行的"批注（可选，采纳/拒绝/待处理都可以写）"，
用 st.text_input(..., on_change=_save_note) 实现——不需要额外的"保存"按钮，
失焦/回车即通过 on_change 回调写库，批注值本身就靠该 widget 自身 key 对应
的 st.session_state 在 rerun 间保持，不需要在 issue_status 缓存字典里额外
镜像一份。详见 core/workflow/CLAUDE.md"批注与状态解耦"一节。

## 历史记录详情页要点

核心设计：完全复用 _render_issue_card，不为历史记录页另写一套卡片UI。这个
函数一直按"属性访问"（issue.priority/issue.page_location/…）编写，参数在
实时校对流程里是内存中的 ClassifiedIssue；历史记录页的数据来源是
get_issues(record_id) 返回的字典（DB行），两者接口不兼容。没有为此重写
_render_issue_card 或改成字典访问（改了会牵动所有既有调用点和测试），而是
新增 _row_to_issue_view(row: dict)，用 types.SimpleNamespace 把DB字典行包
成同样支持属性访问的对象，实时流程和历史记录页因此共用同一份卡片渲染逻辑，
包括采纳/拒绝按钮、批注输入框、追问expander——全部原样可用，因为这些交互
本来就是直接写库的，不依赖是从哪个页面触发的。

layer_notes（归层依据）历史记录页展示不出来，是数据本身没有，不是遗漏：
issues 表没有 layer_notes 列（ClassifiedIssue.layer_notes 只在校对当次运行
时存在于内存里，persist_result 从未把它落库）。_row_to_issue_view 对这个
字段填的是一句说明文字，不是留空更不是编造假数据。

两处状态同步的坑，处理方式：

1. _render_issue_card 用
   st.session_state["issue_status"].setdefault(issue_id, {"status": "待处理"})
   取显示用的状态——这个 setdefault 是给实时流程设计的（issue刚生成时确实
   是"待处理"）。如果历史记录页不做任何处理直接复用，会把DB里明明"已采纳"
   的历史issue，在还没被点击过的这个新会话里错误显示成"待处理"。修复：
   _render_history_detail 在渲染问题卡之前，先用 get_issues 查到的DB当前值
   无条件覆写 st.session_state["issue_status"][issue_id]（不是 setdefault，
   就是直接赋值），每次进页面/切换记录/点完按钮触发 rerun 后都会重新执行
   这一步，保证展示的状态永远是DB真实值。
2. 批注输入框 st.text_input(key=f"note_{issue_id}", ...) 同理：Streamlit
   组件的初始值来自 st.session_state[key]，历史issue的批注只存在DB里，这
   个会话从没设置过对应的 note_{issue_id} key，不预填就会显示空白，看起来
   像"批注丢了"。修复：_render_history_detail 在第一次遇到某个 issue_id 时
   （if note_key not in st.session_state）用DB的 note 值预填——用条件预填
   而不是像状态那样无条件覆写，是为了不打断用户正在输入但还没触发
   on_change（失焦/回车）的编辑内容。

_render_stats 接受 (stats: dict, warnings: list) 两个原始值，不是 ClassifiedResult
对象——db.models.get_records() 返回的字典字段名本就和 ClassifiedResult.stats 一一
对应，直接传历史record字典即可；历史记录页 warnings 传空列表（records 表没存解析/
分块警告）。文档名/校对模式的副标题由调用方用 _render_doc_subtitle 在 st.header 下
单独渲染（那里才拿得到 doc_name），不进 _render_stats。

导出Excel按钮在历史记录页复用 core.exporter.export_issues_to_excel，与标准
校对页实现一致；key= 加上 record_id 后缀
（history_export_{record_id}/history_download_{record_id}）避免和标准校对
页的同名按钮/未来可能的多记录场景冲突。

## 反馈学习管理要点

同一类被人工判定"判错了"的问题（点击"拒绝"）会在后续校对里反复出现，靠
`_render_issue_card` 的拒绝分支解决：记 `feedback.record_rejection(issue,
issue_id, record_id)` 把这条issue存进 `feedback` 表——
`_render_issue_card` 是实时校对流程和历史记录页共用的同一份逻辑，这一行改动
自动覆盖两个入口。历史反馈整批交给LLM做语义总结，产出的规则直接注入校对LLM
的系统提示词，让它在生成建议这一步就主动规避，不由分类器事后改判，详见
`core/feedback_rules.py` 模块docstring。

**规则重算（`regenerate_rejection_rules`，一次真实LLM调用）的触发时机不放在
"拒绝"里**：早期版本每次点"拒绝"都同步重算，连续拒绝会次次触发几秒级LLM调用，
既卡（"拒绝"要等好几秒才响应）又费额度。现在"拒绝"只快速写库、秒响应；重算改到
两个低频的收尾/主动时机：① 标准校对页、原稿比对页"导出Excel"成功后
（`_regenerate_rules_after_export`，"这一轮审校完成"的自然收尾，用 st.spinner
给出等待提示）——历史记录页的导出**不**触发，那不是刚审完一批新反馈的场景；
② 反馈页"重新生成规则"按钮手动触发。重算失败只记 `logger`，不影响导出/拒绝
这些主操作（反馈已落库，只是规则集合没刷新）。

事实性错误类问题不参与规则总结——`core/feedback_rules.py::regenerate_rejection_rules`
在喂给LLM之前就按 `config.FEEDBACK_EXEMPT_ISSUE_TYPES` 过滤掉，这是设计铁律
"事实性内容必须始终保留人工复核机会"的延伸。这类问题的拒绝仍会被记录（管理
页"原始反馈记录"可见），只是不会沉淀成规则。

"反馈学习"页面（`_render_feedback_management`）分两块：顶部"当前生效的反馈
规则"直接展示 `get_feedback_rules()` 读到的已总结规则（纯DB读取，不调用
LLM），并提供"重新生成规则"按钮供用户在删除/调整原始反馈后主动刷新；下方
"原始反馈记录"是按时间倒序的扁平列表，每条支持"撤销"（删除该条反馈记录，
下次重新生成规则时不再参与总结）。

## 原稿比对要点

`_render_document_comparison` 结构上跟标准校对流平行（两个 st.file_uploader +
"开始比对"按钮 + workflow.run_document_comparison + workflow.persist_comparison_result），
但**结果展示阶段完全复用历史记录页那一套**，不是照抄标准校对流：比对结果落库后，
直接用 get_issues(record_id) 现读 + _row_to_issue_view + _render_issue_card 渲染
（跟 _render_history_detail 一模一样的流程），而不是像标准校对流那样把内存中的
ClassifiedResult 存进 session_state 再渲染。原因：core.comparer.compare_documents
返回的是 list[dict]，落库之后这些差异条目在 issues 表里跟标准校对的 issue 长得
完全一样（page_location/original_text/issue_type/priority/layer/suggestion 等
字段），没必要为它单独维护一份内存态或另写一套卡片UI——采纳/拒绝/批注/追问/导出
Excel 全部原样可用。

`issue_status`（渲染用的状态缓存）是全局共享的 session_state 字典，键是数据库
自增的 issue_id（标准校对/历史记录/原稿比对三个页面之间不会撞号），所以渲染前
都要用 get_issues 的DB当前值无条件刷新一遍，跟 _render_history_detail 处理"两处
状态同步的坑"是同一个原因、同一套写法。

原稿限定只收 Word（type=["docx"]），排版稿收 PDF/Word（type=["pdf","docx"]），
对应框架文档"输入：原稿Word + 排版稿PDF/Word"这条。

## 日志落地与落库失败兜底

**日志**：core/llm_client.py、core/proofreader、
core/classifier/postprocess.py 里的 logger.warning/error 调用要真正落到
data/app.log，需要在本文件顶部配置 logging.basicConfig(filename=
config.LOG_PATH, ...)；本文件内的 except 分支（落库失败、导出失败等）统一
用 logger.exception(...) 记录完整堆栈，不是只有 st.error 给用户看一句话、
事后无法复盘。

**落库失败不丢结果**：workflow.persist_result(...) 之前已经花了真实API额度
跑完LLM校对，写库失败（磁盘满/库被锁）不该让这份结果跟着丢。_execute_proofread
拿到 result/parsed 后先无条件存进 session_state["pending_result"/
"pending_parsed"]（在任何落库尝试之前），落库封装进 _try_persist_pending()，
失败时只 st.error+记日志、不清空 pending 状态；_render_standard_proofread
检测到 pending_result 存在但 classified_result 还是 None 时，展示"重试保存"
按钮（复用 _try_persist_pending，不重新调用LLM）而不是回到上传界面。原稿比
对（run_document_comparison）没有逐块LLM调用、重跑成本低得多，这里只加了
try/except+日志，没有做同样的 pending 状态机，是有意的范围取舍。

**"全灭"时的提示更醒目**：total_issues==0 且 warnings 非空时，_render_stats
顶部加一条 st.error 提示"这可能不是文档没有问题"，同时把提示信息 expander
强制展开（expanded=True）——总问题数为0时和"文档真的没问题"长得一模一样，
不能指望用户主动点开一个默认折叠的 expander 才发现。

## 未覆盖范围

边界场景（文档损坏/加密PDF的具体报错是否够清楚、SQLite真实并发写冲突、超大
文件在解析/分块阶段的表现）仍未专门写故障注入测试验证，不是没做，是没有系
统性验收过。
"""

import hashlib
import html
import logging
import types
from datetime import datetime
from pathlib import Path

import streamlit as st

import config
from core import exporter, feedback, feedback_rules, followup, workflow
from core.llm_client import LLMCallError
from core.parser import NoTextLayerError, UnsupportedFormatError
from core.proofreader import LLMResponseError
from db.database import init_db
from db.models import (
    count_records_by_task,
    create_task,
    get_authors,
    get_feedback,
    get_feedback_rules,
    get_issues,
    get_records,
    get_task,
    get_tasks,
    update_record_task,
    update_task_status,
)

logging.basicConfig(
    filename=str(config.LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="出版校对AI助手", layout="wide")

_LAYER_SLUG = {
    config.LAYER_CONFIRMED: "confirmed",
    config.LAYER_DOUBTFUL: "doubtful",
    config.LAYER_QUOTATION: "quotation",
    config.LAYER_OPTIONAL: "optional",
}
# 四层语义色，贯穿指标区数字、问题卡片的层级色条与标题——同一层级在哪都用同一个色。
_LAYER_COLOR = {
    config.LAYER_CONFIRMED: "#D66E45",
    config.LAYER_DOUBTFUL: "#C5C81F",
    config.LAYER_QUOTATION: "#3B7DD8",
    config.LAYER_OPTIONAL: "#7A7288FF",
}
# 采纳=绿、拒绝=红，只用在卡片左侧色条与状态标签上（不给整卡上底色）。
_STATUS_COLOR = {"已采纳": "#2E8B57", "已拒绝": "#D64570"}
# 原稿比对产出的issue的 layer 字段取值是 config.DIFF_LAYER_SUBSTANTIVE/
# DIFF_LAYER_FORMATTING（独立于四层分类的另一个轴），不在 _LAYER_SLUG/_LAYER_COLOR
# 里——用这个中性灰兜底，避免 KeyError。
_OTHER_LAYER_SLUG = "other"
_OTHER_LAYER_COLOR = "#8a8f98"
# 存疑类/引文类的判断依赖置信度/引用识别，需要人工复核依据；错误类/风格类判断相对
# 直接，卡片不展示"归层依据"以免信息过载。
_LAYERS_WITH_NOTES = (config.LAYER_DOUBTFUL, config.LAYER_QUOTATION)


def _inject_card_styles() -> None:
    """一次性注入问题卡片的层级左色条样式。

    不给整卡上底色（用户明确要求"背景不上色"），只在卡片左侧加一条4px色条表达层级/
    状态：待处理态按层级色，已采纳=绿、已拒绝=红。靠 st.container(border=True, key=...)
    自动生成的稳定 st-key-<key> CSS class（Streamlit 1.32+）做属性选择器，一条规则覆盖
    该层级/状态的所有卡片，不需要为每张卡片单独出样式。终态 key 用固定前缀（不含层级
    slug），因此"层级×终态"不必各写一条规则。
    """
    # padding 从 Streamlit 默认的 1rem 收紧到 0.5rem/0.8rem，是压缩卡片高度的一部分
    # （另一部分是 _render_issue_card 内部的 min-height/gap 收紧），只用于问题卡，
    # 不影响页面其他 st.container(border=True)（选择器限定在 st-key-issue_card_ 前缀）。
    bar = "border-left: 4px solid {color}; border-radius: 2px; padding: 0.5rem 0.8rem;"
    rules = "\n".join(
        f'div[class*="st-key-issue_card_{slug}_"] {{ {bar.format(color=color)} }}'
        for slug, color in (
            (_LAYER_SLUG[config.LAYER_CONFIRMED], _LAYER_COLOR[config.LAYER_CONFIRMED]),
            (_LAYER_SLUG[config.LAYER_DOUBTFUL], _LAYER_COLOR[config.LAYER_DOUBTFUL]),
            (_LAYER_SLUG[config.LAYER_QUOTATION], _LAYER_COLOR[config.LAYER_QUOTATION]),
            (_LAYER_SLUG[config.LAYER_OPTIONAL], _LAYER_COLOR[config.LAYER_OPTIONAL]),
            ("accepted", _STATUS_COLOR["已采纳"]),
            ("rejected", _STATUS_COLOR["已拒绝"]),
            (_OTHER_LAYER_SLUG, _OTHER_LAYER_COLOR),
        )
    )
    st.markdown(f"<style>\n{rules}\n</style>", unsafe_allow_html=True)


def _inject_sidebar_brand() -> None:
    """在侧边栏顶部放一个品牌标识（色块logo+名称），替代之前主区那个和页面标题重复的
    巨型 st.title。顺带压掉侧边栏默认的顶部留白，让品牌贴着顶部。
    """
    st.markdown(
        "<style>section[data-testid='stSidebar'] div[data-testid='stSidebarHeader']"
        "{padding-bottom:0}</style>",
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(
        "<div style='display:flex;align-items:center;gap:10px;padding:4px 0 12px'>"
        "<div style='width:34px;height:34px;border-radius:8px;background:#2C5578;"
        "color:#fff;display:flex;align-items:center;justify-content:center;"
        "font-weight:700;font-size:1.05rem'>校</div>"
        "<div style='font-size:1.15rem;font-weight:700;color:#1A1A1A'>校对助手</div>"
        "</div>",
        unsafe_allow_html=True,
    )


_inject_sidebar_brand()
_inject_card_styles()

init_db()

_NEW_AUTHOR_OPTION = "＋新增署名…"
# 切换任务时保留的 session_state key：署名是"我是谁"，跟"现在在哪个任务里"是两个
# 独立维度，换任务不该要求重选一次署名（另两个是署名选择器自身的widget状态，一并留下
# 才能让下拉/输入框在rerun后仍显示同一个人）。
_KEEP_ON_TASK_SWITCH = {"author", "author_select", "new_author_input"}


def _render_author_picker() -> None:
    """侧边栏的"当前署名"选择器：从库里出现过的署名里选，或新增一个。

    署名的语义是"这条校对记录是谁做的"，不是登录账号，也不再决定数据存放位置——
    全部数据都在同一个库里，任何人都看得到全部任务与记录。用下拉而不是自由文本框，
    是因为多人共用时手输必然出现"张三"和"张 三"这种同人不同名。

    位置在任务之上（而不是像早先的"当前用户"那样压在侧边栏最底部）：创建任务要记
    created_by，所以选任务那一屏就得能填署名，而那一屏之后的代码全被 st.stop() 挡住了。
    """
    options = get_authors() + [_NEW_AUTHOR_OPTION]
    current = st.session_state.get("author")
    index = options.index(current) if current in options else len(options) - 1
    choice = st.sidebar.selectbox("当前署名", options, index=index, key="author_select")

    if choice == _NEW_AUTHOR_OPTION:
        typed = st.sidebar.text_input(
            "新署名", key="new_author_input", placeholder="填写后即生效"
        ).strip()
        st.session_state["author"] = typed or None
    else:
        st.session_state["author"] = choice

    if not st.session_state.get("author"):
        st.sidebar.caption("未填写署名，本次校对记录不会标注完成人。")


def _switch_task() -> None:
    """回到任务选择界面，并清空当前任务范围内的工作状态。

    换任务等于换了一个工作上下文，上一个任务的校对结果/待保存结果/选中记录都不该
    残留下来显示成新任务的东西（数据本身在库里，回到原任务能重新看到）；署名相关的
    key 例外，见 _KEEP_ON_TASK_SWITCH。
    """
    for _key in list(st.session_state.keys()):
        if _key not in _KEEP_ON_TASK_SWITCH:
            del st.session_state[_key]
    st.rerun()


def _render_task_card(task: dict, counts: dict[int, int]) -> None:
    """渲染任务选择页里的一张任务卡片（半宽双列布局下的一格）。

    卡片变窄后不再用"信息+状态+进入"三栏并排（会挤成一团），改成信息独占一行、
    状态下拉与进入按钮共占下一行——两级布局比硬塞进三个窄栏更适合半宽卡片。
    """
    with st.container(border=True):
        st.markdown(
            f"**{html.escape(task['name'])}**　"
            f"<span style='color:#999;font-size:0.85rem'>"
            f"{counts.get(task['task_id'], 0)} 条记录 · 创建于 {task['created_at'][:10]}"
            f"{' · ' + html.escape(task['created_by']) if task['created_by'] else ''}"
            f"</span>",
            unsafe_allow_html=True,
        )
        # 固定单行高度：st.caption是普通文本会自然换行，备注长短不一时换行行数
        # 跟着不一样，卡片就会高矮不齐（试过用空格占位只解决"有没有"，解决不了
        # "长短"）。改用自己拼HTML，nowrap+ellipsis强制卡进一行、超出截断，
        # 没有备注时占位一个空格——这样不管有没有备注、备注多长，这一行的高度都恒定。
        desc_text = html.escape(task["description"]) if task["description"] else " "
        st.markdown(
            f"<div style='color:#808495;font-size:0.8rem;white-space:nowrap;"
            f"overflow:hidden;text-overflow:ellipsis;margin:2px 0 6px' "
            f"title='{desc_text}'>{desc_text}</div>",
            unsafe_allow_html=True,
        )

        # 状态就地改，不必进任务详情——这是任务列表上唯一需要的管理动作。选"已关闭"
        # 提交后，下次rerun这个任务就不再出现在tasks里（见调用方的过滤），它对应的
        # 这套widget自然跟着消失，不需要额外处理。
        col_status, col_enter = st.columns([3, 2])
        status_index = (
            config.TASK_STATUSES.index(task["status"])
            if task["status"] in config.TASK_STATUSES
            else 0
        )
        new_status = col_status.selectbox(
            "状态",
            config.TASK_STATUSES,
            index=status_index,
            key=f"task_status_{task['task_id']}",
            label_visibility="collapsed",
        )
        if new_status != task["status"]:
            update_task_status(task["task_id"], new_status)
            st.rerun()

        if col_enter.button("进入", key=f"enter_task_{task['task_id']}", use_container_width=True):
            st.session_state["task_id"] = task["task_id"]
            st.rerun()


def _render_task_selection() -> None:
    """任务选择/创建界面——没有当前任务时，整个应用只显示这一屏。

    流程是线性的：先选任务（或建任务），才出现功能入口。调用方在这之后立刻 st.stop()，
    所以侧边栏导航和四个功能页的代码根本不会执行。
    """
    st.header("选择任务")
    st.caption("每一次校对都归属于一个任务（比如某本期刊）。先选择要进入的任务，再选择功能。")

    show_all = st.checkbox("显示已解决的任务", value=False)
    tasks = get_tasks(status=None if show_all else config.TASK_STATUS_ACTIVE)
    # "已关闭"在前端任何地方都不展示——下拉框里选它能把任务关闭，但关闭后这个任务立刻
    # 从这里以及"改归属任务"下拉（_render_move_record_to_task）里消失，不受上面这个
    # 复选框影响；想再看到/改回来，只能直接改数据库（config.TASK_STATUS_CLOSED 因此是
    # 前端唯一能写入、但读不出来的状态值）。
    tasks = [t for t in tasks if t["status"] != config.TASK_STATUS_CLOSED]
    counts = count_records_by_task()

    if not tasks:
        st.info("还没有任务，请在下方新建一个。")
    else:
        # 固定高度、内部滚动——任务一多不能让整页跟着往下拉，把"新建任务"表单挤出屏幕。
        # 每行两张卡片（各占半宽），同样的高度能容纳的行数因此减半，滚动更少。
        with st.container(height=420):
            for i in range(0, len(tasks), 2):
                cols = st.columns(2)
                # get_tasks() 按创建时间倒序返回（最新在前），每行右边放更新的一条、
                # 左边放更旧的一条——cols[::-1] 反转列顺序去配对，奇数条数的最后一行
                # 只剩一条时也会落在右边，不会出现"左边比右边新"的情况。
                for col, task in zip(cols[::-1], tasks[i : i + 2]):
                    with col:
                        _render_task_card(task, counts)

    st.divider()
    st.subheader("新建任务")
    new_name = st.text_input("任务名称", key="new_task_name")
    new_desc = st.text_area("备注（选填）", key="new_task_desc")

    # 任务名不做唯一性校验——task_id才是真正的标识，卡片上创建日期/记录数已经够分辨。
    # 但输入的名字和现有任务（已关闭的除外，那些前端本来就看不到）撞了，说明大概率是
    # 手滑，提醒一下、不永久阻止创建。
    typed_name = new_name.strip()
    is_duplicate = False
    if typed_name:
        existing_names = {
            t["name"] for t in get_tasks() if t["status"] != config.TASK_STATUS_CLOSED
        }
        is_duplicate = typed_name in existing_names

    # 撞名时不能让"警告"和"创建成功后立刻rerun跳进新任务"落在同一次脚本执行里——
    # 那样警告刚算出来、页面已经跳走，浏览器根本没机会把它画出来（几乎不可见，
    # 真实反馈过的问题）。所以撞名的第一次点击只记一个"已提醒过这个名字"的标记、
    # 不创建，让警告单独停留一次刷新；名字不变的情况下再点一次才真正创建，
    # 名字改了（不再撞名，或撞了另一个名字）则这个标记自动失效，按新状态重新走一遍。
    if is_duplicate:
        st.warning(
            f"已有同名任务「{typed_name}」，请检查是否失误。"
            "确认新建请再点击一次「创建并进入」。"
        )

    if st.button("创建并进入"):
        if not typed_name:
            st.error("任务名称不能为空。")
        elif is_duplicate and st.session_state.get("task_dup_ack_name") != typed_name:
            st.session_state["task_dup_ack_name"] = typed_name
        else:
            task_id = create_task(
                typed_name,
                description=new_desc.strip() or None,
                created_by=st.session_state.get("author"),
            )
            st.session_state.pop("task_dup_ack_name", None)
            st.session_state["task_id"] = task_id
            st.rerun()


_render_author_picker()

# 任务闸门：session_state 里没有一个仍然存在的当前任务时，只渲染任务选择界面。
# get_task 的 None 兜底覆盖"库被换掉/任务被删掉"导致 task_id 悬空的情况。
current_task = (
    get_task(st.session_state["task_id"]) if st.session_state.get("task_id") else None
)
if current_task is None:
    st.session_state.pop("task_id", None)
    _render_task_selection()
    st.stop()

st.sidebar.markdown(f"**当前任务**　{current_task['name']}")
st.sidebar.caption(f"状态：{current_task['status']}")
if st.sidebar.button("切换任务"):
    _switch_task()

st.sidebar.divider()
page = st.sidebar.radio("功能入口", ("标准校对", "原稿比对", "历史记录", "反馈学习"))


def _file_id(uploaded_file) -> str:
    return hashlib.md5(uploaded_file.getvalue()).hexdigest()


def _prune_uploads_dir(directory: Path) -> None:
    """只保留 directory 下最近修改的 config.UPLOADS_RETENTION_COUNT 个文件，其余删除。

    上传文件落盘只是为了给 parse_document/run_document_comparison 一个路径读，写入后
    立即被消费，不进 records 表、后续也不会再被读取，删旧文件不影响任何已生成的结果。
    """
    files = [f for f in directory.iterdir() if f.is_file()]
    if len(files) <= config.UPLOADS_RETENTION_COUNT:
        return
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    for stale in files[config.UPLOADS_RETENTION_COUNT:]:
        stale.unlink(missing_ok=True)


def _reset_session_state():
    st.session_state["classified_result"] = None
    st.session_state["record_id"] = None
    st.session_state["issue_ids"] = []
    st.session_state["issue_status"] = {}
    st.session_state["mode"] = None
    # pending_* 属于上一次校对留下的"已算出结果但还没存库成功"暂存态（见
    # _try_persist_pending），换文件时这份结果已经跟不上当前上传的文件了，一并清掉。
    st.session_state["pending_result"] = None
    st.session_state["pending_parsed"] = None
    st.session_state["pending_doc_name"] = None


def _render_doc_subtitle(doc_name: str | None, mode: str | None) -> None:
    """在 st.header 之下渲染"文档名 · 模式"副标题（目标设计里页面标题下那行灰字）。
    doc_name 缺失时只显示模式，两者都缺时不渲染。
    """
    parts = [p for p in (doc_name, f"{mode}模式" if mode else None) if p]
    if parts:
        st.caption(" · ".join(parts))


def _regenerate_rules_after_export() -> None:
    """导出Excel成功后顺带把历史拒绝总结成规避规则跑一次。

    规则重算是一次几秒的LLM调用，之前放在每次点"拒绝"里，连续拒绝会次次触发、又慢又
    费额度。导出是"这一轮审校完成"的自然收尾动作、频率低，把重算挪到这里既让"拒绝"
    秒响应，又能让反馈自动生效，还把 N 次拒绝的 N 次重算压成 1 次。只在标准校对/原稿
    比对页的导出后调用（那两处是刚审完一批新反馈的场景）；历史记录页的导出不触发。
    失败只记日志，不影响导出这个主操作，也可随时去反馈页手动"重新生成规则"。
    """
    try:
        with st.spinner("正在根据本轮反馈更新规避规则…"):
            feedback_rules.regenerate_rejection_rules()
    except Exception:
        logger.exception("反馈规则重新生成失败")


def _render_stats(stats: dict, warnings: list):
    """渲染统计总结句 + 六个对齐的指标数字。stats 取值只用 .get()，兼容 ClassifiedResult.stats
    和 db.models.get_records() 返回的原始字典（字段名本就一一对应，历史记录页直接传records行）。

    文档名/校对模式的副标题由调用方在 st.header 之下自行渲染（那里才拿得到 doc_name），
    不在本函数里。
    """
    total_issues = stats.get("total_issues", 0)
    confirmed = stats.get("count_confirmed", 0)
    doubtful = stats.get("count_doubtful", 0)
    quotation = stats.get("count_quotation", 0)
    optional = stats.get("count_optional", 0)
    high = stats.get("high_priority_count", 0)

    # 0个问题 + 存在警告，很可能是"部分/全部chunk校对失败"而不是"文档真的没问题"——
    # 这两种情况在"总问题数"这个最显眼的指标上长得一模一样，不能只让用户自己点开
    # 下面默认折叠的提示框才发现，必须在结果为0时主动提醒来看警告。
    if total_issues == 0 and warnings:
        st.error(
            f"本次结果为0条问题，但同时有{len(warnings)}条处理失败/异常提示——"
            "这很可能不是「文档没有问题」，而是部分或全部内容没有被真正校对到，"
            "请务必查看下方「提示信息」再下结论。"
        )

    # 先给一句自然语言总结（"助手会总结，工具只会统计"），再列指标数字。
    st.markdown(
        f"本次共发现 **{total_issues}** 处："
        f"{config.LAYER_CONFIRMED} {confirmed}、{config.LAYER_DOUBTFUL} {doubtful}、"
        f"{config.LAYER_QUOTATION} {quotation}、{config.LAYER_OPTIONAL} {optional}"
        f"（其中高优先级 {high}）。"
    )

    # 六个指标用同一个HTML flex行渲染，保证标签/数字在各列之间基线对齐（之前 st.metric
    # 和手写markdown混用导致高低不齐）；四个层级数字用 _LAYER_COLOR 语义色，总数/高优先级
    # 用中性深色。
    cells = (
        ("总问题数", total_issues, "#1A1A1A"),
        (config.LAYER_CONFIRMED, confirmed, _LAYER_COLOR[config.LAYER_CONFIRMED]),
        (config.LAYER_DOUBTFUL, doubtful, _LAYER_COLOR[config.LAYER_DOUBTFUL]),
        (config.LAYER_QUOTATION, quotation, _LAYER_COLOR[config.LAYER_QUOTATION]),
        (config.LAYER_OPTIONAL, optional, _LAYER_COLOR[config.LAYER_OPTIONAL]),
        ("高优先级", high, "#1A1A1A"),
    )
    cells_html = "".join(
        "<div style='flex:1;min-width:90px'>"
        f"<div style='font-size:0.8rem;color:#666;margin-bottom:2px'>{html.escape(label)}</div>"
        f"<div style='font-size:2rem;font-weight:600;line-height:1.15;color:{color}'>{value}</div>"
        "</div>"
        for label, value, color in cells
    )
    st.markdown(
        f"<div style='display:flex;gap:16px;flex-wrap:wrap;margin:6px 0 4px'>{cells_html}</div>",
        unsafe_allow_html=True,
    )

    if warnings:
        with st.expander(f"提示信息（{len(warnings)}条）", expanded=(total_issues == 0)):
            for w in warnings:
                st.warning(w)


def _render_issue_card(issue, issue_id, record_id):
    status_info = st.session_state["issue_status"].setdefault(issue_id, {"status": "待处理"})
    status = status_info["status"]

    # 容器 key 决定卡片左侧色条颜色（见 _inject_card_styles，不给整卡上底色）：待处理态
    # 按层级色，已采纳=绿、已拒绝=红。用固定前缀区分终态，rerun 后 key 变化、色条随之切换，
    # 不需要额外状态管理代码。
    if status == "已采纳":
        card_key = f"issue_card_accepted_{issue_id}"
    elif status == "已拒绝":
        card_key = f"issue_card_rejected_{issue_id}"
    else:
        card_key = f"issue_card_{_LAYER_SLUG.get(issue.layer, _OTHER_LAYER_SLUG)}_{issue_id}"

    layer_color = _LAYER_COLOR.get(issue.layer, _OTHER_LAYER_COLOR)
    # 标题：层级名+定位用层级色，优先级用灰色；终态再追加一个绿/红状态标签。
    header = (
        f"<span style='color:{layer_color};font-weight:600'>"
        f"{html.escape(issue.layer)} · {html.escape(issue.page_location or '未定位')}</span>"
        f"<span style='color:#999;font-weight:400'> · {html.escape(issue.priority)}</span>"
    )
    if status in _STATUS_COLOR:
        header += (
            f"<span style='color:{_STATUS_COLOR[status]};font-weight:600'> · {status}</span>"
        )

    with st.container(border=True, key=card_key, gap="xxsmall"):
        st.markdown(header, unsafe_allow_html=True)
        # 干净地分两块展示原文和建议（不做字符级diff：suggestion 是自由文本说明，不是
        # 平行的"改后文本"，硬做diff只会得到乱码）。用小灰标签区分两块，正文都用黑字，
        # 一眼能看清哪句是原文、哪句是建议。原文本身是被标记的一小段原句，长度稳定，
        # 不预留高度；建议是自由文本说明，长短差异大，才是同一行两张卡参差不齐的来源，
        # 只在建议上预留两行高度（min-height+line-height，短文本也占满这份高度）——
        # 这个预留不参与"压卡片高度"，压缩改从别处拿（卡片padding、容器gap、批注行合并
        # 到按钮行，见下方与 _inject_card_styles）；建议字号略调小，减少超出两行的概率。
        # 容器整体用 gap="xxsmall" 压缩，但那个间距对"建议"这段正文和下一个元素
        # （归层依据expander，或没有该expander时的采纳/拒绝按钮行）来说太挤、看着像
        # 糊在一起，所以在建议块自己身上单独补一个 margin-bottom，不改动其他地方的
        # 间距（不拉高header-content、按钮行-追问expander之间已经压紧的空隙）。
        st.markdown(
            f"<div style='margin-top:1px'><span style='color:#999;font-size:0.78rem'>原文</span>"
            f"<div style='color:#1A1A1A'>{html.escape(issue.original_text)}</div></div>"
            f"<div style='margin-top:3px;margin-bottom:10px'>"
            f"<span style='color:#999;font-size:0.78rem'>建议</span>"
            f"<div style='color:#1A1A1A;font-size:0.9rem;line-height:1.4;min-height:2.8em'>"
            f"{html.escape(issue.suggestion)}</div></div>",
            unsafe_allow_html=True,
        )

        # 存疑类/引文类的判断依赖置信度/引用识别，需要人工复核依据；错误类/风格类判断
        # 相对直接，不展示这个expander以免信息过载（见 _LAYERS_WITH_NOTES 定义处说明）。
        if issue.layer in _LAYERS_WITH_NOTES:
            with st.expander("归层依据"):
                st.caption("；".join(issue.layer_notes))

        def _save_note():
            workflow.set_issue_note(issue_id, st.session_state.get(f"note_{issue_id}", ""))

        # 批注输入框跟采纳/拒绝（或撤销）按钮挤在同一行的剩余空间里，不再单独占一整行——
        # 是压缩卡片高度的一部分，其余是上面 min-height 收紧和 _inject_card_styles 里的
        # padding 收紧。label_visibility="collapsed" 省掉的"批注"文字标签行用占位符补上，
        # 紧挨着按钮不影响辨识。
        if status == "待处理":
            col_accept, col_reject, col_note = st.columns([1, 1, 4])
            if col_accept.button("采纳", key=f"accept_{issue_id}"):
                workflow.set_issue_status(issue_id, "已采纳", record_id=record_id)
                status_info["status"] = "已采纳"
                st.rerun()

            if col_reject.button("拒绝", key=f"reject_{issue_id}"):
                workflow.set_issue_status(issue_id, "已拒绝", record_id=record_id)
                feedback.record_rejection(issue, issue_id, record_id)
                # 只快速记录这条拒绝，不在这里同步跑规则总结——那是一次几秒的LLM调用，
                # 连续拒绝会次次触发、又慢又费额度。规则重算改到"导出Excel成功后"
                # （_regenerate_rules_after_export，一轮审校的收尾动作）和反馈页手动按钮触发。
                status_info["status"] = "已拒绝"
                st.rerun()
        else:
            # 卡片底色已经表达了"已采纳"/"已拒绝"这个终态，不再需要"— 待处理"这类文字；
            # 撤销直接把状态改回待处理，复用通用的 set_issue_status，不新增函数。撤销
            # "已拒绝"不撤销已写入的 feedback 表记录——反馈学习是独立的历史留痕。
            col_undo, col_note = st.columns([1, 5])
            if col_undo.button("撤销", key=f"undo_{issue_id}"):
                workflow.set_issue_status(issue_id, "待处理", record_id=record_id)
                status_info["status"] = "待处理"
                st.rerun()

        col_note.text_input(
            "批注",
            key=f"note_{issue_id}",
            on_change=_save_note,
            placeholder="批注（可选）",
            label_visibility="collapsed",
        )

        with st.expander("追问"):
            for turn in followup.get_followup_history(issue_id):
                with st.chat_message("user"):
                    st.write(turn["question"])
                with st.chat_message("assistant"):
                    st.write(turn["answer"])

            question = st.text_input(
                "输入追问", key=f"followup_input_{issue_id}", label_visibility="collapsed"
            )
            if st.button("发送", key=f"followup_submit_{issue_id}"):
                if question.strip():
                    try:
                        with st.spinner("正在思考…"):
                            followup.answer_followup(issue_id, question)
                    except LLMCallError as exc:
                        logger.error("追问失败: %s", exc)
                        st.error(f"追问失败：{exc}")
                    else:
                        st.rerun()


def _render_issue_cards(items, record_id, n_cols: int = 2) -> None:
    """两条一行的网格布局渲染问题卡列表，压缩列表纵向长度（原先一条卡打满整行，问题
    一多列表就很长）。items 是 (issue, issue_id) 二元组列表；和任务选择页任务卡的两列
    网格是同一个模式（见本文件顶部 docstring）。试过三列，但单卡变窄后原文/建议更容易
    换行，同一行内卡片高度参差不齐反而更明显，改回两列。
    """
    for i in range(0, len(items), n_cols):
        cols = st.columns(n_cols)
        for col, (issue, issue_id) in zip(cols, items[i : i + n_cols]):
            with col:
                _render_issue_card(issue, issue_id, record_id)


def _execute_proofread(uploaded_file, mode: str) -> bool:
    """实际跑一遍 解析→校对→分层，再尝试落库，把结果写进 session_state。

    从"开始校对"按钮和"重新校对本文件"按钮两处调用（后者需要 uploaded_file 还没
    因切页丢失才能直接调用，见 _render_standard_proofread）。返回是否成功——失败
    时已经用 st.error 展示了原因，调用方只需要据此决定要不要 st.rerun()。

    解析/校对阶段的异常（还没花出真实API额度算出结果，或者压根没算出来）直接
    return False，没有数据可丢。一旦拿到 result/parsed（意味着已经花了真实API
    额度），无条件先存进 session_state["pending_result"/"pending_parsed"]，落库
    单独交给 _try_persist_pending 处理——即使落库失败，这份结果也不会跟着丢。
    """
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    save_path = config.UPLOADS_DIR / f"{timestamp}_{uploaded_file.name}"
    save_path.write_bytes(uploaded_file.getvalue())
    _prune_uploads_dir(config.UPLOADS_DIR)

    progress_bar = st.progress(0.0)
    status_text = st.empty()

    def _on_progress(current, total):
        progress_bar.progress(current / total if total else 1.0)
        status_text.text(f"已完成 {current}/{total} 块（所有块并发校对中）…")

    try:
        with st.spinner("正在校对，请稍候…"):
            result, parsed = workflow.run_standard_proofread(
                str(save_path), progress_callback=_on_progress, mode=mode
            )
    except (UnsupportedFormatError, NoTextLayerError) as exc:
        logger.warning("文档解析失败: %s", exc)
        st.error(f"文档解析失败：{exc}")
        return False
    except LLMCallError as exc:
        logger.error("LLM调用失败: %s", exc)
        st.error(f"LLM调用失败（请检查 DASHSCOPE_API_KEY 等配置）：{exc}")
        return False
    except LLMResponseError as exc:
        logger.error("LLM输出解析失败: %s", exc)
        st.error(f"LLM输出解析失败：{exc}")
        return False
    except Exception as exc:  # noqa: BLE001 兜底，避免未预期异常打崩页面
        logger.exception("校对过程中发生未预期错误")
        st.error(f"校对过程中发生未预期错误：{exc}")
        return False

    st.session_state["pending_result"] = result
    st.session_state["pending_parsed"] = parsed
    st.session_state["pending_doc_name"] = uploaded_file.name
    st.session_state["mode"] = mode
    return _try_persist_pending()


def _try_persist_pending() -> bool:
    """把 session_state 里暂存的校对结果落库；成功后清空暂存态、正式进入展示态。

    失败时保留 pending_* 不清空，只 st.error+记日志，不重新调用LLM——"落库失败
    导致已经花真实API额度算出来的结果被一起丢掉"是真实发生过的问题，这里把
    "算结果"和"存结果"两个失败域彻底分开，落库这步可以随便重试，不涉及LLM。
    """
    result = st.session_state.get("pending_result")
    parsed = st.session_state.get("pending_parsed")
    doc_name = st.session_state.get("pending_doc_name")
    mode = st.session_state.get("mode")

    try:
        record_id, issue_ids = workflow.persist_result(
            result,
            task_id=st.session_state["task_id"],
            doc_name=doc_name,
            parsed=parsed,
            mode=mode,
            author=st.session_state.get("author"),
        )
    except Exception as exc:  # noqa: BLE001 兜底：写库失败不能让已算出的结果跟着丢
        logger.exception("校对结果落库失败")
        st.error(f"校对已完成，但保存到数据库失败：{exc}。结果未丢失，可以重试保存。")
        return False

    st.session_state["classified_result"] = result
    st.session_state["record_id"] = record_id
    st.session_state["issue_ids"] = issue_ids
    st.session_state["issue_status"] = {
        issue_id: {"status": "待处理"} for issue_id in issue_ids
    }
    # 展示阶段（st.header 下的副标题）要用到文档名，pending_doc_name 落库后就清空了，
    # 单独留一份 result_doc_name 供结果页副标题显示。
    st.session_state["result_doc_name"] = doc_name
    st.session_state["pending_result"] = None
    st.session_state["pending_parsed"] = None
    st.session_state["pending_doc_name"] = None
    return True


def _render_standard_proofread():
    st.header("标准校对")

    # 用标准校对专属的 classified_result 当"是否初始化过"的哨兵，不能用 issue_status——
    # 后者是历史记录页/原稿比对页共用的 key（它们会 setdefault 建起来），先逛那两个页面
    # 再回标准校对时 issue_status 已存在，会把这里的初始化跳过、导致 classified_result
    # 等键缺失后续 KeyError。classified_result 只有标准校对自己会建，用它做哨兵才准确。
    if "classified_result" not in st.session_state:
        _reset_session_state()
        st.session_state["file_id"] = None

    # 模式选择放在上传控件之前、且不依赖是否已选文件——用户应该能在上传前就定好
    # 用哪种模式，而不是必须先选文件才看得到/能调整这个选项。
    mode = st.radio("校对模式", config.PROOFREAD_MODES, horizontal=True)

    uploaded_file = st.file_uploader("上传文件（PDF / Word）", type=["pdf", "docx"])

    # 注意：st.file_uploader 在页面切走再切回来后，浏览器出于安全限制无法恢复
    # 之前选中的文件，uploaded_file 会变回 None——这不代表用户想清空已校对结果，
    # 所以只有在 uploaded_file 真的拿到新文件时才判断是否换文件/清缓存；
    # uploaded_file 为 None 时直接往下走，看有没有已缓存的结果可以展示。
    if uploaded_file is not None:
        file_id = _file_id(uploaded_file)
        if file_id != st.session_state.get("file_id"):
            _reset_session_state()
            st.session_state["file_id"] = file_id

    if st.session_state["classified_result"] is None:
        if st.session_state.get("pending_result") is not None:
            # 校对已经跑完（花了真实API额度），但上次尝试保存到数据库失败——结果
            # 还在内存里，不需要重新校对，只需要重试保存这一步。
            st.warning(
                f"「{st.session_state.get('pending_doc_name')}」已完成校对，"
                "但保存到数据库失败，结果仍保留在内存中，未丢失。"
            )
            if st.button("重试保存"):
                if _try_persist_pending():
                    st.rerun()
            return
        if uploaded_file is None:
            return
        if st.button("开始校对"):
            if _execute_proofread(uploaded_file, mode):
                st.rerun()
        return

    result = st.session_state["classified_result"]
    record_id = st.session_state["record_id"]
    issue_ids = st.session_state["issue_ids"]

    _render_doc_subtitle(
        st.session_state.get("result_doc_name"), st.session_state.get("mode")
    )

    if st.button("重新校对本文件", key="rerun_proofread"):
        if uploaded_file is not None:
            # 文件对象还在（没离开过页面），直接原地重新执行一遍，不需要用户再点一次
            # "开始校对"、更不需要重新上传。
            if _execute_proofread(uploaded_file, mode):
                st.rerun()
        else:
            # uploaded_file 因切页丢失（浏览器安全限制，见上方说明），没有文件字节可用，
            # 没法直接重跑，只能清空缓存结果、退回等待重新上传的状态。
            st.warning("页面切换后文件已从浏览器丢失，请重新上传该文件后点击「开始校对」。")
            _reset_session_state()
            st.rerun()

    _render_stats(result.stats, result.warnings)

    st.divider()
    for layer in config.LAYERS:
        layer_issues = [
            (issue, issue_id)
            for issue, issue_id in zip(result.issues, issue_ids)
            if issue.layer == layer
        ]
        with st.expander(f"{layer}（{len(layer_issues)}条）", expanded=bool(layer_issues)):
            if not layer_issues:
                st.caption("无")
            _render_issue_cards(layer_issues, record_id)

    st.divider()
    if st.button("导出Excel"):
        try:
            export_path = exporter.export_issues_to_excel(record_id)
        except Exception as exc:  # noqa: BLE001 兜底，避免导出异常打崩页面
            logger.exception("Excel导出失败")
            st.error(f"导出失败：{exc}")
        else:
            st.success(f"已导出：{export_path}")
            st.download_button(
                "下载Excel文件",
                data=export_path.read_bytes(),
                file_name=export_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            _regenerate_rules_after_export()


def _row_to_issue_view(row: dict):
    """把 db.models.get_issues() 返回的原始字典行，包装成 _render_issue_card 期望的
    属性接口（.priority/.page_location/...），使问题卡渲染逻辑在实时校对流程、历史
    记录页、原稿比对结果三处之间原样复用，不需要各自另写一套卡片UI。

    layer_notes 是 ClassifiedIssue 独有字段（归层依据），只在标准校对当次运行时存在
    于内存里，issues 表没有对应列——从数据库现
    读出来渲染的场景（历史记录页、原稿比对结果，两者都是先落库再用 get_issues 读回
    来展示）都没有这份数据可展示，用一句说明文字占位，不是缺陷、也不假装有数据。
    """
    return types.SimpleNamespace(
        priority=row["priority"],
        page_location=row["page_location"],
        issue_type=row["issue_type"],
        original_text=row["original_text"],
        suggestion=row["suggestion"],
        layer=row["layer"],
        layer_notes=["（该字段仅在标准校对当次运行时于内存中可用，未持久化，此处无法展示）"],
    )


def _render_history_detail(record_id: int):
    """展示某条历史流程记录的完整问题卡列表，复用 _render_issue_card——
    与刚校对完时同样可以采纳/拒绝/写批注/追问，所有操作直接写库，与实时流程完全一致。
    """
    issues = get_issues(record_id)

    st.session_state.setdefault("issue_status", {})
    for row in issues:
        # 每次渲染都用DB当前值刷新，保证跟采纳/拒绝按钮点击后的最新状态一致
        st.session_state["issue_status"][row["issue_id"]] = {"status": row["status"]}
        note_key = f"note_{row['issue_id']}"
        if note_key not in st.session_state:
            # 只在这个issue_id第一次出现在本次会话时预填，避免覆盖用户正在编辑但还
            # 未提交（未触发on_change）的批注内容
            st.session_state[note_key] = row["note"] or ""

    for layer in config.LAYERS:
        layer_rows = [r for r in issues if r["layer"] == layer]
        with st.expander(f"{layer}（{len(layer_rows)}条）", expanded=bool(layer_rows)):
            if not layer_rows:
                st.caption("无")
            _render_issue_cards(
                [(_row_to_issue_view(row), row["issue_id"]) for row in layer_rows], record_id
            )

    st.divider()
    if st.button("导出Excel", key=f"history_export_{record_id}"):
        try:
            export_path = exporter.export_issues_to_excel(record_id)
        except Exception as exc:  # noqa: BLE001 兜底，避免导出异常打崩页面
            logger.exception("Excel导出失败")
            st.error(f"导出失败：{exc}")
        else:
            st.success(f"已导出：{export_path}")
            st.download_button(
                "下载Excel文件",
                data=export_path.read_bytes(),
                file_name=export_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"history_download_{record_id}",
            )


def _reset_compare_session_state():
    st.session_state["compare_record_id"] = None


def _render_document_comparison():
    st.header("原稿比对")

    if "compare_record_id" not in st.session_state:
        _reset_compare_session_state()
        st.session_state["compare_file_id"] = None

    col_orig, col_fmt = st.columns(2)
    original_file = col_orig.file_uploader("上传原稿（Word）", type=["docx"], key="compare_original_uploader")
    formatted_file = col_fmt.file_uploader(
        "上传排版稿（PDF / Word）", type=["pdf", "docx"], key="compare_formatted_uploader"
    )

    # 跟标准校对流同一个理由：st.file_uploader 页面切走再切回来会变回 None，不代表
    # 用户想清空已比对结果，只有真的拿到两份新文件时才判断是否换文件/清缓存。
    if original_file is not None and formatted_file is not None:
        file_id = hashlib.md5(original_file.getvalue() + formatted_file.getvalue()).hexdigest()
        if file_id != st.session_state.get("compare_file_id"):
            _reset_compare_session_state()
            st.session_state["compare_file_id"] = file_id

    if st.session_state["compare_record_id"] is None:
        if original_file is None or formatted_file is None:
            st.info("请分别上传原稿和排版稿。")
            return
        if st.button("开始比对"):
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            original_path = config.UPLOADS_DIR / f"{timestamp}_原稿_{original_file.name}"
            formatted_path = config.UPLOADS_DIR / f"{timestamp}_排版稿_{formatted_file.name}"
            original_path.write_bytes(original_file.getvalue())
            formatted_path.write_bytes(formatted_file.getvalue())
            _prune_uploads_dir(config.UPLOADS_DIR)

            try:
                with st.spinner("正在比对，请稍候…"):
                    diffs, formatted_parsed = workflow.run_document_comparison(
                        str(original_path), str(formatted_path)
                    )
            except (UnsupportedFormatError, NoTextLayerError) as exc:
                logger.warning("原稿比对文档解析失败: %s", exc)
                st.error(f"文档解析失败：{exc}")
                return
            except Exception as exc:  # noqa: BLE001 兜底，避免未预期异常打崩页面
                logger.exception("原稿比对过程中发生未预期错误")
                st.error(f"比对过程中发生未预期错误：{exc}")
                return

            # 比对结果不涉及逐块LLM调用（core.comparer 是纯本地diff计算），重算成本
            # 远低于标准校对，落库失败时不做 pending/重试保存那套状态机，只兜底提示，
            # 用户重新点一次"开始比对"即可，这是有意的范围取舍。
            try:
                record_id, _ = workflow.persist_comparison_result(
                    diffs,
                    task_id=st.session_state["task_id"],
                    doc_name=f"{original_file.name} / {formatted_file.name}",
                    formatted=formatted_parsed,
                    author=st.session_state.get("author"),
                )
            except Exception as exc:  # noqa: BLE001 兜底：写库失败不能崩页面
                logger.exception("原稿比对结果落库失败")
                st.error(f"比对已完成，但保存到数据库失败：{exc}，请重试。")
                return

            st.session_state["compare_record_id"] = record_id
            st.rerun()
        return

    record_id = st.session_state["compare_record_id"]
    rows = get_issues(record_id)

    st.caption(f"共发现 {len(rows)} 处实质性内容改动（排版调整不计入，已自动过滤）。")

    # 复用历史记录页同一套"issue_status刷新+批注预填"逻辑：issue_status是全局共享的
    # session_state字典，渲染前必须用DB当前值刷新，否则会显示成陈旧/错误的状态。
    st.session_state.setdefault("issue_status", {})
    for row in rows:
        st.session_state["issue_status"][row["issue_id"]] = {"status": row["status"]}
        note_key = f"note_{row['issue_id']}"
        if note_key not in st.session_state:
            st.session_state[note_key] = row["note"] or ""

    if not rows:
        st.success("未发现排版稿与原稿之间的实质性内容改动。")
    _render_issue_cards([(_row_to_issue_view(row), row["issue_id"]) for row in rows], record_id)

    st.divider()
    if st.button("导出Excel", key=f"compare_export_{record_id}"):
        try:
            export_path = exporter.export_issues_to_excel(record_id)
        except Exception as exc:  # noqa: BLE001 兜底，避免导出异常打崩页面
            logger.exception("Excel导出失败")
            st.error(f"导出失败：{exc}")
        else:
            st.success(f"已导出：{export_path}")
            st.download_button(
                "下载Excel文件",
                data=export_path.read_bytes(),
                file_name=export_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"compare_download_{record_id}",
            )
            _regenerate_rules_after_export()


def _render_history():
    st.header("历史记录")
    st.caption(f"只显示当前任务「{current_task['name']}」下的校对记录。")
    records = get_records(task_id=st.session_state["task_id"])
    if not records:
        st.info("当前任务下还没有校对记录。")
        return

    # record_id 不放进 column_order 即等同隐藏——下面 selectbox 是自己从 records 里
    # 按 record_id 取值，不依赖表格是否显示这一列；其余字段名换成中文表头，时间格式化
    # 成可读形式，不再直接暴露 doc_version/task_type 等数据库原始列名。
    st.dataframe(
        records,
        hide_index=True,
        column_order=[
            "created_at", "doc_name", "author", "task_type", "mode", "total_issues",
            "count_confirmed", "count_doubtful", "count_quotation", "count_optional",
        ],
        column_config={
            "created_at": st.column_config.DatetimeColumn("时间", format="MM-DD HH:mm"),
            "doc_name": st.column_config.TextColumn("文档名", width="large"),
            "author": st.column_config.TextColumn("完成人"),
            "task_type": st.column_config.TextColumn("类型"),
            "mode": st.column_config.TextColumn("模式"),
            "total_issues": st.column_config.NumberColumn("总数"),
            "count_confirmed": st.column_config.NumberColumn(config.LAYER_CONFIRMED),
            "count_doubtful": st.column_config.NumberColumn(config.LAYER_DOUBTFUL),
            "count_quotation": st.column_config.NumberColumn(config.LAYER_QUOTATION),
            "count_optional": st.column_config.NumberColumn(config.LAYER_OPTIONAL),
        },
    )

    options = {
        f"#{r['record_id']} · {r['doc_name']} · {r['created_at']}": r["record_id"] for r in records
    }
    selected_label = st.selectbox("选择一条记录查看详情", list(options.keys()))
    selected_record_id = options[selected_label]
    record = next(r for r in records if r["record_id"] == selected_record_id)

    _render_doc_subtitle(record.get("doc_name"), record.get("mode"))
    if record.get("author"):
        st.caption(f"完成人：{record['author']}")
    _render_move_record_to_task(record)
    _render_stats(record, [])
    st.divider()
    _render_history_detail(selected_record_id)


def _render_move_record_to_task(record: dict) -> None:
    """把当前选中的这条记录改归属到别的任务。

    迁移进来的历史记录全都堆在"历史归档"这一个任务下（迁移不按文档名猜任务归属），
    这里是把它们拆到真实任务里的唯一手段。改完这条记录就不再属于当前任务了，所以
    直接 rerun 让它从本页列表里消失，不做额外提示。

    已关闭的任务不出现在"移动到"选项里——已关闭在前端任何地方都不展示，不能把记录挪进
    一个前端根本看不到、也进不去的任务（见 _render_task_selection 顶部注释）。
    """
    others = [
        t for t in get_tasks()
        if t["task_id"] != record["task_id"] and t["status"] != config.TASK_STATUS_CLOSED
    ]
    if not others:
        return
    with st.expander("改归属任务"):
        labels = {f"{t['name']}（{t['status']}）": t["task_id"] for t in others}
        chosen = st.selectbox("移动到", list(labels.keys()), key=f"move_target_{record['record_id']}")
        if st.button("确认移动", key=f"move_record_{record['record_id']}"):
            update_record_task(record["record_id"], labels[chosen])
            st.rerun()


def _render_feedback_management():
    st.header("反馈学习")

    st.subheader("当前生效的反馈规则")
    st.caption("以下规则由历史拒绝记录经LLM语义总结得出，已注入校对提示词，AI校对时会主动规避这些模式。")
    rules = get_feedback_rules()
    if not rules:
        st.info("暂无总结出的规则。")
    else:
        for r in rules:
            st.write(f"- {r['rule_text']}")
    if st.button("重新生成规则"):
        try:
            feedback_rules.regenerate_rejection_rules()
        except Exception:
            logger.exception("反馈规则重新生成失败")
            st.error("规则重新生成失败，详见日志。")
        st.rerun()

    st.divider()
    st.subheader("原始反馈记录")
    rows = get_feedback()
    if not rows:
        st.info("暂无反馈学习记录。")
        return

    for entry in rows:
        with st.container(border=True):
            st.write(f"[{entry['issue_type']}] 原文：{entry['original_text']}")
            st.write(f"建议：{entry['suggestion']}")
            st.caption(f"AI说明：{entry['reason'] or '（无）'} · 记录时间：{entry['created_at']}")
            if st.button("撤销此条反馈", key=f"forget_feedback_{entry['feedback_id']}"):
                feedback.forget_feedback(entry["feedback_id"])
                st.rerun()


if page == "标准校对":
    _render_standard_proofread()
elif page == "原稿比对":
    _render_document_comparison()
elif page == "历史记录":
    _render_history()
else:
    _render_feedback_management()
