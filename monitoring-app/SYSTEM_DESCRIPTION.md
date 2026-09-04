# AI-инфраструктура: полное описание системы (для внешнего анализа)

Стек: FastAPI + SQLAlchemy + PostgreSQL(TimescaleDB) + Redis + фронтенд
(Vite + Chart.js, без UI-фреймворка). Один docker-compose.
Цель — дашборд мониторинга GPU/CPU/RAM/диск/сеть/vLLM + шлюз (gateway)
управления API-ключами перед vLLM.

## 1. Компоненты и сети

```
                      ┌───────────────────────────────────────────────┐
                      │          Docker-хост (GPU-сервер)             │
                      │                                               │
 Cline/клиенты        │ ┌───────────────┐   httpx   ┌──────────────┐ │
 ─────────────────────┼▶│   backend     ├──────────▶│   vLLM       │ │
 :3000 (public)       │ │   FastAPI     │ 172.17.0.1│   :8000      │ │
 /v1/chat/completions │ │   gateway +   │           │ (host-level, │ │
 /api/*  / (frontend) │ │   monitoring  │           │  вне compose)│ │
                      │ └──┬───────┬────┘           └──────────────┘ │
                      │    │SQL    │Redis                    ▲       │
                      │ ┌──▼───┐ ┌─▼─────┐   ┌───────────────┴────┐  │
                      │ │  db  │ │ redis │   │  Open WebUI :8080  │  │
                      │ │ pg16 │ │ :6379 │   │  (только мониторинг)│ │
                      │ └──────┘ └───────┘   └────────────────────┘  │
                      │  collector — async-задача ВНУТРИ backend     │
                      │  (psutil, NVML, vLLM /metrics, OWUI API)     │
                      └───────────────────────────────────────────────┘
```

- `backend` — единственный сервис с публичным портом (`APP_PORT:3000`).
  Внутри: API мониторинга, статика фронтенда, gateway-прокси vLLM,
  фоновый `collector` (RUN_COLLECTOR=true).
- `db` — timescale/timescaledb-ha:pg16; user/pass/db = monitoring.
  Порт наружу **не проброшен**.
- `redis` — redis:7-alpine, только внутри compose-сети.
- vLLM и Open WebUI — контейнеры **вне compose** на хосте; backend
  ходит к ним через `172.17.0.1` (docker0 bridge):
  VLLM_API_URL=http://172.17.0.1:8000, OPEN_WEBUI_URL=http://172.17.0.1:8080.

## 2. Файлы проекта

```
monitoring-app/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── Dockerfile             # python:3.11-slim + uvicorn
│   ├── requirements.txt       # fastapi, uvicorn, sqlalchemy, psycopg2,
│   │                          # redis, httpx, psutil, pynvml
│   ├── config.py              # Config: все env-var (§7)
│   ├── database.py            # engine, SessionLocal, init_db()=create_all
│   ├── models.py              # ORM: MetricSnapshot, ServiceStatus,
│   │                          # RequestLog, HourlyAgg, Setting,
│   │                          # AdminAction, ApiKey, ApiUsageLog
│   ├── auth.py                # HTTP Basic (ADMIN_USER/MONITORING_PASSWORD)
│   ├── cache.py               # Redis-кэш, fail-open
│   ├── apiproxy.py            # генерация ключей, SHA256, лимиты в Redis
│   ├── collector.py           # цикл сбора метрик (каждые 10с)
│   ├── notifier.py            # Telegram-алерты
│   ├── main.py                # FastAPI app (все эндпоинты, §5)
│   └── migrations/002_api_keys.sql   # опциональная явная миграция
├── frontend/
│   ├── index.html             # вкладки: Обзор / Запросы / API-ключи / Админка
│   └── src/{main.js,app.js,api.js,style.css}

## 3. Схема БД (PostgreSQL / TimescaleDB)

```sql
-- ===== мониторинг (существующее) =====
metric_snapshots (
  id BIGSERIAL PK, ts TIMESTAMPTZ,
  gpu  JSONB,   -- [{util, temp, mem_used, mem_total, power, ...}]
  cpu  JSONB,   -- {pct}
  ram  JSONB,   -- {pct, used, total, ...}
  disk JSONB, net JSONB,
  vllm JSONB    -- {active, tokens_in_s, tokens_out_s, ttft_ms, tpot_ms,
                --  kv_cache, version, ...} — из Prometheus-метрик vLLM
)  -- одна строка на интервал сбора (10с)
service_status (name PK, up BOOL, latency_ms INT, version, last_ok_ts, last_check_ts)
request_logs   (id, ts, source, user_id, model, prompt_preview, latency_ms,
                status, temperature, raw JSONB)   -- из OWUI + тесты дашборда
hourly_aggs    (hour_bucket, *avg по метрикам, requests, errors)  -- преагрегация
settings       (key PK, value)         -- пороги алертов, настройки UI
admin_actions  (id, ts, user, action, details JSONB)  -- аудит

-- ===== НОВОЕ: gateway API-ключей =====
api_keys (
  id CHAR(36) PK,                    -- UUIDv4, генерится в Python (uuid.uuid4)
  name VARCHAR(255) NOT NULL,        -- пользовательское название
  key_hash VARCHAR(255) UNIQUE NOT NULL, -- SHA256(полный ключ); сам ключ НИКОГДА
  prefix VARCHAR(20),                -- первые 6 символов (sk-abc…)
  created_at TIMESTAMPTZ, last_used_at TIMESTAMPTZ,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  rate_limit INT DEFAULT 60,             -- запросов/минуту (Redis-счётчик)
  daily_token_limit INT DEFAULT 1000000, -- токенов/день (Redis-счётчик)
  total_requests INT DEFAULT 0,          -- денормализованные счётчики
  total_tokens INT DEFAULT 0,
  total_latency_ms BIGINT DEFAULT 0      -- сумма длительностей (→ ср. скорость/задержка)
)
api_usage_logs (
  id CHAR(36) PK,
  api_key_id CHAR(36) REFERENCES api_keys(id) ON DELETE CASCADE,
  request_time TIMESTAMPTZ,
  input_tokens INT, output_tokens INT, total_tokens INT,
  endpoint VARCHAR(255), status_code INT, ip_address VARCHAR(45),
  latency_ms INT                        -- длительность запроса через gateway, мс
)
-- индексы: api_keys.key_hash (unique), api_usage_logs.api_key_id, .request_time
```

Свойства:
- UUID — CHAR(36) приложением (не gen_random_uuid), portability pg/timescale.
- Таблицы создаются сами при старте backend (`Base.metadata.create_all`);
  `migrations/002_api_keys.sql`, `migrations/003_api_key_latency.sql` —
  опциональные явные миграции (IF NOT EXISTS). `create_all` не добавляет
  колонки в существующие таблицы, поэтому `init_db()` дополнительно
  выполняет идемпотентные `ALTER TABLE … ADD COLUMN IF NOT EXISTS`
  (latency_ms, total_latency_ms).
- `api_usage_logs` — обычная таблица, НЕ hypertable (при желании:
  `SELECT create_hypertable('api_usage_logs','request_time',
   chunk_time_interval => interval '7 days')`).

## 4. Ключи Redis

```
apikey:rl:{key_id}:{min_epoch}   INCR, expire 70с      — лимит запросов/мин
apikey:tok:{key_id}:{YYYYMMDD}   INCRBY, expire 2 дня  — лимит токенов/день
latest, history:* и пр.                                   — кэши API (TTL ≤ 10с)
```

Всё в gateway — **fail-open**: при недоступном Redis запрос пропускается
(лимит не применяется). Трафик vLLM никогда не роняется из-за Redis.

## 5. HTTP-эндпоинты backend (:3000)

| Эндпоинт | Auth | Назначение |
|---|---|---|
| GET `/api/health` | — | живость, версия, flag timescale |
| GET `/api/latest` | Basic | свежий срез метрик (кэш 10с → иначе БД) |
| GET `/api/history?metric=&range=` | Basic | таймсерия (24h/7d/30d) |
| GET `/api/requests?limit=` | Basic | логи запросов (OWUI + тесты) |
| POST `/api/admin/test-request` | Basic | тест vLLM через backend (пишет request_logs) |
| POST `/api/admin/restart/{svc}` | Basic | рестарт контейнера vLLM/OWUI |
| GET/PUT `/api/settings` | Basic | настройки/пороги алертов |
| GET `/api/actions?limit=` | Basic | аудит действий админа |
| POST `/api/admin/notify-test` | Basic | тест Telegram-алерта |
| **GET `/api/keys`** | Basic | список ключей (без секрета; метаданные + счётчики) |
| **POST `/api/keys`** | Basic | создать: `{name, rate_limit, daily_token_limit, master_password}`; неверный мастер-пароль → 401; ответ `{key: sk-…}` — ЕДИНСТВЕННЫЙ раз, когда полный ключ виден |
| **POST `/api/keys/{id}/block`** / **`unblock`** | Basic | блокировка / разблокировка |
| **DELETE `/api/keys/{id}`** | Basic | удалить ключ (+ каскад его usage-логов) |
| **GET `/api/keys/{id}/stats`** | Basic | daily tokens/requests за 7 дней (миниграфик) |
| **GET `/api/keys/{id}/usage?limit=`** | Basic | последние N запросов ключа |
| **GET `/api/keys/summary`** | Basic | сводка: итоги по всем ключам, today, 7-д series, per-key, URL прокси |
| **POST `/v1/chat/completions`** | **Bearer API-ключ** | GATEWAY (§6) |
| GET `/{path}` | — | статика фронтенда (SPA fallback → index.html) |

Auth: `/api/*` — HTTP Basic (ADMIN_USER / MONITORING_PASSWORD, дефолт
admin/admin), кроме `/api/health`. `/v1/*` — Bearer API-ключ (SHA256 в БД).

## 6. Gateway: POST /v1/chat/completions (детальный поток)

```
1. raw_body = await request.body()
2. key = SELECT * FROM api_keys WHERE key_hash = sha256(Bearer-токен)
      нет строки / is_active = FALSE  → 401 {"error":{"message":"Invalid API key"}}
3. rate limit:  Redis INCR apikey:rl:{id}:{мин} > rate_limit?      → 429 Rate limit exceeded
4. token limit: Redis GET apikey:tok:{id}:{день} >= daily_token_limit? → 429 Daily token limit exceeded
5. stream = body.stream
   ├─ NO (non-stream):
   │    httpx POST {VLLM_API_URL}/v1/chat/completions
   │    headers: без Authorization клиента; если VLLM_API_KEY задан,
   │             подставляем "Authorization: Bearer {VLLM_API_KEY}"
   │    in/out = _extract_usage(response.usage):
    │             in  = prompt_tokens − cached_tokens  # только НОВОЕ в промпте
    │                  (cached_tokens = usage.prompt_tokens_details.cached_tokens,
    │                   prefix-кэш vLLM; если отсутствует → in = prompt_tokens)
    │             out = completion_tokens
   │    fallback _estimate_tokens() ≈ len(текст)/4, если usage пуст
   │    → _record_usage (§6.1); вернуть ответ vLLM как есть (статус, body, content-type)
   │    vLLM недоступен → 502 {"error":{"message":"vLLM unreachable: …"}} (usage пишется)
   └─ YES (stream):
        body.setdefault('stream_options',{})['include_usage'] = True
        # ВАЖНО: без этого vLLM по умолчанию НЕ шлёт usage в SSE-чанках
        #   → токены всегда 0. Это ключевая деталь для учёта.
        StreamingResponse: httpx.stream → по байтам yield клиенту;
        параллельно парсинг SSE-строк "data:…":
           - "usage" в чанке (финальный чанк благодаря include_usage) → точные
             in/out тем же _extract_usage, что и в non-stream
           - choices[].delta.content → сборка completion (для fallback-оценки)
        finally: если in==0 и out==0 → _estimate_tokens(prompt, completion)
        → _record_usage (§6.1)
6.1 _record_usage(key_id, status, in, out, endpoint, ip):
     - отдельная сессия БД:
         api_keys.total_requests += 1
         api_keys.total_tokens   += in + out
         api_keys.last_used_at    = now()
         INSERT api_usage_logs(...)
     - Redis INCRBY apikey:tok:{id}:{день} total
     - log.info("vllm-proxy usage: key=… status=… in=… out=… total=… ip=…")
     - исключение НЕ роняет запрос (только log.warning)
```

Формат ключа: `sk-` + 48 hex (`secrets.token_hex(24)`). В БД — только SHA256.

**Семантика «in» (input tokens).** Чат-клиенты шлют ВЕСЬ диалог с каждым
запросом, поэтому сырой `prompt_tokens` — размер всего контекста (растёт с
каждым ответом модели), и суммы по ключам визуально «раздуваются»
(сотни тысяч токенов за несколько минут). Поэтому «in» =
`prompt_tokens − cached_tokens`: только новая (не кэшированная) часть
промпта. `cached_tokens` отдаёт vLLM в `prompt_tokens_details`, если у него
включён automatic prefix cache (по умолчанию включён в актуальных версиях).
Если vLLM запущен без `--enable-prefix-caching`, `cached_tokens`
отсутствует/равен 0, и «in» остаётся полным `prompt_tokens` — это честный
объём обработанного промпта.

### Безопасность (2 уровня)
1. **Gateway-уровень**: проверка клиентского API-ключа + лимиты (выше).
2. **vLLM-уровень (опционально)**: vLLM стартует с `--api-key <secret>`
   и `--host 127.0.0.1` — наружу недоступен; backend шлёт туда секрет
   из `VLLM_API_KEY`. Тогда vLLM отвечает ТОЛЬКО backend'у, т.е.
   «vLLM отдаёт ответы только по зарегистрированным ключам» обеспечивается
   полностью (двойная защита).

## 7. Переменные окружения (backend)

```
DATABASE_URL          postgresql+psycopg2://monitoring:monitoring@db:5432/monitoring
REDIS_URL             redis://redis:6379/0
VLLM_API_URL          http://172.17.0.1:8000   # куда проксирует gateway
OPEN_WEBUI_URL        http://172.17.0.1:8080   # источник логов запросов
OPEN_WEBUI_API_TOKEN                                 # (для чтения OWUI-логов)
VLLM_API_KEY                                          # опц. секрет vLLM (2-й уровень защиты)
MASTER_PASSWORD     apiopenlabs          # для POST /api/keys
ADMIN_USER / MONITORING_PASSWORD  admin/admin   # Basic auth дашборда
COLLECT_INTERVAL    10                   # период сбора метрик, сек
PROMPT_RETENTION_DAYS 7 / SNAPSHOTS_RETENTION_DAYS 30
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID            # алерты
RUN_COLLECTOR       true
APP_PORT            3000
```

## 8. Поток мониторинга (collector, каждые 10с, внутри backend)

```
psutil (CPU/RAM/диск/сеть) ─┐
NVML / nvidia-smi (GPU)    ─┼→ metric_snapshots (JSONB-поля)
vLLM /metrics (Prometheus) ─┘   + service_status (пинги vLLM/OWUI/db/redis)
OWUI API / лог-файл          → request_logs
почасовая агрегация          → hourly_aggs
retention: 30д snapshots, 7д prompt_preview (обнуление), 49д request_logs
алерты по порогам (settings) → notifier (Telegram, cooldown)

API (GET /api/latest, /api/history) читает БД (кэш Redis 10с)
Фронтенд (JS + Chart.js) опрашивает API каждые 10с → графики
```

Важно: **общая** статистика токенов на главном экране — это vLLM-метрики
Prometheus (vllm:prompt_tokens_total и т.п. — глобально по серверу) + логи OWUI.
Она НЕ зависит от gateway и заполняется, даже если клиенты ходят в vLLM напрямую.

## 9. Фронтенд

- SPA без бандла-фреймворка: index.html + src/app.js (Vanilla JS) + Chart.js.
- Вкладки: Обзор (метрики, графики), Запросы (логи), **API-ключи** (новые), Админка.
- «API-ключи»: карточки (статус ●, метрики, mini-бар 7 дней из /stats,
  история запросов из /usage), панель-сводка из /summary, модалка генерации
  (название, лимиты, мастер-пароль) и reveal-модалка «ключ показан один раз».
- Сборка: `npm run build` → frontend/dist, затем вшивается в образ
  backend: `rm -rf backend/frontend && cp -R frontend/dist backend/frontend`
  (Dockerfile `COPY . .` → `/app/frontend` в образе). Bind-mount
  `./frontend/dist:/app/frontend` убран — на macOS Docker Desktop не имел
  права доступа к `~/Documents` (TCC), и создание нового контейнера с таким
  mount падало (`mkdir /host_mnt/...: operation not permitted`).
- Dev: vite, proxy `/api` и `/v1` → http://localhost:3000.

## 10. Деплой / запуск

```bash
# один-в-всё (при изменении фронтенда — сначала собрать и вшить dist, см. §9):
cd frontend && npm run build && cd ..
rm -rf backend/frontend && cp -R frontend/dist backend/frontend
docker compose up -d --build        # --build обязателен при изменениях кода

# доступы:
#   дашборд:       http://<host>:3000        (Basic admin/admin)
#   gateway vLLM:  http://<host>:3000/v1     (Bearer sk-…)
#   БД (локально): docker compose exec -it db psql -U monitoring -d monitoring
#   логи:          docker compose logs -f backend | grep vllm-proxy
```

## 11. Известные проблемы и текущая диагностика

1. **Панель-сводка API-ключей и таблица `api_usage_logs` не заполнялись.**
   СТАТУС (2026-09-04): **решено** — backend пересобран, `/api/keys/summary`
   отвечает 200; per-key latency/speed записываются (проверено e2e-запросом
   через gateway, включая 502 при недоступном vLLM). Причина (найдена):
   **контейнер backend работал на старом образе** —
   `main.py` в `/app` не содержало ни одного маршрута `/api/keys/*`
   (фронтенд при этом был свежий: `./frontend/dist` смонтирован
   волюмом), поэтому `/api/keys/summary` отвечал 404, а все плитки
   сводки оставались «—». Лечится пересборкой:
   `docker compose up -d --build backend`. Проверить актуальность образа:
   `docker compose exec backend sh -c "grep -c 'api/keys' /app/main.py"`
   (должно быть > 0) или `grep -n 'def keys_summary' /app/main.py`.
   Дополнительная вероятная причина пустой таблицы: **клиенты (Cline)
   обращаются к vLLM напрямую (порт 8000), а не через gateway (порт 3000)**.
   Диагностика:
   - `curl -X POST http://<host>:3000/v1/chat/completions -H "Authorization:
     Bearer <ключ>" -d '{"model":"…","messages":[…]}'` и
     `docker compose logs backend | grep vllm-proxy` — если строка появилась,
     gateway работает, проблема в настройке клиентов.
   - Проверить, какой Base URL стоит в клиентах: должно быть
     `http://<host>:3000/v1`, а не `http://<host>:8000/v1`.
   - Per-key «ср. скорость ответа» (ток/с) и «ср. задержка» (мс)
     считаются из счётчиков ключа: `total_tokens / (total_latency_ms/1000)`
     и `total_latency_ms / total_requests`; latency замеряется в gateway
     (non-stream — время на HTTP-вызов vLLM, stream — время до конца SSE).
2. **GPU не обнаружены** — контейнер backend не видит `/dev/nvidia*`
   (нет `--gpus all` / devices в docker-compose для backend). NVML падает,
   collector переключается на `nvidia-smi` (его в образе нет) → gpu=[].
3. **Одноразовый показ ключа**: полный ключ выдаётся только в ответе
   POST /api/keys; в БД — SHA256. «Копировать ключ» в UI копирует
   маскированную форму (префикс + •••), т.к. восстановить ключ невозможно.
4. **Токены в stream**: без `stream_options.include_usage=true` vLLM не шлёт
   usage в SSE (default) — gateway инжектит этот флаг сам + fallback-оценка
   по длине текста (≈4 знака/токен). Точность fallback — грубая.

## 12. Открытые вопросы для анализа

- Нужен ли отдельный контейнер-прокси (nginx/envoy) вместо gateway внутри
  FastAPI (текущее решение — один процесс, общий порт 3000).
- Гипертаблица для `api_usage_logs`? (сейчас объёмы малы, индексы есть).
- Разделение «глобальной» токено-статистики (Prometheus vLLM) и
  «пер-ключ» (gateway) на одном экране: как согласовать, если часть
  трафика идёт мимо gateway (обход через прямой порт vLLM).
- Жёсткий запрет обхода: vLLM на 127.0.0.1 + `VLLM_API_KEY` (2-й уровень)
  или firewall-правило.
- Per-key обогащение Prometheus-метриками vLLM (TTFT/TPOT) — сейчас это
  глобальные метрики, привязка к ключу возможна только через временну́ю
  корреляцию (ограничено).
```