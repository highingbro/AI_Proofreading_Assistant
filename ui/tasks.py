"""署名选择器与任务选择/创建界面——"先选任务，再选功能"这条线性流程的前半段。

`render_task_selection()` 是任务闸门那一屏：`app.py` 在当前任务不存在时只渲染它然后
`st.stop()`，侧边栏的功能入口 radio 根本不会被创建。
"""

from __future__ import annotations

import html

import streamlit as st

import config
from db.models import (
    count_records_by_task,
    create_task,
    get_authors,
    get_tasks,
    update_task_status,
)

_NEW_AUTHOR_OPTION = "＋新增署名…"
# 切换任务时保留的 session_state key：署名是"我是谁"，跟"现在在哪个任务里"是两个
# 独立维度，换任务不该要求重选一次署名（另两个是署名选择器自身的widget状态，一并留下
# 才能让下拉/输入框在rerun后仍显示同一个人）。
_KEEP_ON_TASK_SWITCH = {"author", "author_select", "new_author_input"}


def render_author_picker() -> None:
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


def switch_task() -> None:
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
        desc_text = html.escape(task["description"]) if task["description"] else " "
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


def render_task_selection() -> None:
    """任务选择/创建界面——没有当前任务时，整个应用只显示这一屏。

    流程是线性的：先选任务（或建任务），才出现功能入口。调用方在这之后立刻 st.stop()，
    所以侧边栏导航和四个功能页的代码根本不会执行。
    """
    st.header("选择任务")
    st.caption("每一次校对都归属于一个任务（比如某本期刊）。先选择要进入的任务，再选择功能。")

    show_all = st.checkbox("显示已解决的任务", value=False)
    tasks = get_tasks(status=None if show_all else config.TASK_STATUS_ACTIVE)
    # "已关闭"在前端任何地方都不展示——下拉框里选它能把任务关闭，但关闭后这个任务立刻
    # 从这里以及"改归属任务"下拉（ui/page_history.py::_render_move_record_to_task）里
    # 消失，不受上面这个复选框影响；想再看到/改回来，只能直接改数据库
    # （config.TASK_STATUS_CLOSED 因此是前端唯一能写入、但读不出来的状态值）。
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
