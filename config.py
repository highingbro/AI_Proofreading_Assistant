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

for _dir in (DATA_DIR, UPLOADS_DIR, EXPORTS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

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

# ---------- 校对模式配置（补丁：模式选择功能新增）----------
# 深度=现有全十类行为（默认，向后兼容一切不显式传mode的旧调用方）；精简=仅保留
# 机械性错别字检查(规则1)+语法结构(规则2)+两类高敏感度红线检查(规则9/10)，语义/
# 引用/事实类判断(3-8)对不需要出版级严格校对的普通文档误报率相对更高，非必需。
PROOFREAD_MODE_DEEP = "深度"
PROOFREAD_MODE_SIMPLIFIED = "精简"
PROOFREAD_MODES = (PROOFREAD_MODE_DEEP, PROOFREAD_MODE_SIMPLIFIED)  # UI单选顺序，深度在前=默认

SIMPLIFIED_RULE_NUMBERS = (1, 2, 9, 10)

# 精简模式补丁：规则1(错别字与拼写)内部"汉字冒充标点符号"这类零歧义的纯排版惯例问题
# （如数词"一"被当成破折号/连接号使用，形近但不是标点，读者理解完全不受影响），和"的/地/得"
# 这类真正可能改变语义/引起误解的错别字不是一回事，精简模式下按风格可选处理。只用建议措辞
# 命中下列连接类标点关键词识别，命中才降级；宁可漏判也不误伤真正的错别字。
SIMPLIFIED_TYPO_PUNCTUATION_KEYWORDS = ("破折号", "连接号", "短横线", "分隔号", "间隔号")

# ---------- LLM API 配置（阶段4使用）----------
# 默认走 DashScope 兼容模式公开固定地址；仍支持 LLM_BASE_URL 环境变量覆盖（换服务商时不用改代码）。
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
# 实际使用的密钥环境变量是 DASHSCOPE_API_KEY（阿里云DashScope标准命名），这个没有默认值，必须设置。
LLM_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
# 模型直接指定 qwen3.6-plus，不强制要求环境变量；仍支持 LLM_MODEL 环境变量覆盖。
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3.5-flash-2026-02-23")
# 不设默认值：留空(None)时 chat_completion 按文本长度动态估算超时（见下方 LLM_TIMEOUT_* 四项）；
# 一旦设置该环境变量，视为显式指定固定超时，不再动态估算。
LLM_TIMEOUT = int(os.environ["LLM_TIMEOUT"]) if os.environ.get("LLM_TIMEOUT") else None
# 动态超时估算参数：timeout = 基础值 + 总字符数(system_prompt+user_content) × 每字符系数，限定在[MIN, MAX]区间。
# 系数来自阶段4实测（qwen3.6-plus，300秒超时下，3900~5600字总输入实际耗时178~212秒），
# 同规模输入耗时波动可达20秒以上（推测与实际问题条数/输出长度有关，不只取决于输入长度），
# 系数刻意取宽松，避免把仍在正常处理的请求判定超时、触发不必要的重试。
LLM_TIMEOUT_BASE_SECONDS = 90
LLM_TIMEOUT_PER_CHAR_SECONDS = 0.035
LLM_TIMEOUT_MIN_SECONDS = 120
LLM_TIMEOUT_MAX_SECONDS = 600
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3"))
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0"))

# ---------- 文档解析配置（阶段2使用）----------
# OCR 推理设备：'auto' 运行时自动检测（有可用GPU则用GPU，否则CPU）/'gpu'/'cpu'。
# GPU 上推理比 CPU 快一到两个数量级，且不走 CPU 那条有已知算子兼容问题的
# MKL-DNN+PIR 路径（见 core/parser.py 里对设备与 MKL-DNN 的处理）。
PADDLE_DEVICE = "auto"

# PDF 页面渲染为图片供 OCR 使用的分辨率（dpi）。分辨率越高OCR质量越好但越慢，
# GPU 下 250dpi 也很快；纯 CPU 环境如嫌慢可下调到 150。
OCR_RENDER_DPI = 250

# 一页可提取字符数低于该阈值，视为无文字层（需走OCR）
TEXT_LAYER_MIN_CHARS = 20

# A类（原生文字层PDF）分栏判断：在页面宽度这个比例区间内寻找空白分栏带
COLUMN_GAP_BAND = (0.4, 0.6)
# 分栏空白带最小宽度占页面宽度的比例，低于此值不视为有效分栏（排除噪声）。
# 真正的印刷双栏栏间距通常有明显宽度；单栏文档里偶尔出现的、由列表缩进等
# 造成的窄"伪分栏线"往往达不到这个宽度。
COLUMN_MIN_GAP_WIDTH_RATIO = 0.05
# 判定双栏还需候选分栏线两侧的文本字符数都达到总字符数的这个比例以上
# （真正的双栏内容会大致对半分布；一两个孤立块造成的伪分栏线两侧字符数
# 会严重失衡，据此过滤误判。按块宽度过滤"宽块"并不可靠——同一版面解析
# 模型对单栏/双栏文本的分块粒度本身就不稳定，窄块在两种情况下都很常见）
COLUMN_BALANCE_MIN_RATIO = 0.25

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

# ---------- 长文档分块配置（阶段3使用）----------
# 目标块大小（字符数）：贪心装填时，加入下一个 block 会超过该值就收束当前块。
# 中文场景下字符数与 token 数近似线性，不引入 tokenizer 依赖。
CHUNK_SIZE_TARGET = 3000
# 硬上限（字符数）：单个 block 自身超过该值才触发"超长block切分"例外。
# 与 CHUNK_SIZE_TARGET 之间留出的余量用于吸收重叠区文本带来的额外长度，
# 使得正常情况下（重叠区不过大）分块最终字符数仍不超过此硬上限。
CHUNK_SIZE_MAX = 4500
# 每个块（第一块除外）携带的前向重叠 block 数，供LLM理解上下文
OVERLAP_BLOCKS = 2

# 分块文本中“重叠区”与“正文”的分隔标记。阶段4的校对提示词会引用同一常量，
# 提醒模型重叠区问题不要重复报告。
CHUNK_OVERLAP_MARK = "【上文回顾，仅供理解上下文，此部分的问题不要报告】"
CHUNK_BODY_MARK = "【正文开始，请校对以下内容】"

# ---------- 全局术语表配置（补丁：解决chunk间互不可见导致的一致性误判）----------
# 校对前先统计文档里反复出现的候选词条（人名/机构名/专有术语候选），只把候选词条列表
# （不是全文）交给一次LLM调用做语义分类+同名异写检测，产出"全局术语表"注入每个chunk的
# 校对提示词，解决同一实体在不同chunk写法不一致（如"张三"/"张叁"）完全检测不到的问题。
# 详见 core/glossary.py 模块docstring。
GLOSSARY_NGRAM_MIN_LEN = 2
GLOSSARY_NGRAM_MAX_LEN = 6
# 候选词条最少重复出现次数，低于此值大概率是偶然片段而非有意义的实体/术语
GLOSSARY_MIN_FREQUENCY = 3
# 送去给LLM分类的候选词条上限（按频次降序截断），控制该次LLM调用的输入体量，
# 不随文档长度线性增长
GLOSSARY_CANDIDATE_TOP_K = 150

# ---------- 结果分层配置（阶段5使用）----------
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

# 规则G（补丁，真实使用中发现后追加，非stage5原始设计）：知识时效性误判豁免。
# LLM可能仅因为某个年份/日期超出了它的训练数据覆盖范围就怀疑"这看起来太新/我没见过"，
# 但这种怀疑只针对"这个时间点本身是否存在"，不针对"该时间点发生的事是否属实"——是知识
# 时效性局限造成的误判，不是真正的事实性错误。命中下面关键词+年份在容忍窗口内时，把该
# issue强制降级为存疑待核实+最低优先级（不直接剔除：保留可审计性，用户在界面上一眼就能
# 判断要不要忽略；如果关键词误判了真正的事实性错误，信息不会凭空消失）。
LLM_KNOWLEDGE_CUTOFF_YEAR = 2025  # 拍脑袋定的初值，需要按实际配置的模型（当前默认qwen3.6-plus）真实知识截止时间校正
KNOWLEDGE_CUTOFF_GRACE_YEARS = 10  # 文中年份超过"截止年份+此宽限期"还被怀疑"太新"，才视为真的离谱，不豁免
RECENCY_DOUBT_KEYWORDS = (
    "训练数据", "知识截止", "知识范围", "我的认知", "未收录", "无法查证是否存在",
    "较新", "尚未听说", "无法确认该时间点", "超出我的知识",
)

# ---------- 原稿比对配置（阶段9使用）----------
# 句子级diff的切分标点：按这几个句末标点把段落切成句子列表再逐句比较，标点保留在
# 前一句末尾（core/comparer.py::_split_sentences 用零宽断言切分，不消耗字符）。
COMPARE_SENTENCE_SPLIT_PUNCTUATION = "。！？；"
# replace区间内两段相似度（difflib.SequenceMatcher.ratio()）低于此值，判定不是同一段
# 的改写，分别标记为删除+插入，不再往下做句子级diff。
COMPARE_PARAGRAPH_MATCH_MIN_RATIO = 0.5

DIFF_LAYER_SUBSTANTIVE = "实质性改动"
DIFF_LAYER_FORMATTING = "排版调整"  # 当前实现不主动产出（归一化后完全相同的差异直接跳过），保留用于表结构完整性

# ---------- 追问上下文配置（阶段7使用）----------
# 追问时携带的原文上下文窗口：取issue所在block前后各N个block拼接，控制token成本（T5）。
FOLLOWUP_CONTEXT_WINDOW_BLOCKS = 2
# 同一条issue被多次追问时，只把最近N轮问答拼进prompt注入历史（更早的仍完整存库，只是不再喂给LLM）。
FOLLOWUP_MAX_HISTORY_TURNS = 5

# ---------- 用户反馈学习配置（阶段12使用）----------
# 同一类问题（issue_type相同，原文精确重复或AI修改建议高度相似）累计被人工拒绝达到这个
# 次数才自动降级为风格可选，避免手滑拒绝一次就永久压掉一类问题。用户指定初值3，太敏感/
# 太迟钝都只改这里，不用碰 core/classifier/modifier_rules.py。
FEEDBACK_REJECTION_THRESHOLD = 3
# difflib.SequenceMatcher.ratio() 阈值，判断两条issue的AI修改建议(suggestion)或判定依据
# (reason)——归一化去掉具体数字/引号内容后（core/feedback.py::_normalize_variable_parts）
# ——是否属于同一种"没有意义的改动"（而非同一句原文的字面重复，也不要求同一份文档）。
# 早期版本只用suggestion、阈值0.70；归一化上线后剥离了大部分误判来源，用户要求把reason也
# 纳入判定、阈值调低到0.60，调阈值只改这里。
FEEDBACK_SIMILARITY_THRESHOLD = 0.60
