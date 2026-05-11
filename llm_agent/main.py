from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT
from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI, RateLimitError

from llm_agent.system_prompt import SYSTEM_PROMPT

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
ERROR_TEXT = "Произошла ошибка"
MAX_TOOL_TEXT_CHARS = 1200
MAX_TOOL_LIST_ITEMS = 20
MAX_TOOL_ITERATIONS = 4


def _clean_final_prefix(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.lower().startswith("final"):
        cleaned = cleaned[5:].lstrip(" :-\t\r\n")
    return cleaned.strip()


def sanitize_chat_output(text: str) -> str:
    s = (text or "").strip()
    low = s.lower()
    marker = "assistantfinal"
    i = low.rfind(marker)
    if i != -1:
        s = s[i + len(marker) :]
    s = re.sub(r"(?is)assistantanalysis.*?(?=assistantfinal|$)", "", s)
    s = re.sub(r"(?is)assistantcommentary.*?(?=assistantanalysis|assistantfinal|$)", "", s)
    s = re.sub(r"(?is)\bto=functions\.[a-z0-9_.]+\s+json\s*\{[^}]*\}\s*", "", s)
    s = re.sub(r"(?is)\bto=functions\.[a-z0-9_.]+", "", s)
    lines: list[str] = []
    for line in s.splitlines():
        t = line.strip().lower()
        if "assistantanalysis" in t or "assistantcommentary" in t:
            continue
        if "to=functions." in t:
            continue
        lines.append(line)
    s = "\n".join(lines).strip()
    return _clean_final_prefix(s)


def _serialize_tool_result(raw_content: Any) -> str:
    inner = _to_python_tool_result(raw_content)
    inner = _compact_tool_payload(inner)
    if isinstance(inner, str):
        return inner
    return json.dumps(inner, ensure_ascii=False, default=str)


def _compact_tool_payload(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return str(value)[:MAX_TOOL_TEXT_CHARS]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out[str(k)] = _compact_tool_payload(v, depth + 1)
        return out
    if isinstance(value, list):
        trimmed = value[:MAX_TOOL_LIST_ITEMS]
        return [_compact_tool_payload(v, depth + 1) for v in trimmed]
    if isinstance(value, str):
        s = value.strip()
        return s if len(s) <= MAX_TOOL_TEXT_CHARS else s[:MAX_TOOL_TEXT_CHARS] + "..."
    return value


def _to_python_tool_result(raw_content: Any) -> Any:
    if isinstance(raw_content, list):
        converted = []
        for item in raw_content:
            if hasattr(item, "model_dump"):
                converted.append(item.model_dump())
            elif hasattr(item, "dict"):
                converted.append(item.dict())
            elif hasattr(item, "__dict__"):
                converted.append(item.__dict__)
            else:
                converted.append(item)
        text_blocks = []
        for item in converted:
            if isinstance(item, dict) and "text" in item:
                block = str(item.get("text", "")).strip()
                if block:
                    text_blocks.append(block)
        if text_blocks:
            if len(text_blocks) == 1:
                try:
                    return json.loads(text_blocks[0])
                except json.JSONDecodeError:
                    return text_blocks[0]
            merged = "".join(text_blocks).strip()
            if merged:
                try:
                    return json.loads(merged)
                except json.JSONDecodeError:
                    parsed_chunks = []
                    for block in text_blocks:
                        try:
                            parsed_chunks.append(json.loads(block))
                        except json.JSONDecodeError:
                            parsed_chunks.append(block)
                    return parsed_chunks
        return converted
    if hasattr(raw_content, "model_dump"):
        return raw_content.model_dump()
    return raw_content


def _format_incident_card(data: dict[str, Any]) -> str:
    host = data.get("host") or {}
    host_name = host.get("name") or host.get("host_id") or "unknown"
    host_id = host.get("host_id") or "unknown"
    host_status = host.get("status") or "unknown"
    last_seen = host.get("last_seen") or "n/a"
    metrics = data.get("current_metrics", {}) or {}
    alerts = data.get("active_alerts", []) or []
    errors = data.get("recent_errors_in_logs", []) or []
    traces = data.get("recent_traces", []) or []
    top_cpu = data.get("top_cpu_processes", []) or []
    cpu_hot = isinstance(metrics.get("cpu_percent"), (int, float)) and float(metrics.get("cpu_percent", 0.0)) >= 85
    mem_hot = isinstance(metrics.get("memory_percent"), (int, float)) and float(metrics.get("memory_percent", 0.0)) >= 90
    disk_hot = isinstance(metrics.get("disk_percent"), (int, float)) and float(metrics.get("disk_percent", 0.0)) >= 90
    incident_confirmed = bool(data.get("incident_confirmed")) or bool(alerts or errors or cpu_hot or mem_hot or disk_hot)

    symptom_lines: list[str] = []
    if alerts:
        for row in alerts[:3]:
            symptom_lines.append(
                f"- alert: {row.get('metric')}={row.get('value')} (threshold {row.get('threshold')}, {row.get('severity')})"
            )
    else:
        symptom_lines.append("- активных алертов нет")
    for metric, threshold in (("cpu_percent", 85), ("memory_percent", 90), ("disk_percent", 90)):
        value = metrics.get(metric)
        if isinstance(value, (int, float)) and value >= threshold:
            symptom_lines.append(f"- {metric}={value} (выше {threshold})")
    if errors:
        row = errors[0]
        symptom_lines.append(
            f"- ERROR в логах: {row.get('ts')} {row.get('service')}: {row.get('message')}"
        )
    else:
        symptom_lines.append("- ERROR в логах не найдено")

    context_lines: list[str] = []
    if traces:
        for row in traces[:3]:
            context_lines.append(
                f"- trace: {row.get('ts')} | {row.get('trace_id')} | {row.get('operation')} | {row.get('status')}"
            )
    else:
        context_lines.append("- трассировки не поступали")

    probable_causes: list[str] = []
    if alerts:
        top_alert = alerts[0]
        probable_causes.append(
            f"- превышение порога по {top_alert.get('metric')} ({top_alert.get('severity')})"
        )
    if top_cpu and cpu_hot:
        top_proc = top_cpu[0]
        probable_causes.append(
            f"- высокая нагрузка от процесса {top_proc.get('name')} (pid {top_proc.get('pid')})"
        )
    if not probable_causes:
        probable_causes.append("- критичный инцидент по текущим данным не подтвержден")

    check_steps = [
        "- проверить тренд cpu/memory/disk за 15м через get_metrics",
        "- проверить последние ERROR/WARNING в логах сервиса через get_recent_logs",
        "- проверить последние трассировки и статус операций через get_recent_traces",
    ]
    if top_cpu:
        top_proc = top_cpu[0]
        check_steps.append(f"- проверить процесс {top_proc.get('name')} на хосте {host_id}")
    if alerts:
        check_steps.append("- убедиться, что после нормализации метрик алерт резолвится")

    lines = [
        f"Инцидент: {host_name} ({host_id})",
        f"Статус: {host_status}, last_seen={last_seen}",
        f"Подтверждение инцидента: {'да' if incident_confirmed else 'нет'}",
        "",
        "Симптомы:",
        *symptom_lines,
        "",
        "Контекст:",
        *context_lines,
        "",
        "Вероятная причина:",
        *probable_causes[:2],
        "",
        "Что проверить сейчас:",
        *check_steps[:5],
    ]
    return "\n".join(lines)


def _grounded_key(tool_name: str, data: Any) -> str:
    if tool_name == "get_metrics" and isinstance(data, dict):
        return f"{tool_name}:{data.get('host_id')}:{data.get('metric')}"
    if tool_name in {"get_recent_logs", "get_recent_traces", "get_trace_by_id", "diagnose_host"} and isinstance(data, dict):
        host = data.get("host_id") or ((data.get("host") or {}).get("host_id") if isinstance(data.get("host"), dict) else None)
        if host:
            return f"{tool_name}:{host}"
    return tool_name


def _render_grounded_response(tool_name: str, data: Any) -> str:
    if tool_name == "list_hosts":
        if isinstance(data, dict) and data.get("host_id"):
            rows = [data]
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        if not rows:
            return "Хосты не найдены."
        lines = ["Доступные хосты:"]
        for row in rows[:20]:
            ls = row.get("last_seen")
            lines.append(
                f"- {row.get('name')} ({row.get('host_id')}) — {row.get('status')}, last_seen={ls}"
            )
        return "\n".join(lines)
    if tool_name == "get_recent_traces":
        if isinstance(data, dict) and data.get("trace_id"):
            rows = [data]
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        if not rows:
            return "Трассировок по этому хосту сейчас нет."
        lines = ["Последние трассировки:"]
        for row in rows[:10]:
            lines.append(
                f"- {row.get('ts')} | {row.get('trace_id')} | {row.get('operation')} | "
                f"{row.get('status')} | {row.get('duration_ms')} ms"
            )
        return "\n".join(lines)
    if tool_name == "get_trace_by_id":
        if isinstance(data, dict) and data.get("trace_id"):
            rows = [data]
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        if not rows:
            return "Трассировка с указанным trace_id не найдена."
        lines = ["Найденные записи трассировки:"]
        for row in rows[:10]:
            lines.append(
                f"- {row.get('ts')} | {row.get('trace_id')} | {row.get('operation')} | "
                f"{row.get('status')} | {row.get('duration_ms')} ms"
            )
        return "\n".join(lines)
    if tool_name == "get_active_alerts":
        if isinstance(data, dict) and data.get("id") is not None:
            rows = [data]
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        if not rows:
            return "Активных алертов нет."
        lines = ["Активные алерты:"]
        for row in rows[:10]:
            lines.append(
                f"- {row.get('host_id')} | {row.get('metric')}={row.get('value')} "
                f"(threshold {row.get('threshold')}) | {row.get('severity')}"
            )
        return "\n".join(lines)
    if tool_name == "get_recent_logs":
        if isinstance(data, dict) and "message" in data:
            rows = [data]
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        if not rows:
            return "Подходящих строк логов не найдено."
        lines = ["Последние строки логов:"]
        for row in rows[:20]:
            lines.append(f"- {row.get('ts')} [{row.get('level')}] {row.get('service')}: {row.get('message')}")
        return "\n".join(lines)
    if tool_name == "get_metrics":
        if isinstance(data, dict):
            stats = data.get("stats", {})
            return (
                f"Метрика {data.get('metric')} для {data.get('host_id')}: "
                f"min={stats.get('min')}, max={stats.get('max')}, avg={stats.get('avg')}, p95={stats.get('p95')}."
            )
    if tool_name == "diagnose_host" and isinstance(data, dict):
        return _format_incident_card(data)
    return ""


def _pick_host_id(user_query: str, hosts: list[dict[str, Any]]) -> str | None:
    q = (user_query or "").lower()
    for row in hosts:
        host_id = str(row.get("host_id") or "")
        name = str(row.get("name") or "")
        if host_id and host_id.lower() in q:
            return host_id
        if name and name.lower() in q:
            return host_id or None
    if hosts:
        return str(hosts[0].get("host_id") or "") or None
    return None


async def _answer_without_llm(user_query: str, session: ClientSession) -> str:
    q = (user_query or "").lower()
    hosts_result = await session.call_tool("list_hosts", {})
    hosts_data = _to_python_tool_result(hosts_result.content)
    if isinstance(hosts_data, list):
        hosts = hosts_data
    elif isinstance(hosts_data, dict):
        hosts = [hosts_data]
    else:
        hosts = []
    if not hosts:
        return "Хосты пока не зарегистрированы."

    host_id = _pick_host_id(user_query, hosts)
    if not host_id:
        return "Не удалось определить хост."

    if ("пик" in q or "макс" in q or "максим" in q) and "cpu" in q:
        metrics_result = await session.call_tool(
            "get_metrics",
            {"host_id": host_id, "metric": "cpu_percent", "since": "15m", "until": "now"},
        )
        data = _to_python_tool_result(metrics_result.content)
        if isinstance(data, dict):
            stats = data.get("stats", {}) if isinstance(data.get("stats"), dict) else {}
            points = data.get("points", []) if isinstance(data.get("points"), list) else []
            peak = stats.get("max")
            if peak is None and points:
                nums = [p.get("value") for p in points if isinstance(p, dict) and isinstance(p.get("value"), (int, float))]
                peak = max(nums) if nums else None
            if peak is not None:
                return f"Пиковый CPU по {host_id} за 15 минут: {peak}%."
        return "Недостаточно данных по CPU."

    if "трасс" in q or "trace" in q:
        traces_result = await session.call_tool("get_recent_traces", {"host_id": host_id, "limit": 20})
        traces_data = _to_python_tool_result(traces_result.content)
        rendered = _render_grounded_response("get_recent_traces", traces_data)
        return rendered or "Трассировок по этому хосту сейчас нет."

    if "лог" in q or "ошиб" in q:
        logs_result = await session.call_tool("get_recent_logs", {"host_id": host_id, "lines": 50, "level": "ERROR"})
        logs_data = _to_python_tool_result(logs_result.content)
        rendered = _render_grounded_response("get_recent_logs", logs_data)
        return rendered or "Подходящих строк логов не найдено."

    diagnose_result = await session.call_tool("diagnose_host", {"host_id": host_id})
    diagnose_data = _to_python_tool_result(diagnose_result.content)
    rendered = _render_grounded_response("diagnose_host", diagnose_data)
    return rendered or "Не удалось собрать сводку по хосту."


async def _fallback_answer_via_tools(user_query: str, mcp_url: str) -> str:
    mcp_timeout = httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=mcp_timeout,
        trust_env=False,
    ) as mcp_http:
        async with streamable_http_client(
            mcp_url,
            http_client=mcp_http,
            terminate_on_close=False,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await _answer_without_llm(user_query, session)


def _sync_chat_completion(
    llm: OpenAI,
    model: str,
    messages: list[dict[str, Any]],
    openai_tools: list[dict[str, Any]],
):
    total_chars = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
    logger.info("LLM request: messages=%s content_chars=%s", len(messages), total_chars)
    return llm.chat.completions.create(
        model=model,
        messages=messages,
        tools=openai_tools,
        tool_choice="auto",
        temperature=0.2,
    )


def _synthesize_from_tool_data_sync(
    llm: OpenAI,
    model: str,
    user_query: str,
    tool_records: list[dict[str, Any]],
) -> str:
    payload = json.dumps(tool_records, ensure_ascii=False)
    synth_prompt = (
        "Ты SRE-ассистент для оператора инфраструктуры. Тебе даны только факты из JSON результатов инструментов.\n"
        "Сформируй итоговый ответ на русском:\n"
        "1) краткий вывод,\n"
        "2) ключевые факты,\n"
        "3) конкретные рекомендации.\n"
        "Инцидент считай подтвержденным только при наличии активных алертов, ERROR-логов "
        "или превышении порогов cpu>=85, memory>=90, disk>=90.\n"
        "Трассировки с synthetic=true не использовать как единственное доказательство инцидента.\n"
        "Если условий нет, явно напиши что критичный инцидент не подтвержден.\n"
        "Не советуй менять исходный код мониторинга, репозиторий, Docker или «заменить агент приложения». "
        "Рекомендации только по хосту и сервисам пользователя (процессы, службы, диск, сеть, логи приложений, эскалация).\n"
        "Запрещено добавлять факты, которых нет в JSON."
    )
    response = llm.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": synth_prompt},
            {"role": "user", "content": f"Запрос пользователя: {user_query}\n\nJSON факты:\n{payload}"},
        ],
    )
    text = sanitize_chat_output(response.choices[0].message.content or "")
    return text.strip()


async def _answer_with_session(
    user_query: str,
    history: list[dict[str, Any]] | None,
    llm: OpenAI,
    model: str,
    session: ClientSession,
) -> str:
    await session.initialize()
    mcp_tools = await session.list_tools()
    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            },
        }
        for t in mcp_tools.tools
    ]
    hosts_context = ""
    try:
        hosts_result = await session.call_tool("list_hosts", {})
        hosts_context = _serialize_tool_result(hosts_result.content)
    except Exception:
        hosts_context = ""

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if hosts_context:
        messages.append(
            {
                "role": "system",
                "content": f"Доступные хосты (используй host_id строго из этого списка): {hosts_context}",
            }
        )
    for msg in history or []:
        if msg.get("role") in {"user", "assistant"}:
            messages.append({"role": msg["role"], "content": str(msg.get("content", ""))})
    messages.append({"role": "user", "content": user_query})
    last_tool_name = ""
    last_tool_data: Any = None
    non_discovery_tool_used = False
    grounded_chunks: dict[str, str] = {}
    grounded_order: list[str] = []
    incident_card: str = ""
    tool_records: list[dict[str, Any]] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        response = await asyncio.to_thread(_sync_chat_completion, llm, model, messages, openai_tools)
        choice = response.choices[0].message
        tool_calls = choice.tool_calls or []

        if not tool_calls:
            grounded_fallback = ""
            if incident_card:
                grounded_fallback = incident_card
            elif grounded_order:
                grounded_fallback = "\n\n".join(grounded_chunks[k] for k in grounded_order)
            if tool_records:
                try:
                    synthesized = await asyncio.to_thread(
                        _synthesize_from_tool_data_sync, llm, model, user_query, tool_records
                    )
                    if synthesized:
                        return synthesized
                except Exception:
                    pass
            if grounded_fallback:
                return grounded_fallback
            grounded = _render_grounded_response(last_tool_name, last_tool_data)
            if grounded:
                return grounded
            if not non_discovery_tool_used:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Ты пока вызвал только discovery-инструменты. "
                            "Сделай минимум один профильный вызов для ответа по сути "
                            "(например get_recent_traces/get_trace_by_id/get_recent_logs/"
                            "get_metrics/get_active_alerts/diagnose_host/get_top_processes)."
                        ),
                    }
                )
                continue
            text = sanitize_chat_output(choice.content or "")
            return text.strip() or "Не удалось сформировать ответ."

        assistant_message = {
            "role": "assistant",
            "content": choice.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        }
        messages.append(assistant_message)

        for tc in tool_calls:
            tool_name = tc.function.name
            tool_args = json.loads(tc.function.arguments or "{}")
            result = await session.call_tool(tool_name, tool_args)
            last_tool_name = tool_name
            last_tool_data = _to_python_tool_result(result.content)
            tool_records.append(
                {"tool": tool_name, "arguments": tool_args, "result": last_tool_data}
            )
            if tool_name != "list_hosts":
                non_discovery_tool_used = True
            snip = _render_grounded_response(tool_name, last_tool_data)
            if snip:
                if tool_name == "diagnose_host":
                    incident_card = snip
                else:
                    key = _grounded_key(tool_name, last_tool_data)
                    if key not in grounded_chunks:
                        grounded_order.append(key)
                    grounded_chunks[key] = snip
            tool_content = _serialize_tool_result(result.content)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tool_name,
                    "content": tool_content,
                }
            )

    grounded_fallback = ""
    if incident_card:
        grounded_fallback = incident_card
    elif grounded_order:
        grounded_fallback = "\n\n".join(grounded_chunks[k] for k in grounded_order)
    if tool_records:
        try:
            synthesized = await asyncio.to_thread(
                _synthesize_from_tool_data_sync, llm, model, user_query, tool_records
            )
            if synthesized:
                return synthesized
        except Exception:
            pass
    if grounded_fallback:
        return grounded_fallback
    return "Превышен лимит итераций."


def _first_exception(exc: BaseException) -> BaseException:
    if isinstance(exc, BaseExceptionGroup):
        for item in exc.exceptions:
            return _first_exception(item)
    return exc


def _flatten_exception_tree(root: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    stack: list[BaseException] = [root]
    seen: set[int] = set()
    while stack:
        cur = stack.pop()
        i = id(cur)
        if i in seen:
            continue
        seen.add(i)
        out.append(cur)
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        else:
            if cur.__cause__ is not None:
                stack.append(cur.__cause__)
            ctx = cur.__context__
            if ctx is not None and ctx is not cur.__cause__:
                stack.append(ctx)
    return out


def _openai_user_message_from_group(eg: BaseExceptionGroup, base_url: str, model: str) -> str | None:
    flat = _flatten_exception_tree(eg)
    for e in flat:
        if isinstance(e, AuthenticationError):
            logger.exception("OPENAI_API_KEY rejected base_url=%s", base_url)
            return ERROR_TEXT
    for e in flat:
        if isinstance(e, RateLimitError):
            logger.warning("LLM rate limit: %s", e)
            return ERROR_TEXT
    for e in flat:
        if isinstance(e, APIStatusError) and not isinstance(e, (AuthenticationError, RateLimitError)):
            logger.exception("LLM HTTP %s base_url=%s", e.status_code, base_url)
            return ERROR_TEXT
    for e in flat:
        if isinstance(e, APIConnectionError):
            logger.exception("LLM API connection (inside ExceptionGroup) base_url=%s model=%s", base_url, model)
            return ERROR_TEXT
    return None


async def answer(user_query: str, history: list[dict[str, Any]] | None = None) -> str:
    mcp_url = (os.getenv("MCP_URL") or "").strip()
    model = (os.getenv("OPENAI_MODEL") or "").strip()
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    base_url = (os.getenv("OPENAI_BASE_URL") or "").strip()
    missing = [n for n, v in (
        ("MCP_URL", mcp_url),
        ("OPENAI_MODEL", model),
        ("OPENAI_API_KEY", api_key),
        ("OPENAI_BASE_URL", base_url),
    ) if not v]
    if missing:
        logger.error("LLM chat: missing env %s", ", ".join(missing))
        return ERROR_TEXT
    llm_timeout = httpx.Timeout(120.0, connect=30.0, read=90.0)
    llm_http = httpx.Client(
        timeout=llm_timeout,
        follow_redirects=True,
        trust_env=False,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    llm = OpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=llm_http,
        timeout=120.0,
        max_retries=2,
    )
    impl_out: str | None = None
    mcp_timeout = httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=mcp_timeout,
            trust_env=False,
        ) as mcp_http:
            async with streamable_http_client(
                mcp_url,
                http_client=mcp_http,
                terminate_on_close=False,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    impl_out = await _answer_with_session(user_query, history, llm, model, session)
    except AuthenticationError:
        if impl_out is not None:
            return impl_out
        logger.exception("OPENAI_API_KEY rejected base_url=%s", base_url)
        try:
            return await _fallback_answer_via_tools(user_query, mcp_url)
        except Exception:
            return ERROR_TEXT
    except RateLimitError:
        if impl_out is not None:
            return impl_out
        logger.warning("LLM rate limit")
        try:
            return await _fallback_answer_via_tools(user_query, mcp_url)
        except Exception:
            return ERROR_TEXT
    except APIStatusError as e:
        if impl_out is not None:
            return impl_out
        logger.exception("LLM HTTP %s base_url=%s", e.status_code, base_url)
        try:
            return await _fallback_answer_via_tools(user_query, mcp_url)
        except Exception:
            return ERROR_TEXT
    except APIConnectionError:
        if impl_out is not None:
            return impl_out
        logger.exception("LLM API connection base_url=%s model=%s", base_url, model)
        try:
            return await _fallback_answer_via_tools(user_query, mcp_url)
        except Exception:
            return ERROR_TEXT
    except BaseExceptionGroup as eg:
        if impl_out is not None:
            logger.warning("MCP session cleanup raised (response kept): %s", eg)
            return impl_out
        openai_msg = _openai_user_message_from_group(eg, base_url, model)
        if openai_msg is not None:
            try:
                return await _fallback_answer_via_tools(user_query, mcp_url)
            except Exception:
                return openai_msg
        sub = _first_exception(eg)
        logger.exception("MCP_URL=%s underlying=%s", mcp_url, sub)
        return ERROR_TEXT
    except Exception as e:
        if impl_out is not None:
            logger.warning("MCP session cleanup raised (response kept): %s", e)
            return impl_out
        logger.exception("chat failure MCP_URL=%s", mcp_url)
        return ERROR_TEXT
    finally:
        try:
            if not llm.is_closed:
                llm.close()
        except Exception:
            logger.warning("OpenAI client close failed", exc_info=True)

    return impl_out if impl_out is not None else ERROR_TEXT


def main() -> None:
    query = input("Введите запрос: ").strip()
    response = asyncio.run(answer(query))
    print(response)


if __name__ == "__main__":
    main()
