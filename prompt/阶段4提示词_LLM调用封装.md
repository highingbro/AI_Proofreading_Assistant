# 阶段4 开发提示词:LLM调用封装 + 校对提示词接入(core/llm_client.py)



---

接上一阶段。解析与分块已完成。现在实现**阶段4:LLM调用封装与校对提示词接入(core/llm_client.py + core/proofreader.py)**。本阶段目标:**单个 Chunk 进,结构化问题列表出**。结果分层的归层校验是阶段5,本阶段只要求 LLM 按约定格式输出原始判断。

## 配置约定(config.py 扩展)

从环境变量读取(启动时校验,缺失给出明确报错):

- `LLM_BASE_URL`:API 地址(使用qwen的sdk)
- `LLM_API_KEY`
- `LLM_MODEL`:模型名 qwen 3.6 plus


其他配置项:`LLM_TIMEOUT`(默认120秒)、`LLM_MAX_RETRIES`(默认3)、`LLM_TEMPERATURE`(默认0,校对任务要求稳定输出)。

## 本阶段任务

### 1. LLM 客户端封装(core/llm_client.py)

```python
def chat_completion(
    system_prompt: str,
    user_content: str,
    *,
    temperature: float | None = None,
    timeout: int | None = None,
) -> str:
    """单轮调用,返回助手回复文本。内部处理重试与错误。"""
```

要求:
- **重试**:网络错误、超时、429限流、5xx 按指数退避重试(1s/4s/16s),重试耗尽抛自定义异常 `LLMCallError`(带最后一次错误信息)
- **限流友好**:429 响应若带 Retry-After 则遵守
- **日志**:每次调用记录耗时、输入/输出字符数、重试次数(标准 logging,别 print)
- 不做流式,不做并发(免费额度普遍限速,串行最稳;并发留到以后有需要再说)

### 2. 校对提示词组装(prompts/ 目录 + core/proofreader.py)

**2.1 提示词文件结构**

```
prompt/
├── proofread_rules.md      # 我提供的既有校对规则(十类维度)——原样使用,不要改写
└── proofread_system.md     # 本阶段新建:系统提示词模板
```

`proofread_system.md` 需包含(按此顺序组装):
1. 角色设定:出版社专业校对
2. 引用 proofread_rules.md 全文(十类检查维度)
3. **补充规则一(引文识别与保护)**:判断为引用原始文献/古籍/他人原话的内容,一律标注 category='quotation',不得给出修改建议,suggestion 填"原文照录,不建议改动"
4. **补充规则二(事实性置信度分级)**:涉及人名、职务、历史事件等具体事实的修改建议,必须给出 confidence(high/medium/low);只有 high 才允许写确定性的修改建议,否则 suggestion 措辞为"存疑,建议人工核实:……"
5. **重叠区规则**:输入文本中【上文回顾…】标记之间的内容仅供理解上下文,其中的问题**不要报告**(标记文案引用 config.py 中阶段3定义的常量,组装时动态注入,保证与分块输出一致)
6. 输出格式规范(见2.2)

**2.2 LLM 输出格式(JSON)**

要求 LLM 只输出 JSON 数组,每个元素:

```json
{
  "original_text": "有问题的原文片段(≤50字,必须逐字取自正文)",
  "issue_type": "十类维度之一,如'错别字'",
  "category": "normal | quotation | factual | style",
  "confidence": "high | medium | low",
  "suggestion": "修改建议或核实提示",
  "reason": "一句话说明依据"
}
```

category 语义:normal=一般语言问题 / quotation=涉及引文 / factual=涉及事实知识 / style=纯风格建议。(这是 LLM 的"自报分类",阶段5会做规则化归层校验,不直接信任它。)

### 3. 单块校对函数(core/proofreader.py)

```python
def proofread_chunk(chunk: Chunk) -> list[RawIssue]: ...
```

- 组装 system prompt + chunk.text 调用 chat_completion
- **解析与容错**(重点,免费模型输出纪律普遍较差):
  - 剥离 markdown 代码围栏(```json ... ```)后再解析
  - 解析失败→重试一次(在user消息中附加"你上次输出不是合法JSON,只输出JSON数组");再失败抛 `LLMResponseError`,原始回复存入日志
  - 逐条字段校验:缺字段/枚举值非法的条目丢弃并记日志,不让一条脏数据毒死整块
- **原文定位回填**:每条 issue 的 original_text 在 chunk 的**正文区**(非重叠区)文本中查找,定位到所属 block_index,生成 RawIssue:

```python
@dataclass
class RawIssue:
    original_text: str
    issue_type: str
    category: str        # LLM自报,待阶段5校验
    confidence: str
    suggestion: str
    reason: str
    block_index: int | None   # 定位失败为None
    page_location: str | None # locate_block() 的结果
    chunk_index: int
    located: bool             # 是否成功定位
```

- 定位规则:先精确子串匹配;失败则做宽松匹配(去空白后匹配);再失败 located=False 并记日志(**不丢弃**,阶段5会把未定位条目降级处理)
- **重叠区过滤**:original_text 只在重叠区文本中出现、正文区找不到的条目,直接丢弃并记日志(LLM 违反了"重叠区不报告"规则的兜底)

### 4. 全文档校对函数

```python
def proofread_document(chunked: ChunkedDocument, progress_callback=None) -> list[RawIssue]: ...
```

- 顺序遍历 chunks 调用 proofread_chunk,progress_callback(当前块序号, 总块数) 供阶段6接进度条
- 单块失败(LLMCallError/LLMResponseError)不中断全文档:记录该块失败信息到返回结果附带的 warnings(可把返回改为带warnings的结果对象),继续下一块

### 5. 调试辅助脚本

`tools/run_proofread.py`:传入文件路径(串起 parse→chunk→proofread),参数 `--max-chunks N` 限制只跑前N块(省额度),输出:
- 每条 RawIssue 一行:`[页码位置][issue_type][category][confidence][located] 原文→建议`
- 末尾统计:总条数、各category条数、未定位条数、失败块列表

### 6. 测试(tests/test_stage4.py)

**不消耗API额度的单元测试(mock chat_completion)**:
- JSON解析:正常JSON、带围栏JSON、非法JSON重试后成功、彻底失败抛异常
- 字段校验:缺字段条目被丢弃,合法条目保留
- 定位回填:构造已知chunk,断言 block_index 和 page_location 正确;正文找不到但重叠区能找到的条目被丢弃;两处都找不到的 located=False 且保留
- 重试逻辑:mock 连续失败→指数退避→LLMCallError

**消耗少量额度的集成冒烟(标记 @pytest.mark.integration,默认跳过,`pytest -m integration` 手动跑)**:
- 构造一段约500字、**故意埋入已知错误**的测试文本(埋错内容见下方"埋错清单"),走真实API
- 断言:返回≥1条issue、JSON全部解析成功;打印全部结果供人工核对召回情况(不对召回率做硬性断言,免费模型效果本来就是本阶段要评估的对象)

**埋错清单(写入测试文件,内容示例)**:
1. 错别字:"必需"误作"必须"类搭配错误 × 2处
2. 标点:句中误用顿号 × 1处
3. 一段带书名号和引号的古文引文(内容故意用一个古今写法有差异的字,看模型是否妄改→期望 category=quotation)
4. 一个真实历史人物+错误职务的表述(期望 category=factual 且非high则给核实措辞)
5. 一句完全正确但可有可无的口语化表达(看是否被标为 style)

### 7. requirements.txt

新增 qwen(或不加,用 requests 亦可,二选一说明理由)。

## 完成标准

1. 不耗额度的单元测试全部通过
2. `pytest -m integration` 真实API冒烟通过,人工核对埋错文本的输出:五类埋错的检出与分类情况如实记录(检不出/分类错也如实记,这是模型效果基线,不是代码bug)
3. `tools/run_proofread.py tests/samples/sample.pdf --max-chunks 2` 全链路跑通,输出可读
4. API信息全部来自环境变量,代码中无硬编码密钥

## 约束

- 只实现 llm_client.py、proofreader.py、提示词文件与脚本,不动 classifier(阶段5)及以后
- proofread_rules.md 原样引用,不要"优化"我的校对规则文案
- temperature 默认0
- 免费额度模型对复杂指令遵循可能不稳定,如果集成测试发现输出格式频繁失败,如实报告失败率,提出提示词调整建议后由我决定,不要自行大改提示词结构

---

## 附:给你自己的提醒(不粘给 Claude Code)

1. **本阶段真正要评估的是免费模型的效果基线**:埋错文本的集成测试结果(五类错误的检出率、引文是否被妄改、事实类是否乱下结论)决定了后面要不要换模型。把结果留存,作为以后换模型时的对比基准。
2. **视觉模型 vs OCR 的对比实验**可以在本阶段顺手做:埋错文本排成图片喂视觉模型 vs OCR文字喂文字模型,同一模型厂商下比较。如果你想做,验收完阶段4主线后告诉我,我单独给你出一份对比实验的小提示词,不混进主线。
3. **重叠区去重的另一半**在阶段5:本阶段只过滤了"仅出现在重叠区"的条目,相邻块对同一处问题的重复报告(正文区重叠导致)留给阶段5按 block_index+original_text 去重。
4. Temperature=0 下同一文本多跑几次结果仍可能有波动,属正常,别追求逐次完全一致。
