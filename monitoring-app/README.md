# AI Monitoring

Мониторинг локальной LLM-инфраструктуры: **GPU (NVIDIA)**, **vLLM**,
**Open WebUI** — метрики системы, загрузка GPU, лог запросов пользователей
и алерты в Telegram.

Стек: FastAPI + PostgreSQL (TimescaleDB-совместимо) + Redis +
vanilla-JS фронтенд (Vite + Chart.js).

## Возможности

- **Система**: CPU (по ядрам, load), RAM/swap, диск (I/O + занятость), сеть.
- **GPU**: утилизация, температура, VRAM, мощность — через NVML
  (`pynvml`) или `nvidia-smi` (если NVML недоступен).
- **vLLM**: активные/ожидающие запросы, KV-cache, prefix cache, TTFT,
  TPOT, E2E latency, токены/сек — из Prometheus-метрик `/metrics`.
- **Запросы пользователей**:
  - из API Open WebUI (по админ-токену: чаты, модель, токены, latency),
  - из access-лога прокси/OWUI (опционально, `LOG_FILE`),
  - тестовые запросы прямо с дашборда (`/api/test-request`).
- **Алерты в Telegram**: GPU выше порога, ошибка запросов выше порога
  (пороги настраиваются в админке, кулдаун 15 мин на каждое сообщение).
- **Ретеншн**: очистка старых снапшотов и prompt-превью (по дням, из env).
- **Часовая пред-агрегация** для быстрого графика за 7/30 дней.
- Кэш ответов в Redis с деградацией при недоступности.
- TimescaleDB (hypertable `metrics_snapshot`) — опционально, работает и с
  обычным PostgreSQL.

## Быстрый старт

```bash
cd monitoring-app
cp .env.example .env          # заполнить URL vLLM/Open WebUI, токен, пароль
npm --prefix frontend install
npm --prefix frontend run build
docker compose up -d --build
# дашборд: http://<host>:3000  (логин/пароль из ADMIN_USER/MONITORING_PASSWORD)
```

Если GPU в другой машине/сети — укажите её IP в `VLLM_API_URL`/
`OPEN_WEBUI_URL` (по умолчанию `172.17.0.1` — Docker host из контейнера).
Для доступа к GPU из контейнера добавьте в сервис `backend`:

```yaml
    devices:
      - /dev/nvidia0
      - /dev/nvidiactl
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

## Архитектура

```
┌─────────────┐   ┌──────────────────────────────┐   ┌──────────────┐
│   vLLM      │   │        backend (FastAPI)     │   │ PostgreSQL   │
│  :8000      ├──►│  collector (каждые N сек):   ├──►│  (+Timescale)│
├─────────────┤   │   система / GPU / vLLM /     │   ├──────────────┤
│ Open WebUI  │   │   сервисы-пинги / запросы /  │   │ Redis (кэш)  │
│  :8080      ├──►│   аггрегация / очистка /     │   └──────────────┘
└─────────────┘   │   алерты (Telegram)          │
                  │  API + статика (frontend)    │──►  браузер
                  └──────────────────────────────┘
```

Коллектор может работать внутри контейнера бэкенда (`RUN_COLLECTOR=true`)
или отдельно: `python -m collector` из папки `backend/`.

## Переменные окружения

См. [.env.example](.env.example). Ключевые:

| Переменная | По умолчанию | Описание |
|---|---|---|
| `VLLM_API_URL` | `http://172.17.0.1:8000` | API vLLM |
| `OPEN_WEBUI_URL` | `http://172.17.0.1:8080` | Open WebUI |
| `OPEN_WEBUI_API_TOKEN` | — | админ-API-ключ OWUI (лог запросов) |
| `COLLECT_INTERVAL` | `10` | период сбора, сек |
| `RUN_COLLECTOR` | `true` | коллектор внутри бэкенда |
| `ADMIN_USER` / `MONITORING_PASSWORD` | `admin`/`admin` | HTTP Basic |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | алерты |
| `LOG_FILE` | — | access-лог прокси (опционально) |
| `PROMPT_RETENTION_DAYS` | `7` | хранимость prompt-превью |
| `SNAPSHOTS_RETENTION_DAYS` | `30` | хранимость снапшотов |

## API

| Метод | Путь | Auth | Описание |
|---|---|---|---|
| GET | `/api/health` | нет | версия, uptime-инфо |
| GET | `/api/latest` | да | последний снапшот + статусы сервисов |
| GET | `/api/history?metric=&range=` | да | история: `1h/6h/24h/3d/7d/30d` |
| GET | `/api/requests?limit=&offset=&q=&user=&model=&status=` | нет | лог запросов |
| GET | `/api/status` | нет | статусы сервисов |
| POST | `/api/status/{name}/restart` | да | vllm/openwebui/db/redis |
| POST | `/api/test-request` | да | тестовый запрос в vLLM |
| POST | `/api/admin/notify-test` | да | тест Telegram-алерта |
| GET/PUT | `/api/settings` | да (PUT) | пороги GPU/ошибок, уведомления |
| GET | `/api/actions` | нет | журнал действий администратора |

Метрики для `/api/history`: `gpu_util`, `gpu_temp`, `cpu_pct`, `ram_pct`,
`net_rx`, `net_tx`, `disk_read`, `disk_write`, `vllm_active`,
`vllm_tokens_in`, `vllm_tokens_out`, `vllm_ttft`, `vllm_tpot`.

## Фронтенд

Ванильный JS + Chart.js, сборка Vite в `frontend/dist` (монтируется в
контейнер бэкенда). Вкладки: **Дашборд** (карточки, график, сервисы),
**Запросы** (фильтры, пагинация, тестовый запрос), **Админка** (настройки,
тест уведомлений, журнал действий). Авторизация — HTTP Basic.

```bash
npm --prefix frontend install
npm --prefix frontend run build   # → frontend/dist
npm --prefix frontend run dev     # dev-сервер с прокси на :3000
```
