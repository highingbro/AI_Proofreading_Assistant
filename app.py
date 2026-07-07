"""Streamlit 入口（阶段1：仅最简页面骨架，不含业务逻辑）。"""

import streamlit as st

from db.database import init_db
from db.models import get_records

init_db()

st.set_page_config(page_title="出版校对AI助手", layout="wide")
st.title("出版校对AI助手")

page = st.sidebar.radio("功能入口", ("标准校对", "原稿比对", "历史记录"))

if page == "标准校对":
    st.header("标准校对")
    st.info("该功能将在后续阶段实现。")
elif page == "原稿比对":
    st.header("原稿比对")
    st.info("该功能将在后续阶段实现。")
else:
    st.header("历史记录")
    records = get_records()
    st.dataframe(records)
