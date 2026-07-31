"""全局配置。

集中定义数据目录路径、分层常量、优先级常量、LLM API 相关配置占位。
"""

import os
from pathlib import Path

# ---------- 路径配置 ----------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
EXPORTS_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "app.db"
LOG_PATH = DATA_DIR / "app.log"

for _dir in (DATA_DIR, UPLOADS_DIR, EXPORTS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

# 上传文件落盘只是为了给 parse_document 一个路径读，用完即弃、不进 records 表，
# 不删会无限堆积。每次写入后只保留最近这么多个文件（按修改时间），多余的直接删除。
UPLOADS_RETENTION_COUNT = 10

# ---------- 任务常量 ----------
# 任务是校对记录的顶层容器（一个任务=一件持续的校对工作，如"期刊A"，下面挂多轮
# 校对记录）。状态只驱动两件事：任务列表默认只列激活的、列表里可按状态筛选；不做
# 自动推进（一个任务下会有多轮校对和多次导出，"导出即完成"这类推断一定会误判）。
TASK_STATUS_ACTIVE = "激活"
TASK_STATUS_RESOLVED = "解决"
TASK_STATUS_CLOSED = "关闭"

TASK_STATUSES = (TASK_STATUS_ACTIVE, TASK_STATUS_RESOLVED, TASK_STATUS_CLOSED)

# 引入任务概念之前就已存在的历史记录，迁移时统一归入这个名字的任务（见
# db/database.py::_migrate_backfill_legacy_task）。不按文档名自动聚类——"79期"和
# "82期"算一个任务还是两个只有使用者知道，迁移后在任务页手动改归属即可。
LEGACY_TASK_NAME = "历史归档"

# ---------- 结果分层常量 ----------
LAYER_CONFIRMED = "错误类"
LAYER_DOUBTFUL = "存疑类"
LAYER_QUOTATION = "引文类"
LAYER_OPTIONAL = "风格类"

LAYERS = (LAYER_CONFIRMED, LAYER_DOUBTFUL, LAYER_QUOTATION, LAYER_OPTIONAL)

# ---------- 优先级常量 ----------
PRIORITY_HIGH = "高"
PRIORITY_MEDIUM = "中"
PRIORITY_LOW = "低"
PRIORITY_OPTIONAL = "可选"

PRIORITIES = (PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW, PRIORITY_OPTIONAL)

# ---------- 校对模式配置 ----------
# 深度=现有全十类行为（默认，向后兼容一切不显式传mode的旧调用方）；精简=仅保留
# 机械性错别字检查(规则1)+语法结构(规则2)+两类高敏感度红线检查(规则9/10)，语义/
# 引用/事实类判断(3-8)对不需要出版级严格校对的普通文档误报率相对更高，非必需。
PROOFREAD_MODE_DEEP = "深度"
PROOFREAD_MODE_SIMPLIFIED = "精简"
PROOFREAD_MODES = (PROOFREAD_MODE_DEEP, PROOFREAD_MODE_SIMPLIFIED)  # UI单选顺序，深度在前=默认

SIMPLIFIED_RULE_NUMBERS = (1, 2, 9, 10)

# 精简模式下，规则1(错别字与拼写)内部"汉字冒充标点符号"这类零歧义的纯排版惯例问题
# （如数词"一"被当成破折号/连接号使用，形近但不是标点，读者理解完全不受影响），和"的/地/得"
# 这类真正可能改变语义/引起误解的错别字不是一回事，按风格可选处理。只用建议措辞命中下列
# 连接类标点关键词识别，命中才降级；宁可漏判也不误伤真正的错别字。
SIMPLIFIED_TYPO_PUNCTUATION_KEYWORDS = ("破折号", "连接号", "短横线", "分隔号", "间隔号")

# ---------- 校对并发配置（core/proofreader/使用）----------
# 原先不设并发上限（一次性提交全部块），实测账号RPM/TPM额度远够用；但 data/app.log
# 真实数据显示瓶颈不是账号限流，而是模型服务端实际并发处理能力——29个块一次性
# 全发时，先完成的也要170~230s（对比低并发下的47~66s），后完成的拖到300~570s甚至
# 撞上超时上限集体失败，失败后的整批重试还会把同样的拥堵原样重演一次。改成分批、
# 每批最多这么多个块并发，避免请求数超过服务端实际承载能力后排队时间反超收益。
# 具体数值凭日志间接推断（不是精确压测得出），后续如有更多实测数据可调整。
PROOFREAD_MAX_CONCURRENT_CHUNKS = 8

# ---------- LLM API 配置（core/llm_client.py使用）----------
# 默认走 DashScope 兼容模式公开固定地址；仍支持 LLM_BASE_URL 环境变量覆盖（换服务商时不用改代码）。
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
# 实际使用的密钥环境变量是 DASHSCOPE_API_KEY（阿里云DashScope标准命名），这个没有默认值，必须设置。
LLM_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")

LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-pro")
# 不设默认值：留空(None)时 chat_completion 固定用 LLM_TIMEOUT_FIXED_SECONDS；一旦设置该
# 环境变量，视为显式指定超时，覆盖固定值。
LLM_TIMEOUT = int(os.environ["LLM_TIMEOUT"]) if os.environ.get("LLM_TIMEOUT") else None
# 曾按文本长度动态估算超时，系数来自早期实测（qwen3.6-plus，300秒超时下，3900~5600字
# 总输入实际耗时178~212秒）；但 data/app.log 真实数据显示当前实际使用的模型（deepseek-v3.2）
# 耗时经常逼近/超过按这组系数算出的估算值（约5400字输入估算~280s，真实成功耗时集中在
# 210~274s，大量请求在280s边界被提前判超时、触发本可避免的重试），说明这组系数对当前
# 模型偏紧。改为固定值，不再随输入长度浮动。
LLM_TIMEOUT_FIXED_SECONDS = 900
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3"))
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0"))

# ---------- 文档解析配置（core/parser/使用）----------
# OCR 推理设备：'auto' 运行时自动检测（有可用GPU则用GPU，否则CPU）/'gpu'/'cpu'。
# GPU 上推理比 CPU 快一到两个数量级，且不走 CPU 那条有已知算子兼容问题的
# MKL-DNN+PIR 路径（见 core/parser.py 里对设备与 MKL-DNN 的处理）。
PADDLE_DEVICE = "auto"

# PDF 页面渲染为图片供 OCR 使用的分辨率（dpi）。分辨率越高OCR质量越好但越慢，
# GPU 下 250dpi 也很快；纯 CPU 环境如嫌慢可下调到 150。
OCR_RENDER_DPI = 250

# 一页可提取字符数低于该阈值，视为无文字层（需走OCR）
TEXT_LAYER_MIN_CHARS = 20

# B类（OCR扫描PDF）分栏判断（`core/parser/_common.py`）——只判"分不分栏"加一条主分栏线。
# A类原生PDF不用这批常量，走下面的 COLUMN_A_* 一组（`core/parser/_columns.py`），两套
# 阈值不通用，原因见 core/parser/CLAUDE.md"分栏检测"一节。
# 在页面宽度这个比例区间内寻找空白分栏带
COLUMN_GAP_BAND = (0.4, 0.6)
# 分栏空白带最小宽度占页面宽度的比例，低于此值不视为有效分栏（排除噪声）。
# 真正的印刷双栏栏间距通常有明显宽度；单栏文档里偶尔出现的、由列表缩进等
# 造成的窄"伪分栏线"往往达不到这个宽度。
COLUMN_MIN_GAP_WIDTH_RATIO = 0.05
# 判定双栏还需候选分栏线两侧的文本字符数都达到总字符数的这个比例以上
# （真正的双栏内容会大致对半分布；一两个孤立块造成的伪分栏线两侧字符数
# 会严重失衡，据此过滤误判。不按块宽度筛"窄块"——同一版面解析模型对单栏/
# 双栏文本的分块粒度本身就不稳定，窄块在两种情况下都很常见）
COLUMN_BALANCE_MIN_RATIO = 0.25
# 宽度超过"所在区域宽度×该比例"的块视为通栏块，在画栏间空白带的覆盖图时不计入。
# 通栏块（整页刊头、通栏标题、横跨整幅的表格）会把中间那条空白带整条盖住，只要
# 有一个就足以让明显的分栏版面被判成单栏。真实案例：某期刊每页顶部一条通栏刊头，
# 使左半表格与右半正文被判成单栏后按y坐标交错输出，正文被切成"是最根本的成"+
# "页码"这类断句，LLM据此报出大量并不存在的错别字/漏字问题。
COLUMN_SPANNING_BLOCK_WIDTH_RATIO = 0.7
# 剔除通栏块后，剩余块的字符数要占该区域总字符数这个比例以上，才继续判分栏。
# 单栏页每行本身就接近整幅宽度、会被整体当成通栏块剔除，剩下的只有段末短行；
# 没有这道闸门，剔除后近乎空白的覆盖图会让任何单栏页都"找得到"空白带。
COLUMN_NON_SPANNING_MIN_CHAR_RATIO = 0.5

# ---------------------------------------------------------------------------
# A类分栏检测（core/parser/_columns.py，分层占用率剖面）专用的一组常量。
#
# 与上面那批 COLUMN_* 的区别：上面那批是"二值覆盖图 + 字符数统计"那套判据，现在只有
# B类OCR通道（_common.py::_detect_column_split）还在用；下面这批 COLUMN_A_* 是A类专用，
# 全部只看几何量。算法为什么长这样见 core/parser/_columns.py 顶部 docstring。
#
# 全部按"占页宽的比例"而非绝对pt定义，A类的PDF点与B类的渲染像素通用（虽然目前只接A类）。
# 工作点：134页目视核定GT命中129、相对旧算法收益41、回归0，另221页单栏零误判。
# 每个值两侧的余量见 tools/column_probe_data.md 第七节，那里也有复算命令。
# ---------------------------------------------------------------------------
# x方向分箱宽度占页宽的比例。下界由"真栏间距最窄12pt要落到≥6个分箱"定；实测
# 0.001~0.003 命中数完全相同，0.004 掉2页、0.008 彻底崩掉，取中间的0.002。
COLUMN_A_PROFILE_BIN_RATIO = 0.002
# 一个分箱的占用率（被文字覆盖的y长度÷内容总高度）低于此值才算空白。
# **两侧是贴着的、不存在干净可分的阈值**：真栏间距最难的一页要求 ≥0.075，而整页大表格
# 的列间距在 0.08 就冒头被当成栏间距（正文被切碎，比漏检严重得多），两侧只差 0.005。
# 0.06 是收益曲线的膝点（0.05→命中126、0.06→129、0.075 仍129、0.08 起回归），
# **不是"两组分布的中点"，不要以为可以随手往上放**。
COLUMN_A_GAP_MAX_OCCUPANCY_RATIO = 0.06
# 整页主缝（跨页对开刊物即装订缝）的最小宽度占页宽比例。0.02~0.05 命中数相同，
# 0.10 起回归，取靠上的0.05多挡一层噪声。
COLUMN_A_MAIN_GAP_MIN_WIDTH_RATIO = 0.05
# 半区内子缝的最小宽度占页宽比例。四栏版面的子缝比主缝窄得多，必须单列一个更低的值。
# 0.002~0.005 命中数相同，0.015 起回归。
COLUMN_A_SUB_GAP_MIN_WIDTH_RATIO = 0.005
# 单栏最小宽度占页宽的比例，防止把页边一条窄空白当成栏间距。真栏宽下界实测
# 四栏页 200/1009≈19.8%、某刊 194/1208≈16%；0.05~0.12 命中数相同，0.13 起回归。
COLUMN_A_MIN_WIDTH_RATIO = 0.10
# 宽度超过"本层内容范围宽度×该比例"的块视为该层的通栏块，算占用率剖面时不计入。
# 注意是**本层的内容范围宽度**，不是页宽、也不是几何区域宽（含页边距）——这是分层
# 结构存在的全部理由，见 _columns.py docstring。
# **这是这批常量里最脆的一个：安全窗只有 0.58~0.60，两侧都会坏**（0.55 起回归、
# 0.62 掉3页）。它与 τ 的上界由同一页（唯一的整页大表格样本）决定，过拟合风险最高；
# 换出版社版面出问题时，保守退路是这里放0.7 + τ降到0.05（代价约7页漏检，离悬崖很远）。
# 不复用 COLUMN_SPANNING_BLOCK_WIDTH_RATIO(0.7)：那个被B类的_split_region共用，改它
# 会连带改变B类行为。
COLUMN_A_SPANNING_WIDTH_RATIO = 0.6

# A类：PyMuPDF把block内多行文本拼接时，判断相邻两个"line"是否其实是PyMuPDF自己误拆的
# 同一视觉行（如项目符号用符号字体+和正文间有较大缩进间隙时，PyMuPDF行聚类偶尔会按字体/
# 间隙把同一行拆成两个line对象）——y轴重叠长度占较小那个line自身高度的比例达到此阈值，
# 判定为同一行，拼接时用空格而非换行符（避免"项目符号应换行"这类伪问题，见
# core/parser/CLAUDE.md）。真实数据验证过：真正被误拆的同一行重叠比例是100%，真正
# 不同行的重叠比例是0，阈值取中间值留出充分余量，不会误伤真正的换行。
NATIVE_SAME_ROW_OVERLAP_MIN_RATIO = 0.5
# 同上，但这道是横向闸门：两个line的水平间隙不能超过较小那个line自身高度的这个倍数
# （行高≈字号≈一个汉字的宽度，所以这个倍数约等于"隔了几个字"）。只看y轴重叠不够——
# 跨页对开版面里，左页和右页同一水平线上的两块文字纵向可以100%重叠而横向相距半个
# 页面，PyMuPDF会把它们聚成一个block，只按y轴判定会把它们空格拼成一句串文。真实
# 数据：项目符号误拆场景间隙只有1.2倍行高，跨页对开误聚场景是61倍，取3倍两边都留
# 出充分余量（见 core/parser/CLAUDE.md"跨页对开版面"一节）。
NATIVE_SAME_ROW_MAX_GAP_HEIGHT_RATIO = 3.0
# 同上，判定为同一视觉行、且两段字符按坐标排序后真的交错时，要按字符级重新拼接（见
# core/parser/_glyphs.py）。拼接时丢掉"字框被相邻字符盖住"的排版填充空格——与任一侧
# 邻字的横向重叠超过自身宽度的这个比例就算填充。真实数据（`1.《数据…》` 里 `.` 与
# `《` 之间那个空格）重叠比例是100%，真正的词间隔空格与左右邻字重叠为0，取0.5两边都
# 留出充分余量。
NATIVE_FILLER_SPACE_MIN_COVER_RATIO = 0.5
# A类：排版字距微调（tracking）被导出成真空格字符时（`APS`变成`A PS`、`BOM`变成`B O M`，
# 三份期刊里100多处），靠"空格宽度 / 同一span内非空格字符间隙的中位数"识别——字距是整段
# 均匀施加的，假空格宽度恰好等于字母间隙，真词间空格远宽于字母间隙。全量实测：假空格
# 0.94~1.40，真词间空格 ≥4.0（紧排文本里字母间隙≈0，比值上百），中间2~3一个样本都没有，
# 取2.0两侧余量0.6/2.0。**不要改回"按空格宽/字号"或"按该字体众数空格宽"归一化**，那两条
# 都在真实数据上证伪过（两端对齐会压缩真词间空格，与假空格完全交叠），理由见
# core/parser/_glyphs.py::_drop_tracking_spaces。
NATIVE_TRACKING_SPACE_MAX_GAP_RATIO = 2.0
# 同上：一个span内非空格字符对少于这个数时不做上述判定——中位数样本量不足不可靠，宁可
# 漏修也不误删。真实命中场景的样本量普遍在14~20对，3只是挡住"整段就一两个字符"的极端情况。
NATIVE_TRACKING_SPACE_MIN_GAP_SAMPLES = 3

# A类：判断一次换行到底是"排版被页宽顶到头才换的行"（下一行是同一句话的延续，中文里
# 两行之间不该留任何分隔）还是"内容说完了主动换的行"（标题、列表项、段落结束，必须
# 留分隔否则会被读成一句病句）。判据一：这一行的右边界离本栏最右还剩不到多少个字的
# 宽度——剩不下一个字就说明是被宽度顶回来的，用字号近似一个汉字的宽度。
# 起因见 core/parser/CLAUDE.md"被栏宽顶回来的续写块并回上一块"一节：不区分这两种
# 换行、一律插换行符，会把"是最根本的成/功要素。"这种词中断行原样喂给LLM，被当成
# 多余空格/漏字报出来。
NATIVE_WRAP_RIGHT_EDGE_TOLERANCE_CHARS = 1.0
# 判据二：续写行还必须纵向紧邻，两行间距不超过上一行自身高度的这个倍数。真实数据
# （某期刊四栏正文）：相邻正文行间距约为行高的0.94倍，标题与正文之间1.27倍，段落
# 之间更大——取1.5既容得下正常行距波动，又挡得住跨段/跨版块的"假续写"。
NATIVE_WRAP_MAX_LINE_GAP_RATIO = 1.5
# 判据三：两块的平均字号相差不超过这个pt数，才可能是同一段被栏宽切开的。真实数据
# （预览版四栏正文8.0 vs 小标题9.5、大标题14+）：同段两块的字号差实测全是0.0（同一段
# 本来就是同一种字号，差异只来自块内混排的英文/数字），取0.6留出混排波动的余量，又
# 挡得住最小的标题-正文落差1.5。
NATIVE_WRAP_SAME_SIZE_TOLERANCE = 0.6
# 判据四：上一块的最后一行宽度要达到这个字数（用字号近似一个汉字宽度），才算"满行"。
# 判据一只问"右边界够不够靠右"，一个缩在栏中间、既短又恰好右对齐的行也能蒙混过关——
# 目录页码块 `2`、竖排刊名 `二/O/二/六` 就是这么被误判成续写行的。真实数据：被栏宽顶
# 回来的正文满行普遍在18~24个字宽，上述误判样本全在3个字宽以内，取8两侧余量都很大。
NATIVE_WRAP_MIN_FULL_LINE_CHARS = 8.0

# B类（OCR）跨页检测：宽高比超过该阈值才可能是两页拼接的横版扫描
SPREAD_ASPECT_RATIO_THRESHOLD = 1.6
# 跨页检测：水平中线附近空白带宽度占页面宽度的比例超过该阈值，才判定为两页拼接
# （双栏排版的正常栏间距远小于此值，避免误伤双栏单页）
SPREAD_GAP_WIDTH_RATIO = 0.12

# OCR 单块结果过滤：置信度低于该阈值且文本长度低于 OCR_MIN_BLOCK_CHARS 才丢弃
OCR_MIN_BLOCK_CONFIDENCE = 0.6
OCR_MIN_BLOCK_CHARS = 4
# 整页平均置信度低于该阈值时记录 warning
OCR_PAGE_LOW_CONFIDENCE_THRESHOLD = 0.7

# 页眉页脚判定区域：页面顶部/底部该比例范围内的重复短文本视为页眉页脚
HEADER_FOOTER_ZONE_RATIO = 0.08

# PaddleOCR 识别语言
PADDLEOCR_LANG = "ch"

# OCR 文字检测/识别模型选择（曾尝试用于降低低画质截图的字符误识别率，见 core/parser/CLAUDE.md）。
# 保留为 None：用真实文档（系统管理员.pdf 页1）做过 PP-OCRv6_medium（库默认）vs
# 上一代最大档 PP-OCRv5_server 的同页真实A/B，结果 v5_server 没有更准——"个人信息"
# 字段两者战平（4次里都是3对1错），"我的收藏夹"字段 v5_server 明显更差（1对3错 vs
# 默认的2对2错）。没有证据支持切换，保留可调但不生效，别在没有新证据前重新默认成v5_server。
PADDLEOCR_DET_MODEL = None   # 备选 "PP-OCRv5_server_det"（本机已缓存），需先补更大样本A/B再启用
PADDLEOCR_REC_MODEL = None   # 备选 "PP-OCRv5_server_rec"，理由同上

# ---------- 长文档分块配置（core/chunker/使用）----------
# 目标块大小（字符数）：贪心装填时，加入下一个 block 会超过该值就收束当前块。
# 中文场景下字符数与 token 数近似线性，不引入 tokenizer 依赖。
CHUNK_SIZE_TARGET = 3000
# 硬上限（字符数）：单个 block 自身超过该值才触发"超长block切分"例外。
# 与 CHUNK_SIZE_TARGET 之间留出的余量用于吸收重叠区文本带来的额外长度，
# 使得正常情况下（重叠区不过大）分块最终字符数仍不超过此硬上限。
CHUNK_SIZE_MAX = 4500
# 每个块（第一块除外）携带的前向重叠 block 数，供LLM理解上下文
OVERLAP_BLOCKS = 2

# 分块文本中“重叠区”与“正文”的分隔标记。core/proofreader/ 的校对提示词会引用同一常量，
# 提醒模型重叠区问题不要重复报告。
CHUNK_OVERLAP_MARK = "【上文回顾，仅供理解上下文，此部分的问题不要报告】"
CHUNK_BODY_MARK = "【正文开始，请校对以下内容】"

# ---------- 结果分层配置（core/classifier/使用）----------
# 规则C：block的OCR置信度低于该阈值时，"错别字/标点"类问题降级（很可能是OCR认错字而非原文真错，
# 如 AI→A1、形近字误判）。初值是拍脑袋定的，等真实文档跑起来后按"被降级条目里真OCR错/真原文错"
# 的实际比例调整——调阈值只改这里，不用碰 classifier.py。
OCR_CONF_DOWNGRADE_THRESHOLD = 0.90

# 规则A：引文识别启发式。成对引号内文本达到该字数才视为"引文级"引用（更短的更可能是强调用法，
# 不是整段引用）。
QUOTATION_QUOTE_MIN_CHARS = 10
# 文言文特征：虚词密度达到阈值、且虚词出现次数达到最小计数，两个条件同时满足才判定（双重条件是
# 为了避免"总之""也是"这类现代汉语常见词中单个虚词孤例造成误判）。
QUOTATION_CLASSICAL_PARTICLES = "之乎者也矣焉哉"
QUOTATION_CLASSICAL_DENSITY_THRESHOLD = 0.06
QUOTATION_CLASSICAL_MIN_COUNT = 2

# 规则B：事实性内容启发式关键词——命中人名职务/机构名后缀或年份格式，且建议非空、与原文不同
# （即"改写型"建议），用于兜底LLM未自报category='factual'、issue_type也不是"常识与事实性错误"
# 的漏报场景。宁可漏判，不引入NER等新依赖。
FACTUAL_TITLE_KEYWORDS = ("部长", "主任", "总裁", "董事长", "教授", "院士", "书记", "市长",
                           "省长", "主席", "总经理", "校长", "局长", "厅长", "秘书长", "会长", "理事长")
FACTUAL_ORG_SUFFIXES = ("公司", "集团", "协会", "委员会", "大学", "医院", "研究院", "研究所")

# 规则E：风格类关键词，命中suggestion/reason中任一词即视为风格建议。
STYLE_KEYWORDS = ("更通顺", "更简洁", "建议润色")

# 优先级覆盖：这两类issue_type无论落在哪一层，priority强制为'高'（对应校对规则文件
# "优先标注、单独提醒"的要求），直接引用 proofread_rules.md 里的类目原文，不是新定义的名称。
HIGH_PRIORITY_ISSUE_TYPES = ("政治敏感性表述", "民族与地名规范")

# 规则G：知识时效性误判豁免。
# LLM可能仅因为某个年份/日期超出了它的训练数据覆盖范围就怀疑"这看起来太新/我没见过"，
# 但这种怀疑只针对"这个时间点本身是否存在"，不针对"该时间点发生的事是否属实"——是知识
# 时效性局限造成的误判，不是真正的事实性错误。命中下面关键词+年份在容忍窗口内时，把该
# issue强制降级为存疑待核实+最低优先级（不直接剔除：保留可审计性，用户在界面上一眼就能
# 判断要不要忽略；如果关键词误判了真正的事实性错误，信息不会凭空消失）。
LLM_KNOWLEDGE_CUTOFF_YEAR = 2026  # 拍脑袋定的初值，需要按实际配置的模型（LLM_MODEL）真实知识截止时间校正
KNOWLEDGE_CUTOFF_GRACE_YEARS = 2  # 文中年份超过"截止年份+此宽限期"还被怀疑"太新"，才视为真的离谱，不豁免
RECENCY_DOUBT_KEYWORDS = (
    "训练数据", "知识截止", "知识范围", "我的认知", "未收录", "无法查证是否存在",
    "较新", "尚未听说", "无法确认该时间点", "超出我的知识",
)

# 规则J：LLM在reason/suggestion里自己描述"这是版式错乱/跨行错位/乱码"时的兜底降级
# 关键词——真实数据（data/app.db 35号记录）里出现过的原话摘出，命中任一即认为
# LLM自己都没看懂被打乱的版面，不是"一眼就能看出"的确定性语言错误。
LAYOUT_ARTIFACT_KEYWORDS = ("排版错乱", "跨行", "错行", "错位", "乱码", "段落顺序", "图片位置")

# LLM自陈"这条本来就不该报"时的措辞——提示词"补充规则三：历史反馈规避"要求命中规避
# 规则的内容直接不要输出，但真实数据里LLM会照样输出一条issue、把"我为什么不该报它"
# 写进suggestion/reason（如"根据历史反馈规避规则第三条，此类因换行导致的词语拆分不应
# 报告为问题"）。命中即整条丢弃：LLM自己都判定这不是问题，没有人工核实价值。
# 措辞限定在"报告/作为问题"和"历史反馈规避"这两个模式上，不收"无需修改""不建议改动"
# 这类宽泛说法——引文类的固定suggestion就是"原文照录，不建议改动"，收进来会误伤整层。
NON_ISSUE_SELF_DECLARATION_KEYWORDS = (
    "不应报告为问题", "不报告为问题", "不作为问题报告", "不应作为问题",
    "不视为问题", "不属于问题", "历史反馈规避",
)

# ---------- 原稿比对配置（core/comparer.py使用）----------
# 句子级diff的切分标点：按这几个句末标点把段落切成句子列表再逐句比较，标点保留在
# 前一句末尾（core/comparer.py::_split_sentences 用零宽断言切分，不消耗字符）。
COMPARE_SENTENCE_SPLIT_PUNCTUATION = "。！？；"
# replace区间内两段相似度（difflib.SequenceMatcher.ratio()）低于此值，判定不是同一段
# 的改写，分别标记为删除+插入，不再往下做句子级diff。
COMPARE_PARAGRAPH_MATCH_MIN_RATIO = 0.5

DIFF_LAYER_SUBSTANTIVE = "实质性改动"
DIFF_LAYER_FORMATTING = "排版调整"  # 当前实现不主动产出（归一化后完全相同的差异直接跳过），保留用于表结构完整性

# ---------- 追问上下文配置（core/followup.py使用）----------
# 追问时携带的原文上下文窗口：取issue所在block前后各N个block拼接，控制token成本（T5）。
FOLLOWUP_CONTEXT_WINDOW_BLOCKS = 2
# 同一条issue被多次追问时，只把最近N轮问答拼进prompt注入历史（更早的仍完整存库，只是不再喂给LLM）。
FOLLOWUP_MAX_HISTORY_TURNS = 5

# ---------- 用户反馈学习配置（见core/feedback_rules.py）----------
# 历史拒绝记录整批交给LLM做语义总结，只有真正同一类原因被拒绝的才归并成一条规则，
# 注入校对提示词，让LLM在生成建议这一步就主动规避（不用字符串相似度比对——同一句式
# 模板如"改为『A』或『B』"会让语义无关的建议被误判为同类，见core/feedback_rules.py
# 模块docstring）。
#
# 总结出的每条规则必须有至少这么多条历史反馈佐证才成立——既写进
# prompt/feedback_rules_system.md 提示LLM，也在 core/feedback_rules.py 里对LLM输出的
# matched_feedback_ids数量做代码层校验（不完全信任LLM自报"是否够3次"）。太敏感/太迟钝
# 都只改这里。
FEEDBACK_REJECTION_THRESHOLD = 3

# 事实类issue_type在总结规则前直接从输入里过滤掉，永远不参与总结——项目设计铁律"涉及
# 人名/职务/历史事实的修改建议禁止确定性结论，必须保留人工复核机会"的延伸，见
# core/feedback_rules.py::regenerate_rejection_rules。
FEEDBACK_EXEMPT_ISSUE_TYPES = ("常识与事实性错误",)
