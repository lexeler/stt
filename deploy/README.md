# Деплой Texpin на nuc через Docker

Получится: **`https://texpin.duckdns.org:8080`** с реальным Let's Encrypt сертификатом, всё в двух контейнерах.

## Архитектура

```
интернет ──:8080──> nuc:8080 ──┐
                                │  docker network "internal"
                                ▼
                        ┌────────────┐
                        │   caddy    │  (TLS, DuckDNS DNS-01, статика, reverse proxy)
                        └─────┬──────┘
                              │
                  /api/* + /healthz
                              │
                              ▼
                        ┌────────────┐
                        │    app     │  uvicorn :8000 + GigaAM
                        └─────┬──────┘
                              │
                       /home/lexeler/stt/app/data
                       (host bind-mount: SQLite, transcripts, uploads)
```

Caddy получает Let's Encrypt сертификат через **DNS-01 challenge** (DuckDNS API), порт 80 не нужен. Это позволяет иметь HTTPS на нестандартном 8080.

`nuc:80` остаётся занятым твоим существующим nginx — мы его не трогаем.

## Шаги (10–20 минут, в основном на сборку Docker-образов)

### 1. На ноуте — синхронизировать проект на nuc

```bash
chmod +x /home/lexeler/stt/deploy/sync-to-nuc.sh
/home/lexeler/stt/deploy/sync-to-nuc.sh
```

Что делает:
- `npm run build` в `web/` (фронт собирается в `web/dist/`)
- `rsync` всего проекта на `nuc:/home/lexeler/stt/` (исключая `.venv`, `node_modules`, `data`)
- Перезапускает контейнеры на nuc (если они уже подняты)

### 2. На nuc — поднять контейнеры (один раз)

```bash
ssh nuc
chmod +x /home/lexeler/stt/deploy/install-on-nuc.sh
bash /home/lexeler/stt/deploy/install-on-nuc.sh
```

Что произойдёт:
- ставит Docker и docker compose plugin (если не стояли)
  - **Если Docker только что поставился — скрипт скажет «перелогинься». Выйди из ssh, зайди заново, запусти ещё раз.**
- генерит `app/.env` с production-настройками (свежий `SECRET_KEY`, `APP_ORIGIN=https://texpin.duckdns.org:8080`, `COOKIE_SECURE=true`)
- собирает два Docker-образа:
  - `texpin-app` — FastAPI + GigaAM + ffmpeg (~3 GB, первый раз ~5–15 минут)
  - `texpin-caddy` — Caddy с DuckDNS-плагином (~50 MB, секунды)
- `docker compose up -d` стартует обе контейнера в detached режиме
- Открывает 8080 в UFW
- Ставит cron на DuckDNS DDNS (каждые 5 мин) и бэкап БД (ежедневно, хранит 7 дней)

### 3. Проверить

Сначала Caddy должен получить сертификат — это занимает 30–120 секунд.

```bash
# на nuc
docker compose logs -f caddy
# увидишь "certificate obtained successfully" — готово
```

Затем uvicorn должен загрузить модель (~30 сек):
```bash
docker compose logs -f app
# увидишь "Model ready" + "Application startup complete."
```

Финальная проверка:
```bash
curl https://texpin.duckdns.org:8080/healthz
# {"status":"ok","model_loaded":true,"queue_depth":0}
```

Открой в браузере: **https://texpin.duckdns.org:8080**

## Когда обновляешь код

С ноута:
```bash
/home/lexeler/stt/deploy/sync-to-nuc.sh
```

Скрипт сам перезапустит контейнеры на nuc после rsync. Caddy не пересобирается без изменений в Caddyfile/Dockerfile, app пересобирается только если изменились `app/server/`, `app/Dockerfile` или `app/requirements.txt` (Docker layer cache).

## Полезные команды на nuc

```bash
cd /home/lexeler/stt

# Статус
docker compose ps

# Логи
docker compose logs -f app
docker compose logs -f caddy
docker compose logs -f --tail=50

# Перезапуск конкретного контейнера
docker compose restart app
docker compose reload caddy   # без полного перезапуска (если меняешь Caddyfile)

# Полная остановка / запуск
docker compose down
docker compose up -d

# Пересобрать после изменений
docker compose up -d --build

# Зайти внутрь контейнера
docker exec -it texpin-app bash
docker exec -it texpin-caddy sh

# Бэкапы БД (последние 7 дней)
ls -la /home/lexeler/stt/app/data/backup/

# Откатить БД из бэкапа
docker compose stop app
cp /home/lexeler/stt/app/data/backup/app-2026-05-06.db /home/lexeler/stt/app/data/app.db
docker compose start app

# Что слушает на 8080
sudo ss -tlnp | grep 8080
```

## Что если HTTPS не получается

Логи Caddy: `docker compose logs caddy`

Возможные причины:

1. **DuckDNS токен неправильный** — проверь `deploy/caddy.env`, должен совпадать с тем что на duckdns.org
2. **Сеть наружу режется** — Caddy не может достучаться до DuckDNS API. Проверь `docker exec texpin-caddy wget -O- https://www.duckdns.org`
3. **Let's Encrypt rate limit** — если за час сделал >5 неудачных попыток, LE блокирует на час
4. **Время рассинхронизировано** — `timedatectl` на nuc должен показывать корректное время (ACME валидирует по timestamp'ам)

Пока сертификат не получен, сайт может отвечать только по `http://`. Caddy продолжает пробовать каждые несколько минут.

## Что внутри `deploy/`

| Файл | Что |
|---|---|
| `Caddyfile` | Конфиг Caddy: TLS на 8080, DNS-01, /api/* → app, / → /srv/web |
| `caddy.Dockerfile` | Кастомный билд Caddy с плагином `caddy-dns/duckdns` |
| `caddy.env` | `DUCKDNS_TOKEN` — отдельно от других секретов |
| `env.production` | Шаблон для `app/.env` (SECRET_KEY заменяется install-скриптом) |
| `install-on-nuc.sh` | Установка Docker, генерация .env, `docker compose up`, cron |
| `sync-to-nuc.sh` | На ноуте: build + rsync + restart на nuc |
| `README.md` | Этот файл |
