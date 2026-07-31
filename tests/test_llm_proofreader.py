"""阶段4验收测试：LLM调用封装 + 校对提示词接入。

不耗API额度部分：全部 mock core.proofreader.chat_completion 或更底层的
requests.post，覆盖JSON解析容错、字段校验、定位回填、chat_completion自身
的重试/退避逻辑。

消耗额度的集成冒烟（@pytest.mark.integration，默认跳过，pytest -m integration 手动跑）：
构造一段约500字、故意埋入5类已知错误的文本，走真实API，人工核对检出情况。
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import core.llm_client as llm_client
import core.proofreader as proofreader
from core.chunker import Chunk
from core.llm_client import LLMCallError, chat_completion
from core.parser import ParsedBlock, ParsedDocument
from core.proofreader import LLMResponseError, ProofreadResult, proofread_chunk, proofread_document


def _synthetic_doc(blocks: list[ParsedBlock], total_pages: int = 1) -> ParsedDocument:
    return ParsedDocument(
        file_name="synthetic.docx",
        file_type="docx",
        total_pages=total_pages,
        blocks=blocks,
        layout_mode="single",
        text_source="native",
        warnings=[],
    )


def _make_chunk(text: str, block_indices: list[int], chunk_index: int = 0) -> Chunk:
    return Chunk(
        chunk_index=chunk_index,
        text=text,
        block_indices=block_indices,
        overlap_prefix_blocks=[],
        page_range=(1, 1),
        char_count=len(text),
    )


def _valid_item(original_text: str, **overrides) -> dict:
    item = {
        "original_text": original_text,
        "issue_type": "标点符号问题",
        "category": "normal",
        "confidence": "high",
        "suggestion": "建议修改",
        "reason": "示例依据",
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# JSON解析与容错
# ---------------------------------------------------------------------------

def test_parse_plain_json(monkeypatch):
    block_text = "这段正文包含待校对片段用于解析测试。"
    blocks = [ParsedBlock(page=1, block_index=0, text=block_text, block_type="paragraph", source_location="第1段")]
    parsed = _synthetic_doc(blocks)
    chunk = _make_chunk(block_text, [0])

    calls = []

    def fake_chat_completion(system_prompt, user_content):
        calls.append(user_content)
        return json.dumps([_valid_item("待校对片段")], ensure_ascii=False)

    monkeypatch.setattr(proofreader, "chat_completion", fake_chat_completion)

    issues = proofread_chunk(chunk, parsed)
    assert len(calls) == 1
    assert len(issues) == 1
    assert issues[0].original_text == "待校对片段"
    assert issues[0].located is True


def test_parse_fenced_json(monkeypatch):
    block_text = "这段正文包含围栏片段用于解析测试。"
    blocks = [ParsedBlock(page=1, block_index=0, text=block_text, block_type="paragraph", source_location="第1段")]
    parsed = _synthetic_doc(blocks)
    chunk = _make_chunk(block_text, [0])

    def fake_chat_completion(system_prompt, user_content):
        payload = json.dumps([_valid_item("围栏片段")], ensure_ascii=False)
        return f"```json\n{payload}\n```"

    monkeypatch.setattr(proofreader, "chat_completion", fake_chat_completion)

    issues = proofread_chunk(chunk, parsed)
    assert len(issues) == 1
    assert issues[0].original_text == "围栏片段"


def test_parse_json_with_raw_control_character_in_string_succeeds_without_retry(monkeypatch):
    """回归测试：真实使用中LLM在reason字段里直接输出裸换行（未转义成\\n），
    真实报错 "Invalid control character at: line 40 column 45"。这类内容本身是合法的
    多行文本，不该被当成坏JSON触发重试（重试意味着整块再等一轮LLM调用，真实耗时178秒）。
    """
    block_text = "这段正文包含控制字符片段用于解析测试。"
    blocks = [ParsedBlock(page=1, block_index=0, text=block_text, block_type="paragraph", source_location="第1段")]
    parsed = _synthetic_doc(blocks)
    chunk = _make_chunk(block_text, [0])

    calls = []
    # 手写JSON字符串，reason字段内嵌一个裸换行（不是"\\n"转义），模拟真实LLM输出。
    raw_response = (
        '[{"original_text": "控制字符片段", "issue_type": "标点符号问题", '
        '"category": "normal", "confidence": "high", "suggestion": "建议修改", '
        '"reason": "第一行依据\n第二行依据"}]'
    )

    def fake_chat_completion(system_prompt, user_content):
        calls.append(user_content)
        return raw_response

    monkeypatch.setattr(proofreader, "chat_completion", fake_chat_completion)

    issues = proofread_chunk(chunk, parsed)
    assert len(calls) == 1  # 不应触发重试
    assert len(issues) == 1
    assert issues[0].original_text == "控制字符片段"
    assert issues[0].reason == "第一行依据\n第二行依据"


def test_parse_invalid_json_retries_then_succeeds(monkeypatch):
    block_text = "这段正文包含重试片段用于解析测试。"
    blocks = [ParsedBlock(page=1, block_index=0, text=block_text, block_type="paragraph", source_location="第1段")]
    parsed = _synthetic_doc(blocks)
    chunk = _make_chunk(block_text, [0])

    calls = []

    def fake_chat_completion(system_prompt, user_content):
        calls.append(user_content)
        if len(calls) == 1:
            return "这不是合法JSON"
        return json.dumps([_valid_item("重试片段")], ensure_ascii=False)

    monkeypatch.setattr(proofreader, "chat_completion", fake_chat_completion)

    issues = proofread_chunk(chunk, parsed)
    assert len(calls) == 2
    assert proofreader._RETRY_HINT in calls[1]
    assert len(issues) == 1
    assert issues[0].original_text == "重试片段"


def test_parse_invalid_json_fails_after_retry(monkeypatch):
    block_text = "这段正文用于彻底失败测试。"
    blocks = [ParsedBlock(page=1, block_index=0, text=block_text, block_type="paragraph", source_location="第1段")]
    parsed = _synthetic_doc(blocks)
    chunk = _make_chunk(block_text, [0])

    def fake_chat_completion(system_prompt, user_content):
        return "依然不是合法JSON"

    monkeypatch.setattr(proofreader, "chat_completion", fake_chat_completion)

    with pytest.raises(LLMResponseError) as exc_info:
        proofread_chunk(chunk, parsed)
    assert exc_info.value.raw_response == "依然不是合法JSON"


# ---------------------------------------------------------------------------
# 字段校验
# ---------------------------------------------------------------------------

def test_field_validation_drops_invalid_entries(monkeypatch):
    block_text = "这段正文包含字段测试文本片段用于校验丢弃逻辑标点符号问题较多需要校对。"
    blocks = [ParsedBlock(page=1, block_index=0, text=block_text, block_type="paragraph", source_location="第1段")]
    parsed = _synthetic_doc(blocks)
    chunk = _make_chunk(block_text, [0])

    items = [
        _valid_item("字段测试文本片段"),
        {  # 缺 reason 字段
            "original_text": "字段测试文本片段",
            "issue_type": "标点符号问题",
            "category": "normal",
            "confidence": "high",
            "suggestion": "建议修改",
        },
        _valid_item("字段测试文本片段", category="unknown"),  # 非法枚举值
        _valid_item("字段测试文本片段", confidence="超高"),  # 非法枚举值
    ]

    def fake_chat_completion(system_prompt, user_content):
        return json.dumps(items, ensure_ascii=False)

    monkeypatch.setattr(proofreader, "chat_completion", fake_chat_completion)

    issues = proofread_chunk(chunk, parsed)
    assert len(issues) == 1
    assert issues[0].original_text == "字段测试文本片段"


# ---------------------------------------------------------------------------
# 原文定位回填
# ---------------------------------------------------------------------------

def test_location_backfill(monkeypatch):
    overlap_block_text = "上文占位内容丙丁戊己庚辛壬癸子丑寅卯用于填充"
    body_block1_text = "正文包含错误关键词ABC需要校对的内容片段"
    body_block2_text = "另一段正文包含关键词XYZ用于测试定位是否正确"

    blocks = [
        ParsedBlock(page=1, block_index=0, text=overlap_block_text, block_type="paragraph", source_location="第1段"),
        ParsedBlock(page=1, block_index=1, text=body_block1_text, block_type="paragraph", source_location="第2段"),
        ParsedBlock(page=2, block_index=2, text=body_block2_text, block_type="paragraph", source_location="第3段"),
    ]
    parsed = _synthetic_doc(blocks, total_pages=2)

    chunk_text = (
        f"{config.CHUNK_OVERLAP_MARK}\n{overlap_block_text}\n"
        f"{config.CHUNK_BODY_MARK}\n{body_block1_text}{body_block2_text}"
    )
    chunk = Chunk(
        chunk_index=1,
        text=chunk_text,
        block_indices=[1, 2],
        overlap_prefix_blocks=[0],
        page_range=(1, 2),
        char_count=len(chunk_text),
    )

    items = [
        _valid_item("错误关键词ABC"),  # 正文区block1
        _valid_item("关键词XYZ"),  # 正文区block2
        _valid_item("占位内容丙丁戊"),  # 仅出现在重叠区 -> 应丢弃
        _valid_item("完全不存在的内容片段"),  # 哪里都找不到 -> 保留但located=False
    ]

    def fake_chat_completion(system_prompt, user_content):
        return json.dumps(items, ensure_ascii=False)

    monkeypatch.setattr(proofreader, "chat_completion", fake_chat_completion)

    issues = proofread_chunk(chunk, parsed)
    by_text = {i.original_text: i for i in issues}

    assert "占位内容丙丁戊" not in by_text  # 仅在重叠区出现，被丢弃

    assert by_text["错误关键词ABC"].located is True
    assert by_text["错误关键词ABC"].block_index == 1
    assert by_text["错误关键词ABC"].page_location == "第2段"

    assert by_text["关键词XYZ"].located is True
    assert by_text["关键词XYZ"].block_index == 2
    assert by_text["关键词XYZ"].page_location == "第3段"

    assert by_text["完全不存在的内容片段"].located is False
    assert by_text["完全不存在的内容片段"].block_index is None
    assert by_text["完全不存在的内容片段"].page_location is None

    assert len(issues) == 3


# ---------------------------------------------------------------------------
# 校对模式（精简/深度）规则子集
# ---------------------------------------------------------------------------

def test_split_rules_by_number_covers_all_ten():
    rules_text = proofreader._RULES_PATH.read_text(encoding="utf-8")
    blocks = proofreader._split_rules_by_number(rules_text)
    assert set(blocks.keys()) == set(range(1, 11))
    assert blocks[1].startswith("**1. 错别字与拼写**")
    assert blocks[10].startswith("**10. 民族与地名规范**")


def test_build_system_prompt_deep_mode_includes_all_rules():
    prompt = proofreader._build_system_prompt(config.PROOFREAD_MODE_DEEP)
    for n in range(1, 11):
        assert f"**{n}. " in prompt


def test_build_system_prompt_default_is_deep_mode():
    assert proofreader._build_system_prompt() == proofreader._build_system_prompt(config.PROOFREAD_MODE_DEEP)


def test_build_system_prompt_simplified_mode_keeps_only_selected_rules():
    prompt = proofreader._build_system_prompt(config.PROOFREAD_MODE_SIMPLIFIED)
    for n in config.SIMPLIFIED_RULE_NUMBERS:
        assert f"**{n}. " in prompt
    for n in set(range(1, 11)) - set(config.SIMPLIFIED_RULE_NUMBERS):
        assert f"**{n}. " not in prompt


def test_proofread_chunk_forwards_mode_to_system_prompt(monkeypatch):
    block_text = "这段正文用于模式透传测试。"
    blocks = [ParsedBlock(page=1, block_index=0, text=block_text, block_type="paragraph", source_location="第1段")]
    parsed = _synthetic_doc(blocks)
    chunk = _make_chunk(block_text, [0])

    captured_prompts = []

    def fake_chat_completion(system_prompt, user_content):
        captured_prompts.append(system_prompt)
        return json.dumps([], ensure_ascii=False)

    monkeypatch.setattr(proofreader, "chat_completion", fake_chat_completion)

    proofread_chunk(chunk, parsed, mode=config.PROOFREAD_MODE_SIMPLIFIED)
    assert "**1. " in captured_prompts[0]
    assert "**3. " not in captured_prompts[0]


def test_proofread_document_forwards_mode_to_proofread_chunk(monkeypatch):
    block_text = "这段正文用于文档级模式透传测试。"
    blocks = [ParsedBlock(page=1, block_index=0, text=block_text, block_type="paragraph", source_location="第1段")]
    parsed = _synthetic_doc(blocks)
    chunks = [_make_chunk(block_text, [0], chunk_index=0)]

    from core.chunker import ChunkedDocument

    chunked = ChunkedDocument(source=parsed, chunks=chunks, chunk_size_target=100, overlap_blocks=0, warnings=[])

    captured_modes = []

    def fake_proofread_chunk(chunk, parsed_doc, mode, rejection_rules_text=""):
        captured_modes.append(mode)
        return []

    monkeypatch.setattr(proofreader, "proofread_chunk", fake_proofread_chunk)

    proofread_document(chunked, mode=config.PROOFREAD_MODE_SIMPLIFIED)
    assert captured_modes == [config.PROOFREAD_MODE_SIMPLIFIED]


# ---------------------------------------------------------------------------
# proofread_document：单块失败隔离 + progress_callback
# ---------------------------------------------------------------------------

def test_proofread_document_isolates_chunk_failures(monkeypatch):
    block_text = "这段正文用于文档级别测试。"
    blocks = [ParsedBlock(page=1, block_index=0, text=block_text, block_type="paragraph", source_location="第1段")]
    parsed = _synthetic_doc(blocks)
    chunks = [_make_chunk(block_text, [0], chunk_index=0), _make_chunk(block_text, [0], chunk_index=1)]

    from core.chunker import ChunkedDocument

    chunked = ChunkedDocument(source=parsed, chunks=chunks, chunk_size_target=100, overlap_blocks=0, warnings=[])

    call_count = [0]

    def fake_proofread_chunk(chunk, parsed_doc, mode, rejection_rules_text=""):
        call_count[0] += 1
        if chunk.chunk_index == 0:
            raise LLMCallError("模拟调用失败")
        return [proofreader.RawIssue(
            original_text="正文",
            issue_type="标点符号问题",
            category="normal",
            confidence="high",
            suggestion="建议修改",
            reason="示例依据",
            block_index=0,
            page_location="第1段",
            chunk_index=1,
            located=True,
        )]

    monkeypatch.setattr(proofreader, "proofread_chunk", fake_proofread_chunk)

    progress_calls = []
    result = proofread_document(chunked, progress_callback=lambda i, total: progress_calls.append((i, total)))

    assert call_count[0] == 2
    assert len(result.issues) == 1
    assert len(result.chunk_warnings) == 1
    assert "第0块" in result.chunk_warnings[0]
    assert progress_calls == [(1, 2), (2, 2)]


def test_proofread_document_runs_chunks_concurrently(monkeypatch):
    """验证 proofread_document 确实并发调用（不是串行排队）：多个块的执行区间必须重叠。"""
    block_text = "并发测试用正文。"
    blocks = [ParsedBlock(page=1, block_index=0, text=block_text, block_type="paragraph", source_location="第1段")]
    parsed = _synthetic_doc(blocks)
    chunks = [_make_chunk(block_text, [0], chunk_index=i) for i in range(4)]

    from core.chunker import ChunkedDocument

    chunked = ChunkedDocument(source=parsed, chunks=chunks, chunk_size_target=100, overlap_blocks=0, warnings=[])

    lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0

    def fake_proofread_chunk(chunk, parsed_doc, mode, rejection_rules_text=""):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.2)
        with lock:
            in_flight -= 1
        return []

    monkeypatch.setattr(proofreader, "proofread_chunk", fake_proofread_chunk)

    start = time.monotonic()
    proofread_document(chunked)
    elapsed = time.monotonic() - start

    assert max_in_flight >= 2  # 确认确实有块同时在执行，不是串行排队
    assert elapsed < 0.2 * len(chunks)  # 并发下总耗时应明显小于"块数×单块耗时"的串行值


def test_proofread_document_caps_concurrency_at_config_limit(monkeypatch):
    """块数超过 config.PROOFREAD_MAX_CONCURRENT_CHUNKS 时，同时在跑的块数不能超过该上限——
    验证 data/app.log 揭示的"服务端并发处理能力有限、无上限并发会互相排队拖时间"这一问题
    确实被修复，不是只测并发存在（那是上面那条测试的职责）。"""
    monkeypatch.setattr(config, "PROOFREAD_MAX_CONCURRENT_CHUNKS", 3)

    block_text = "并发上限测试用正文。"
    blocks = [ParsedBlock(page=1, block_index=0, text=block_text, block_type="paragraph", source_location="第1段")]
    parsed = _synthetic_doc(blocks)
    chunks = [_make_chunk(block_text, [0], chunk_index=i) for i in range(10)]

    from core.chunker import ChunkedDocument

    chunked = ChunkedDocument(source=parsed, chunks=chunks, chunk_size_target=100, overlap_blocks=0, warnings=[])

    lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0

    def fake_proofread_chunk(chunk, parsed_doc, mode, rejection_rules_text=""):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.1)
        with lock:
            in_flight -= 1
        return []

    monkeypatch.setattr(proofreader, "proofread_chunk", fake_proofread_chunk)

    proofread_document(chunked)

    assert max_in_flight == 3


# ---------------------------------------------------------------------------
# chat_completion 自身的重试/退避逻辑
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json_data


@pytest.fixture
def llm_env(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://example.com/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_MODEL", "qwen3.6-plus")
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "LLM_TIMEOUT", None)  # 默认走动态估算，个别测试再自行覆盖
    sleep_calls = []
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: sleep_calls.append(s))
    return sleep_calls


def test_chat_completion_retries_network_error_then_succeeds(monkeypatch, llm_env):
    sleep_calls = llm_env
    call_count = [0]

    def fake_post(url, headers=None, json=None, timeout=None):
        call_count[0] += 1
        if call_count[0] <= 2:
            raise requests.exceptions.ConnectionError("网络错误")
        return _FakeResponse(200, json_data={"choices": [{"message": {"content": "校对结果"}}]})

    monkeypatch.setattr(llm_client.requests, "post", fake_post)

    result = chat_completion("system", "user")
    assert result == "校对结果"
    assert call_count[0] == 3
    assert sleep_calls == [1, 4]


def test_chat_completion_exhausts_retries_raises(monkeypatch, llm_env):
    call_count = [0]

    def fake_post(url, headers=None, json=None, timeout=None):
        call_count[0] += 1
        raise requests.exceptions.Timeout("超时")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)

    with pytest.raises(LLMCallError):
        chat_completion("system", "user")
    assert call_count[0] == config.LLM_MAX_RETRIES + 1


def test_chat_completion_honors_retry_after_header(monkeypatch, llm_env):
    sleep_calls = llm_env
    call_count = [0]

    def fake_post(url, headers=None, json=None, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return _FakeResponse(429, text="rate limited", headers={"Retry-After": "7"})
        return _FakeResponse(200, json_data={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm_client.requests, "post", fake_post)

    result = chat_completion("system", "user")
    assert result == "ok"
    assert sleep_calls == [7]


def test_chat_completion_non_retryable_4xx_fails_immediately(monkeypatch, llm_env):
    call_count = [0]

    def fake_post(url, headers=None, json=None, timeout=None):
        call_count[0] += 1
        return _FakeResponse(401, text="unauthorized")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)

    with pytest.raises(LLMCallError):
        chat_completion("system", "user")
    assert call_count[0] == 1  # 不可重试，只应尝试一次


def test_chat_completion_missing_config_raises_without_request(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "")
    monkeypatch.setattr(config, "LLM_API_KEY", "")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("配置缺失时不应发起真实请求")

    monkeypatch.setattr(llm_client.requests, "post", fail_if_called)

    with pytest.raises(LLMCallError):
        chat_completion("system", "user")


# ---------------------------------------------------------------------------
# 固定超时
# ---------------------------------------------------------------------------

def test_timeout_is_flat_regardless_of_text_length(monkeypatch, llm_env):
    """超时固定为 config.LLM_TIMEOUT_FIXED_SECONDS，不随文本长度浮动（原因见
    core/llm_client.py 模块docstring）。"""
    captured_timeouts = []

    def fake_post(url, headers=None, json=None, timeout=None):
        captured_timeouts.append(timeout)
        return _FakeResponse(200, json_data={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm_client.requests, "post", fake_post)

    chat_completion("s" * 100, "u" * 100)
    chat_completion("s" * 5000, "u" * 5000)

    short_timeout, long_timeout = captured_timeouts
    assert short_timeout == long_timeout == config.LLM_TIMEOUT_FIXED_SECONDS


def test_llm_timeout_env_override_takes_precedence_over_fixed(monkeypatch, llm_env):
    monkeypatch.setattr(config, "LLM_TIMEOUT", 77)
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["timeout"] = timeout
        return _FakeResponse(200, json_data={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm_client.requests, "post", fake_post)

    chat_completion("s" * 5000, "u" * 5000)  # 文本很长也应固定用77，不走LLM_TIMEOUT_FIXED_SECONDS
    assert captured["timeout"] == 77


def test_explicit_timeout_param_overrides_everything(monkeypatch, llm_env):
    monkeypatch.setattr(config, "LLM_TIMEOUT", 77)
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["timeout"] = timeout
        return _FakeResponse(200, json_data={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm_client.requests, "post", fake_post)

    chat_completion("s", "u", timeout=13)
    assert captured["timeout"] == 13


# ---------------------------------------------------------------------------
# 集成冒烟（消耗真实API额度，默认跳过）
# ---------------------------------------------------------------------------

_EMBEDDED_ERROR_TEXT = (
    "本次会议由项目组统一组织，旨在推进季度工作总结与下阶段计划的制定。"
    "会议开始前，主持人宣布、活动正式启动，随后邀请了张三、李四等专家代表发言。\n"
    "发言中提到，安全帽是每位施工人员必须品，任何人未佩戴不得进入现场；"
    "同时强调，携带有效证件是入场的必须条件，请大家提前准备。\n"
    "会上还引用了《论语》中的名句：\"子曰：'学而时习之，不亦说乎？"
    "有朋自远方来，不亦乐乎？'\"，以此勉励团队保持学习热情。\n"
    "此外，主持人介绍了本次特邀嘉宾——著名科学家钱学森，并提到他曾任麻省理工学院校长一职，"
    "在学术界享有盛誉。\n"
    "说实话，这次会议整体安排还挺靠谱的，大家反馈也都比较积极。"
)


@pytest.mark.integration
def test_integration_embedded_errors_smoke():
    """走真实API，人工核对五类埋错的检出/分类情况（不做召回率硬断言）。"""
    blocks = [
        ParsedBlock(
            page=1, block_index=0, text=_EMBEDDED_ERROR_TEXT, block_type="paragraph", source_location="第1段"
        )
    ]
    parsed = _synthetic_doc(blocks)
    chunk = _make_chunk(_EMBEDDED_ERROR_TEXT, [0])

    issues = proofread_chunk(chunk, parsed)

    assert len(issues) >= 1
    for issue in issues:
        print(
            f"[{issue.category}][{issue.confidence}][{issue.issue_type}] "
            f"{issue.original_text} → {issue.suggestion} ({issue.reason})"
        )
