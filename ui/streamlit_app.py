from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_ROOT / ".env")

import httpx
import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(_ROOT))

from llm_agent.main import answer, sanitize_chat_output

SERVER_URL = os.getenv("SERVER_URL", "http://server:8000")

st.set_page_config(page_title="Мониторинг инфраструктуры", layout="wide")
st.title("Мониторинг вычислительной инфраструктуры")


def fetch_json(path: str, params: dict | None = None):
    with httpx.Client(base_url=SERVER_URL, timeout=10.0) as client:
        resp = client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()


def clean_assistant_text(text: str) -> str:
    cleaned = sanitize_chat_output(text)
    return cleaned or "Не удалось сформировать ответ."


tab_dashboard, tab_chat = st.tabs(["Дашборд", "Чат с агентом"])

with tab_dashboard:
    hosts = fetch_json("/api/hosts")
    selected_host_id: str | None = None
    st.subheader("Хосты")
    if not hosts:
        st.info("Хосты пока не зарегистрированы.")
    else:
        cols = st.columns(min(4, len(hosts)))
        for idx, host in enumerate(hosts):
            with cols[idx % len(cols)]:
                st.metric(label=host["name"], value=host["status"], delta=host.get("host_id"))
                last_seen = host.get("last_seen", "n/a")
                st.caption(f"Последний контакт: {last_seen}")

        host_options = {f"{h['name']} ({h['host_id']})": h["host_id"] for h in hosts}
        selected_label = st.selectbox("Выберите хост", list(host_options.keys()))
        selected_host_id = host_options[selected_label]
        st.session_state["selected_host_id"] = selected_host_id
        metric = st.radio(
            "Метрика",
            ["cpu_percent", "memory_percent", "disk_percent"],
            horizontal=True,
        )
        since = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        points = fetch_json(
            "/api/metrics",
            params={"host_id": selected_host_id, "metric": metric, "since": since},
        )["points"]
        if points:
            df = pd.DataFrame(points)
            fig = px.line(df, x="ts", y="value", title=f"{metric} за последние 15 минут")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Недостаточно данных для графика.")

        st.subheader("Координация агента")
        cfg = fetch_json(f"/api/agents/config/{selected_host_id}")
        st.write(
            {
                "interval_seconds": cfg["interval_seconds"],
                "process_top_n": cfg["process_top_n"],
                "log_lines_limit": cfg["log_lines_limit"],
                "enabled": cfg["enabled"],
                "updated_at": cfg["updated_at"],
            }
        )

    st.subheader("Последние трассировки")
    if not selected_host_id:
        st.info("Трассировки появятся после регистрации хотя бы одного хоста (запустите агент на ВМ).")
    else:
        traces = fetch_json(f"/api/traces/{selected_host_id}", params={"limit": 20})
        if traces:
            st.dataframe(pd.DataFrame(traces), use_container_width=True, hide_index=True)
        else:
            st.info("Трассировки пока не поступали.")

    st.subheader("Активные алерты")
    alerts = fetch_json("/api/alerts", params={"active_only": "true"})
    if alerts:
        st.dataframe(pd.DataFrame(alerts), use_container_width=True, hide_index=True)
    else:
        st.success("Активных алертов нет.")

with tab_chat:
    st.subheader("Чат с агентом")
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "selected_host_id" not in st.session_state:
        st.session_state["selected_host_id"] = None

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    quick_query = ""
    selected_host_for_chat = st.session_state.get("selected_host_id")
    if selected_host_for_chat:
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Разбери инцидент по выбранному хосту", use_container_width=True):
                quick_query = f"Разбери инцидент по {selected_host_for_chat}"
        with c2:
            if st.button("Краткий RCA по выбранному хосту за 15 минут", use_container_width=True):
                quick_query = f"Сделай краткий RCA по {selected_host_for_chat} за 15 минут"

    user_text = st.chat_input("Спросите о состоянии инфраструктуры")
    query_text = quick_query or user_text or ""
    if query_text:
        st.session_state.messages.append({"role": "user", "content": query_text})
        with st.chat_message("user"):
            st.markdown(query_text)

        history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages[:-1]]
        with st.chat_message("assistant"):
            with st.spinner("Анализирую состояние..."):
                response = clean_assistant_text(asyncio.run(answer(query_text, history)))
            st.markdown(response)
        st.session_state.messages.append({"role": "assistant", "content": response})
