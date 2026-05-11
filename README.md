# VM Monitoring System

## 1. Описание
Распределённый мониторинг вычислительной инфраструктуры с трёхуровневой архитектурой: хост-агенты собирают метрики, логи и трассировки, слой координации управляет конфигурацией агентов, слой хранения и анализа предоставляет REST и MCP-инструменты для LLM-агента и оператора.

## 2. Архитектура

```text
+-------------------+        HTTP push        +----------------------+
| Host Agent(s)     | ----------------------> | Storage/Analysis     |
| metrics/logs/traces                          | FastAPI + PostgreSQL |
+---------+---------+                         +----------+-----------+
          |                                              ^
          | config pull                                  |
          v                                              |
+-------------------+                                     |
| Coordination      | ------------------------------------+
| agent config API  |
+-------------------+
          ^
          | MCP tools
          |
+-------------------+    MCP streamable HTTP  +----------------------+
| LLM Agent         | <----------------------> | MCP Server (9 tools) |
| Together/OpenAI API                          +----------+-----------+
+---------+---------+                                     |
          |                                               |
          +-------------------- UI Chat/Dashboard --------+
```

Слои:
- `agent/`: сбор живых метрик, логов, трассировок, локальная предобработка, push данных.
- `server/`: API приёма и выдачи данных, координация конфигурации агентов, хранение в PostgreSQL (при необходимости fallback SQLite), вычисление алертов.
- `mcp_server/`: экспорт 9 MCP инструментов для LLM.
- `llm_agent/`: tool-use loop OpenAI-compatible + MCP клиент.
- `ui/`: русскоязычный дашборд и чат с агентом.

## 3. Запуск
1. Создайте `.env` по примеру:
   - `cp .env.example .env` (Linux/macOS) или `copy .env.example .env` (Windows)
2. Укажите настройки Together AI в `.env`:
   - `OPENAI_BASE_URL=https://api.together.xyz/v1`
   - `OPENAI_API_KEY=...`
   - `OPENAI_MODEL=...`
3. Запустите core-сервисы:
   - `docker compose up --build server mcp_server ui`
4. Запустите агент на хосте/ВМ (рекомендуется):
   - Windows PowerShell (из каталога `vm-monitoring`, корень проекта):
     - `$env:SERVER_URL="http://<IP_с_сервером>:8000"`
     - опционально: `$env:HOST_ID` и `$env:HOST_NAME` (если не задать, `HOST_ID` будет `имя_ПК` + суффикс из UUID машины, `HOST_NAME` — `Windows (ИМЯ_ПК)`)
     - `.\scripts\agent-windows.ps1`
   - Windows без установленного Python: соберите exe (см. ниже). Для фона без консоли: `MonitoringAgentTray.exe` — иконка в трее, лог `%LOCALAPPDATA%\MonitoringAgent\agent.log`. Для отладки: `MonitoringAgent.exe` (консоль).
   - Linux (из каталога `vm-monitoring`, нужен `python3`):
     - `chmod +x scripts/run-agent-linux.sh`
     - `SERVER_URL=http://<IP_с_сервером>:8000 ./scripts/run-agent-linux.sh`
     - при необходимости задайте `HOST_ID` и `HOST_NAME` вручную, иначе `HOST_ID` будет `hostname` + фрагмент из `/etc/machine-id`.

Сборка exe агента на Windows (делается на машине разработчика, где есть Python):
- `.\scripts\agent-windows.ps1 -Build`
- результат: `dist\MonitoringAgent.exe` (консоль) и `dist\MonitoringAgentTray.exe` (трей, без окна); на целевой ВМ Python не нужен.
5. Для демо-режима контейнерного агента (не хостовые метрики):
   - `docker compose --profile demo-agent up --build agent`
6. Проверка:
   - Server health: [http://localhost:8000/health](http://localhost:8000/health)
   - UI: [http://localhost:8501](http://localhost:8501)
   - MCP endpoint: [http://localhost:8765/mcp](http://localhost:8765/mcp)

### База данных
- По умолчанию сервисы работают с PostgreSQL-контейнером (`postgres`) через `DATABASE_URL`.
- SQLite оставлен только как fallback для локальной совместимости.

## 4. Спецификация MCP-инструментов

| Tool | Description | Parameters |
|---|---|---|
| `list_hosts()` | Возвращает все хосты со статусом online/stale и метаданными. | none |
| `get_metrics()` | Временной ряд метрики с расчетом `min/max/avg/p95`. | `host_id`, `metric`, `since="5m"`, `until="now"` |
| `get_top_processes()` | Топ процессов из последнего среза. | `host_id`, `by="cpu\|memory"`, `n=10` |
| `get_recent_logs()` | Хвост настроенных файлов логов агента (не таблица трассировок, не поиск по trace_id). | `host_id`, `service=None`, `lines=100`, `level=None` |
| `get_recent_traces()` | Последние строки из таблицы `traces` в БД (как в UI). | `host_id`, `limit=50` |
| `get_trace_by_id()` | Точный поиск в `traces` по `trace_id`. | `host_id`, `trace_id` |
| `get_active_alerts()` | Активные пороговые алерты (не то же, что ERROR в логах). | `host_id=None` |
| `compare_hosts()` | Сравнение хостов по метрике, поиск аномалий по >2σ. | `host_ids`, `metric`, `since="15m"` |
| `diagnose_host()` | Снимок: метрики, процессы, алерты, последние ERROR в логах, последние 20 трассировок. | `host_id` |

Примечание: по умолчанию synthetic traces отключены. Для демо их можно включить переменной `ENABLE_SYNTHETIC_TRACES=1`.

### Переменные для демо и тестов
- `ALLOW_DEV_SEED_ALERTS=1` — включает dev endpoint для генерации тестовых алертов.
- `ENABLE_SYNTHETIC_TRACES=1` — включает synthetic traces для отладки/демо.
- В обычном рабочем режиме обе переменные оставляйте пустыми.

## 5. Демо-сценарии
1. Поднять стек: `docker compose up --build`.
2. Открыть [http://localhost:8501](http://localhost:8501) и убедиться, что виден хост ВМ.
3. Нагрузить CPU:
   - `python -c "while True: pass"`
4. В чате UI проверить:
   - `Что сейчас с моими серверами?`
   - `Какой процесс грузит CPU на Контейнер-1?`
   - `Сравни загрузку CPU между всеми хостами за последние 15 минут`
5. Подождать >60 секунд при `cpu_percent > 85` и убедиться, что алерт отображается в UI и ответах агента.
6. Проверить динамическую координацию агента:
   - `GET /api/agents/config/<host_id>`
   - `PUT /api/agents/config/<host_id>?interval_seconds=2&process_top_n=30`
7. Проверить трассировки:
   - `GET /api/traces/<host_id>?limit=20`

## 6. Структура проекта

Корень репозитория — каталог **`vm-monitoring/`** (ниже все пути относительно него).

```text
vm-monitoring/
├── agent/
│   ├── __init__.py
│   ├── main.py
│   ├── collectors.py
│   └── config.yaml
├── server/
│   ├── __init__.py
│   ├── main.py
│   ├── storage.py
│   ├── alerts.py
│   └── models.py
├── mcp_server/
│   ├── __init__.py
│   ├── main.py
│   └── tools.py
├── llm_agent/
│   ├── __init__.py
│   ├── main.py
│   └── system_prompt.py
├── ui/
│   └── streamlit_app.py
├── scripts/
│   ├── agent-windows.ps1
│   ├── agent-windows-build.cmd
│   ├── agent_windows_launcher.py
│   ├── agent_windows_tray_launcher.py
│   └── run-agent-linux.sh
├── data/
├── docker-compose.yml
├── Dockerfile.server
├── Dockerfile.ui
├── Dockerfile.agent
├── requirements.txt
├── requirements-agent-exe.txt
├── .env.example
└── README.md
```

## 7. Расширение
- Добавить новый MCP инструмент:
  1. Реализовать функцию в `mcp_server/tools.py`.
  2. Экспортировать как `@mcp.tool()` в `mcp_server/main.py` с качественным docstring.
  3. Перезапустить `mcp_server`.
- Добавить новую метрику:
  1. Собирать в `agent/collectors.py`.
  2. Передавать в payload `agent/main.py`.
  3. Сохранять и отдавать в `server/storage.py` и `server/main.py`.
  4. При необходимости включить в правила алертов `server/alerts.py` и в графики `ui/streamlit_app.py`.
- Добавить новый профиль координации:
  1. Расширить модель `agent_configs` в `server/storage.py`.
  2. Добавить поля в `GET/PUT /api/agents/config/{host_id}`.
  3. Применить параметры в цикле агента в `agent/main.py`.
