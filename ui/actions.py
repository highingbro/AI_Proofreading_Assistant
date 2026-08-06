"""页面共用的非渲染动作（有副作用、不画界面）。

放在一起是因为标准校对页和原稿比对页都要用：两者都把上传文件落盘后交给解析。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import streamlit as st

import config
from core import feedback_rules

logger = logging.getLogger(__name__)


def file_id(uploaded_file) -> str:
    return hashlib.md5(uploaded_file.getvalue()).hexdigest()


def prune_uploads_dir(directory: Path) -> None:
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


def regenerate_rules_after_export() -> None:
    """导出Excel成功后顺带把历史拒绝总结成规避规则跑一次。

    规则重算是一次几秒的LLM调用，之前放在每次点"拒绝"里，连续拒绝会次次触发、又慢又
    费额度。导出是"这一轮审校完成"的自然收尾动作、频率低，把重算挪到这里既让"拒绝"
    秒响应，又能让反馈自动生效，还把 N 次拒绝的 N 次重算压成 1 次。

    **只有标准校对页的导出调用它**——规则是注入校对提示词、让LLM少报某类问题用的，
    只有LLM报出来的问题被拒绝才构成"这类判断不对"的信号。原稿比对页的问题是逐字
    diff 出来的客观差异、跟LLM怎么判断无关，拒绝一条只说明这处改动可以接受，总结
    不出任何该让LLM规避的东西；历史记录页的导出也不触发，那不是刚审完一批新反馈的
    场景。失败只记日志，不影响导出这个主操作，也可随时去反馈页手动"重新生成规则"。
    """
    try:
        with st.spinner("正在根据本轮反馈更新规避规则…"):
            feedback_rules.regenerate_rejection_rules()
    except Exception:
        logger.exception("反馈规则重新生成失败")
