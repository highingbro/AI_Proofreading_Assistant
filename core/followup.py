"""对话追问处理模块（阶段7实现）。

针对某一条已分层的问题追问，携带该条的原文上下文（阶段6/本阶段
core/workflow.py::persist_result 落库时算好的 context_snippet）调用 LLM 回答。
回答只影响该条，不重新校对全文、不改变该条的 layer/priority/status。

多轮追问通过在 user_content 里拼接历史问答文本实现（chat_completion 只有单轮
system+user 接口，阶段4已验收冻结，不改其签名）；为控制token成本，只把最近
config.FOLLOWUP_MAX_HISTORY_TURNS 轮历史拼进prompt，更早的历史仍完整存库。

`answer_followup(issue_id, question, db_path=None) -> str` 是唯一的追问入口。
另有 `get_followup_history(issue_id, db_path=None) -> list[dict]`——占位签名
没有这个函数，新增原因是 followup_history 列存的是JSON字符串，这个存储格式
是本模块的内部实现细节，app.py 不应该自己 json.loads 去解析，理应由本模块
提供解析好的结构给UI层用。

四条关键设计决策：

1. context_snippet 必须在 persist_result 落库时就地计算，不能等追问时才现
   算：issues 表只存 page_location（人类可读TEXT，不可解析回整数），没存
   block_index；"原文前后文窗口"必须靠 block_index 去 ParsedDocument.blocks
   找相邻block，而 ParsedDocument 只在 run_standard_proofread() 执行期间存
   在于内存里，追问发生的时刻（可能是校对完成后无数次Streamlit rerun之后）
   根本拿不到。详见 core/workflow/CLAUDE.md"context_snippet 计算时机"一节。
2. 不新增数据库列（不加 reason/block_index 列）：data/app.db 已有真实历史
   数据，CREATE TABLE IF NOT EXISTS 不会给已存在表追加新列，贸然加列会导致
   旧库 INSERT 因列不存在而失败。追问的"依据"素材靠现有列拼：
   original_text+context_snippet+issue_type+layer+priority+suggestion。这
   是权衡后的判断，不是唯一正确答案——如果实际使用中发现追问回答质量因缺少
   LLM原始判断依据（reason）明显打折扣，可以回头补，补的时候要处理"旧库缺
   列"的迁移问题。
3. answer_followup 签名比占位多 db_path=None（对齐 core/exporter.py
   export_issues_to_excel 的先例，内部要读写 issues 表，测试要能传临时库）。
4. UI用 st.chat_message 展示历史 + st.text_input+按钮做输入，没有用框架文档
   技术选型表写的 st.chat_input：st.chat_input 不放进容器时全局只能有一个
   实例，每篇文档可能有几十条issue、每条一个问题卡，用
   st.text_input+st.button（key 分别为 f"followup_input_{issue_id}"/
   f"followup_submit_{issue_id}"，和现有采纳/拒绝按钮的key惯例一致）更可控。

`db/models.py` 支撑函数：get_issue(issue_id, db_path=None) -> dict | None
（按主键查单条，不存在返回 None）、
update_issue_followup(issue_id, followup_history, db_path=None)（只更新这一
列）。

`prompt/followup_system.md` 是追问的系统提示词模板，占位符
{{ORIGINAL_TEXT}}/{{CONTEXT_SNIPPET}}/{{ISSUE_TYPE}}/{{LAYER}}/{{PRIORITY}}/
{{SUGGESTION}}，{{CONTEXT_SNIPPET}} 为空时替换成提示LLM"未能定位到原文上下
文"的话术；回答要求里明确写了"存疑待核实/引文类的追问要延续同等谨慎度，不
能因为用户追问了几句就把回答说成确定无疑的结论"——这是设计铁律第3条（事实
性内容置信度分级）在追问场景下的延伸，不能因为多了一轮对话就绕开。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import config
from core.llm_client import chat_completion
from db.models import get_issue, update_issue_followup

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompt" / "followup_system.md"

_NO_CONTEXT_HINT = "（未能定位到原文上下文，请仅基于原文片段与建议回答，如有必要提醒用户这一点）"


def _build_system_prompt(issue: dict) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    context_snippet = issue["context_snippet"] or _NO_CONTEXT_HINT
    return (
        template.replace("{{ORIGINAL_TEXT}}", issue["original_text"] or "")
        .replace("{{CONTEXT_SNIPPET}}", context_snippet)
        .replace("{{ISSUE_TYPE}}", issue["issue_type"] or "")
        .replace("{{LAYER}}", issue["layer"] or "")
        .replace("{{PRIORITY}}", issue["priority"] or "")
        .replace("{{SUGGESTION}}", issue["suggestion"] or "")
    )


def _parse_history(raw: str | None) -> list[dict]:
    if not raw:
        return []
    return json.loads(raw)


def _build_user_content(history: list[dict], question: str) -> str:
    recent = history[-config.FOLLOWUP_MAX_HISTORY_TURNS :]
    if not recent:
        return f"【本次追问】\n{question}"

    lines = ["【历史追问】"]
    for turn in recent:
        lines.append(f"Q: {turn['question']}")
        lines.append(f"A: {turn['answer']}")
    lines.append("")
    lines.append("【本次追问】")
    lines.append(question)
    return "\n".join(lines)


def answer_followup(issue_id: int, question: str, db_path=None) -> str:
    """针对指定问题条目的追问，携带其上下文调用 LLM 并返回回答，同时把本轮问答追加进该issue的追问历史。"""
    issue = get_issue(issue_id, db_path=db_path)
    if issue is None:
        raise ValueError(f"issue_id {issue_id} 不存在")

    history = _parse_history(issue["followup_history"])

    system_prompt = _build_system_prompt(issue)
    user_content = _build_user_content(history, question)

    answer = chat_completion(system_prompt, user_content)

    history.append(
        {"question": question, "answer": answer, "asked_at": datetime.now().isoformat()}
    )
    update_issue_followup(issue_id, json.dumps(history, ensure_ascii=False), db_path=db_path)

    return answer


def get_followup_history(issue_id: int, db_path=None) -> list[dict]:
    """返回该issue的追问历史，每轮为 {"question", "answer", "asked_at"}，无历史返回空列表。"""
    issue = get_issue(issue_id, db_path=db_path)
    if issue is None:
        raise ValueError(f"issue_id {issue_id} 不存在")
    return _parse_history(issue["followup_history"])
