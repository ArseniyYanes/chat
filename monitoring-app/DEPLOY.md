# Гайд по развёртыванию на сервере

Полная инструкция: от «голого» Ubuntu-сервера до дашборда за HTTPS.
Рассматриваем самый типовой случай — **vLLM и Open WebUI работают на этом
же сервере** (в Docker или напрямую). Вариант «мониторим другую машину»
выделен отдельными примечаниями.

Целевая ОС: Ubuntu 22.04 / 24.04 (или любой Debian-подобный Linux с Docker).

---

## 1. Требования к серверу

| Ресурс | Минимум | Комфорт |
|---|---|---|
| CPU | 2 ядра | 4 ядра |
| RAM | 2 ГБ (свободно) | 4 ГБ |
| Диск | 5 ГБ свободно | 20 ГБ |
| Сеть | доступ к Docker Hub и PyPI | — |
| Права | root или sudo | — |

Мониторинг потребляет немного: коллектор опрашивает метрики раз в 10 с,
базу под ~1000 снапшотов/день на месяц ретеншена занимает < 1 ГБ.

> **Важно про GPU-сервер.** Если сервер — та самая машина с GPU, на
> которой крутится vLLM, учитывайте, что мониторинг — это +2–3 ГБ RAM
> и ~1–2 ядра сверх нагрузки инференса.

---

## 2. Установка Docker

```bash
# установщик от Docker (idempotent, ставит docker engine + compose plugin)
curl -fsSL https://get.docker.com | sh

# добавьте пользователя в группу docker, чтобы не писать sudo
sudo usermod -aG docker $USER

# ОБЯЗАТЕЛЬНО перелогиньтесь (или выполните newgrp docker), затем:
docker --version
docker compose version
```

Проверка: `docker run --rm hello-world` должен напечатать "Hello from
Docker!".

---

## 3. Попадание кода на сервер

Любым удобным способом. Варианты:

**A. Git** (если репозиторий есть):

```bash
sudo apt install -y git
git clone <ваш-репозиторий> ~/monitoring-app
```

**B. Rsync с локальной машины** (сделал у себя — перескочил):

```bash
# с локальной машины, из папки проекта:
rsync -avz \
  --exclude frontend/node_modules \
  --exclude frontend/dist \
  ./ user@server:~/monitoring-app/
```

**C. Tar-архив:**

```bash
# локально:
tar czf monitoring-app.tgz --exclude='frontend/node_modules' \
    --exclude='frontend/dist' monitoring-app/
scp monitoring-app.tgz user@server:~
# на сервере:
tar xzf monitoring-app.tgz -C ~
```

После этого на сервере: `cd ~/monitoring-app`.

---

## 4. Сборка фронтенда

Бэкенд раздаёт `frontend/dist` как статику, поэтому **до первого запуска**
нужно собрать фронт. Node.js на сервере для этого не нужен — соберём
в контейнере:

```bash
cd ~/monitoring-app
docker run --rm \
  -v "$PWD/frontend":/app -w /app \
  node:22-alpine sh -c "npm ci && npm run build"
ls frontend/dist/   # → index.html + assets/
```

> Если Node.js на сервере всё-таки есть — `npm --prefix frontend ci &&
> npm --prefix frontend run build` — то же самое.

---

## 5. Конфигурация: `.env`

```bash
cp .env.example .env
nano .env
```

Рекомендуемые значения для продакшена:

| Переменная | Что поставить | Пояснение |
|---|---|---|
| `APP_PORT` | `3000` | порт дашборда на хосте; поменяйте, если 3000 занят |
| `MONITORING_PASSWORD` | **сложный пароль** | по умолчанию `admin` — не оставляйте |
| `ADMIN_USER` | `admin` (или своё) | логин HTTP Basic |
| `VLLM_API_URL` | см. ниже | по умолчанию `http://172.17.0.1:8000` |
| `OPEN_WEBUI_URL` | см. ниже | по умолчанию `http://172.17.0.1:8080` |
| `OPEN_WEBUI_API_TOKEN` | API-ключ OWUI | Settings → Manage → API keys (admin). Без него не будет лога запросов, только метрики |
| `TELEGRAM_BOT_TOKEN` | токен бота | опционально, алерты |
| `TELEGRAM_CHAT_ID` | ваш chat id | опционально |
| `COLLECT_INTERVAL` | `10` | период сбора, сек |
| `PROMPT_RETENTION_DAYS` | `7` | сколько хранить prompt-превью |
| `SNAPSHOTS_RETENTION_DAYS` | `30` | сколько хранить метрики |

### Куда указывать URL — самое частое место путаницы

Контейнеры бэкенда ходят в сеть через мост Docker. `172.17.0.1` — это
**шлюз (сам хост) по умолчанию в docker0**. Это работает, когда:

- vLLM запущен **на этом сервере** и слушает `0.0.0.0:8000`
  (по умолчанию vLLM именно так слушает);
- Open WebUI запущен **на этом сервере** и доступен с хоста на `:8080`.

Проверьте с хоста (снаружи контейнеров):

```bash
curl -s http://127.0.0.1:8000/health      # vLLM
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/
```

Если vLLM/OWUI запущены с `--host 127.0.0.1` — контейнер их **не увидит**
(loopback хоста изолирован от docker0). Тогда либо перезапустить сервис
на `0.0.0.0`, либо в `.env` указать URL через host-network контейнер.

**Вариант «мониторим другую машину в сети»:** просто подставьте её IP:

```
VLLM_API_URL=http://192.168.1.50:8000
OPEN_WEBUI_URL=http://192.168.1.50:8080
```

(и убедитесь, что эти сервисы слушают не только 127.0.0.1 на той машине).


---

## 6. Запуск

```bash
cd ~/monitoring-app
docker compose up -d --build

# все три контейнера должны быть Up (db и redis — healthy):
docker compose ps

# быстрый check API:
curl -s http://localhost:3000/api/health
# → {"status":"ok","version":"1.0.0","timescale":true,...}
```

Первая сборка образа бэкенда (pip install) занимает 1–3 минуты.

Откройте в браузере `http://<ip-сервера>:3000` — попросит логин/пароль
(`ADMIN_USER` / `MONITORING_PASSWORD` из `.env`).

> **GPU-метрики:** чтобы бэкенд видел GPU, см. раздел 9. До этого в
> дашборде будет `"gpu": null` — это нормально.

---

## 7. Firewall

Откройте только то, что нужно. Если дальше ставите HTTPS-прокси (раздел 8),
порт 3000 наружу открывать **не** нужно — только 80/443.

```bash
sudo ufw allow 22/tcp            # ssh
sudo ufw allow 3000/tcp          # только если НЕ ставите прокси
# либо после прокси: sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

> Без внешнего IP / за NAT'ом: открывать 3000 наружу нежелательно.
> Локально в офисе удобно ходить через SSH-туннель:
> `ssh -L 3000:localhost:3000 user@server` → `http://localhost:3000`.

---

## 8. HTTPS: nginx + Let's Encrypt

HTTP Basic Auth по «голому» HTTP на внешний IP — плохо (логин/пароль
летят в chiaro). Для публичного доступа ставим TLS.

**Требование:** домен (или поддомен) с A-записью на IP сервера, например
`monitoring.example.com`.

```bash
sudo apt install -y nginx certbot python3-certbot-nginx

sudo tee /etc/nginx/sites-available/monitoring.conf <<'EOF'
server {
    server_name monitoring.example.com;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 10m;
    }
}
EOF

sudo ln -s /etc/nginx/sites-available/monitoring.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default   # если порт 80 занят дефолтом
sudo nginx -t && sudo systemctl reload nginx

# сертификат (certbot сам подправит конфиг под HTTPS):
sudo certbot --nginx -d monitoring.example.com
```

Проверка: `https://monitoring.example.com` → дашборд по TLS.
Certbot автоматически продлевает сертификат (timer есть из коробки).

**Альтернатива — Caddy** (TLS выдаёт сам, конфиг на 4 строки):

```bash
sudo apt install -y caddy
sudo tee /etc/caddy/Caddyfile <<'EOF'
monitoring.example.com {
    reverse_proxy 127.0.0.1:3000
}
EOF
sudo systemctl reload caddy
```

---

## 9. GPU для коллектора (если GPU на этом сервере)

Коллектор читает GPU через NVML (pynvml). В контейнере для этого нужно
смонтировать device-узлы. В комплекте готовый override-файл
`docker-compose.gpu.yml` — его не нужно редактировать (если у вас больше
одной карты, добавьте строку `/dev/nvidia1` и т.д.):

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d backend
# в логах должна появиться строка с gpu(nvml)=True:
docker compose logs backend 2>&1 | grep 'collector started'
```

> Почему отдельный файл, а не правка `docker-compose.yml`: в `devices`
> перечислены реальные пути `/dev/nvidia*`. Если оставить их в основном
> compose, стек перестанет подниматься на машинах без NVIDIA (Mac,
> облачные CPU-инстансы и т.п.).

> Если меняли сам compose-файл руками, используйте вашу версию.

Проверка:

```bash
curl -s -u admin:ВашПароль http://localhost:3000/api/latest \
  | python3 -m json.tool | grep -A3 '"gpu"'
```

карточки должны прийти с `util`, `temp`, `mem_*`.

> Fallback на `nvidia-smi` в образе не работает (бинаря нет) —
> монтируйте devices, это единственный рабочий путь в контейнере.
> Если vLLM на **другой** машине — раздел 9 не нужен вообще.

---

## 10. (Опционально) Access-лог в лог запросов

Если Open WebUI сидит за nginx и вы хотите видеть запросы из access-лога:

1. В nginx-конфиге OWUI убедитесь, что access_log пишется в файл
   (например `/var/log/owui-access.log`).
2. Смонтируйте файл в контейнер бэкенда:

```yaml
    volumes:
      - app_data:/app/data
      - ./frontend/dist:/app/frontend:ro
      - /var/log/owui-access.log:/var/log/owui-access.log:ro
```

3. В `.env`:

```
LOG_FILE=/var/log/owui-access.log
```

4. `docker compose up -d backend`.

> Без админ-токена OWUI это единственный способ получить лог запросов.
> С токеном (раздел 5) предпочтительнее — он даёт модель, токены, latency.

## 11. Автозапуск и «самоисцеление»

Настройка уже встроена в `docker-compose.yml`: у всех трёх сервисов
`restart: unless-stopped`. Это значит:

- при перезагрузке сервера Docker (он включён в систему сразу после
  установки) поднимет весь стек сам;
- упавший контейнер (например, OOM у бэкенда) Docker перезапустит сам;
- `docker compose stop` — контейнеры не поднимутся, пока вы сами не
  сделаете `up`.

Проверка:

```bash
# Docker должен быть включён в systemd (после get.docker.com — так и есть):
systemctl is-enabled docker          # → enabled

# перезагрузите сервер и убедитесь:
sudo reboot
# ...после:
docker compose ps                     # все 3 сервиса Up, db/redis healthy
curl -s http://localhost:3000/api/health
```

Нюанс: если сервер — машина с GPU (раздел 9) и при загрузке ядро
ещё не сунуло устройства в `/dev/nvidia*`, бэкенд просто не увидит GPU
и продолжит работать без него (это не сбой, `gpu` в `/api/latest`
будет `null`). Перезапустите `docker compose restart backend`, когда
драйвер подхватит.

Опционально — ограничение размеров логов Docker (по умолчанию
json-file-логи растут безлимитно). Добавьте в `docker-compose.yml`
на уровне `services:` для нужных сервисов:

```yaml
    logging: &default-logging
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

(в каждом сервисе: `logging: *default-logging`.) После этого:
`docker compose up -d` пересоздаст контейнеры с новой политикой.

---

## 12. Обновление, бэкап, рутина

### 12.1. Обновление приложения

```bash
cd ~/monitoring-app
# 1) код: git pull (или rsync/tar по-старому)
git pull
# 2) если менялся фронтенд — пересобрать dist (раздел 4):
docker run --rm -v "$PWD/frontend":/app -w /app \
  node:22-alpine sh -c "npm ci && npm run build"
# 3) пересобрать образ бэкенда и поднять:
docker compose up -d --build
docker compose ps
```

Откат — вернуть предыдущую версию кода (git checkout / старый tar) и
повторить `docker compose up -d --build`. БД откат не затрагивает:
схема создаётся через `create_all` и только дополняется.

> Если в `.env` поменялись переменные — их тоже учитывает
> `docker compose up -d` (контейнер пересоздаётся при смене env).

### 12.2. Резервное копирование

Данных в двух местах:

1. **База** (весь смысл: снапшоты, лог запросов, действия) — том
   `ai-monitoring_db_data`.
2. **`/app/data`** (том `ai-monitoring_app_data`) — служебные файлы
   бэкенда; важность низкая, но копировать несложно.

Основной метод — `pg_dump`:

```bash
cd ~/monitoring-app
docker compose exec -T db \
  pg_dump -U monitoring monitoring \
  > backup-$(date +%F_%H%M).sql
ls -lh backup-*.sql
```

Восстановление (в свежий стек с чистой БД):

```bash
docker compose up -d db redis && docker compose stop backend
docker compose exec -T db psql -U monitoring monitoring \
  < backup-2026-07-28_1200.sql
docker compose start backend
```

Альтернатива для «полной» копии тома (холодная, на остановленной БД):

```bash
docker compose stop db
docker run --rm \
  -v ai-monitoring_db_data:/data:ro \
  -v "$PWD":/backup \
  alpine tar czf /backup/db_data-$(date +%F).tgz -C /data .
docker compose start db
```

Крон (ежедневно в 03:30, хранение 14 дней) — у текущего пользователя:

```bash
crontab -e
30 3 * * * cd $HOME/monitoring-app && docker compose exec -T db pg_dump -U monitoring monitoring > backup-$(date +\%F).sql && find . -name 'backup-*.sql' -mtime +14 -delete
```

> В cron не забудьте экранировать `%` как `\%` в выражении `date`.

### 12.3. Рутина и диагностика

```bash
docker compose ps                       # кто жив
docker compose logs backend --tail 50   # логи приложения
docker compose logs -f db redis         # если что-то подозрительное

curl -s -u $ADMIN_USER:$MONITORING_PASSWORD \
  http://localhost:3000/api/health      # → {"status":"ok","timescale":true}

docker system df                        # место Docker
docker system prune -f                  # очистка мусора (без -a)
```

Типичные симптомы:

| Симптом | Что смотреть |
|---|---|
| Дашборд грузится, но метрики пустые | `docker compose logs backend` — строка `collector started`, `db(ok)`; `/api/status` |
| `openwebui` в статусе `down` | URL/порт OWUI, жив ли сам OWUI; токен (раздел 5) |
| GPU `null` | раздел 9, `ls /dev/nvidia*` |
| Бэкенд не стартует | `docker compose logs backend` — чаще всего `.env` (не тот порт/пароль) |
| Telegram не шлёт алерты | токен/chat_id, `curl -s https://api.telegram.org/bot<токен>/getMe` с сервера |

---

## Чек-лист запуска (сверху вниз)

- [ ] Docker установлен: `docker compose version` отвечает
- [ ] Код на сервере: `~/monitoring-app` с `backend/`, `frontend/`,
      `docker-compose.yml`, `.env.example`
- [ ] Фронт собран: `frontend/dist/index.html` существует
- [ ] `.env` создан, `MONITORING_PASSWORD` не `admin`, URL vLLM/OWUI верные
- [ ] (если GPU на сервере) `devices` для NVIDIA в compose
- [ ] `docker compose up -d` → `docker compose ps`: db/redis healthy,
      backend Up
- [ ] `curl http://localhost:3000/api/health` → `{"status":"ok"}`
- [ ] Дашборд открывается в браузере, логин/пароль работают,
      CPU/RAM/сеть обновляются
- [ ] (публичный доступ) firewall + nginx/Caddy + Let's Encrypt
- [ ] cron-бэкап `pg_dump` настроен
- [ ] (опционально) Telegram-алерты: тестовое срабатывание

Готово. Если что-то из списка не сходится — смотрите таблицу
симптомов в 12.3.

