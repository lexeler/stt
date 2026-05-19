# Citrus Lab — Расшифровщик (бэкенд)

FastAPI + GigaAM v3 + SQLite. Любой может загрузить файл или записать аудио в браузере и получить текст; регистрация поверх анонимной сессии переносит уже сделанные расшифровки в личный кабинет.

## Локальная разработка

```bash
# из /home/lexeler/stt
.venv/bin/pip install -r app/requirements.txt   # если что-то ещё не стоит

# скопируй .env.example → app/.env и при желании отредактируй
cp app/.env.example app/.env

# запуск (модель грузится один раз, ~10–30 сек)
cd app && ../.venv/bin/uvicorn server.main:app --reload --port 8000
```

Открыть `http://localhost:8000/healthz`.

Без модели (быстрая проверка ручек):

```bash
cd app && ASR_LOAD_ON_STARTUP=false ../.venv/bin/uvicorn server.main:app --reload
```

## Тесты

```bash
cd app && ../.venv/bin/python -m pytest
```

Тесты не требуют модели и ffmpeg — `pipeline.transcribe_audio` мокается фикстурами.

## API

| Метод   | Путь                                        | Кому                       |
| ------- | ------------------------------------------- | -------------------------- |
| POST    | `/api/register`                             | анон → upgrade сессии      |
| POST    | `/api/login`                                | возвращает сессию          |
| POST    | `/api/logout`                               | удаляет сессию             |
| GET     | `/api/me`                                   | состояние сессии           |
| POST    | `/api/upload`                               | multipart `file=`          |
| GET     | `/api/jobs?limit=&offset=`                  | список своих джоб          |
| GET     | `/api/jobs/{id}`                            | статус                     |
| POST    | `/api/jobs/{id}/cancel`                     | запросить отмену           |
| DELETE  | `/api/jobs/{id}`                            | удалить (только не активный) |
| GET     | `/api/jobs/{id}/text/{clean\|timestamps}`   | inline `text/plain`        |
| GET     | `/api/jobs/{id}/download/{clean\|timestamps}` | attachment `*.txt`        |
| GET     | `/healthz`                                  | статус сервиса             |

Все mutating ручки требуют корректный `Origin` или `Referer` (отключается `CSRF_CHECK=false`). Cookie `session=…` выставляется автоматически на любом запросе.

## Жизненный цикл

- Анон-сессия живёт 7 дней; джоб (после готовности) — 24 часа.
- Зарегистрированная сессия живёт 30 дней; джоб — 30 дней с момента готовности.
- Регистрация апгрейдит ту же сессию (cookie остаётся той же), переписывая retention уже готовых джоб с 24 ч на 30 дней.
- Логин в существующий аккаунт по умолчанию **тоже** переносит анон-джобы (флаг `claim_anon_session`, по умолчанию true).

## Лимиты

| Ресурс                                   | Анон   | Юзер     |
| ---------------------------------------- | ------ | -------- |
| Загрузок в час                           | 3      | 20       |
| Суммарно аудио в сутки                   | 60 мин | без лимита |
| Размер файла                             | 2 ГиБ  | 2 ГиБ    |
| Длительность файла                       | 5 ч    | 5 ч      |

Все значения настраиваются в `.env`.

## Деплой через Docker

`docker-compose.yml` поднимает два контейнера: `app` (FastAPI) и `caddy` (TLS + reverse proxy). GigaAM подтягивается из `../transcribe/GigaAM` через `additional_contexts` Compose.

```bash
cd app
echo "APP_DOMAIN=stt.example.com" >> .env
echo "ACME_EMAIL=you@example.com" >> .env
docker compose up -d --build
```

Образ — ~2.5 ГБ (CPU-only torch). Первый запуск загружает модель из HuggingFace (1–2 минуты). Volume `./data` хранит `app.db` и тексты.

Бэкап БД:

```bash
sqlite3 app/data/app.db ".backup /tmp/app-$(date +%F).db"
```

## Файлы

```
app/
├── server/
│   ├── main.py             — FastAPI app + lifespan (модель, recovery, cleanup)
│   ├── config.py           — pydantic-settings, env-config
│   ├── db.py               — engine + WAL + sessionmaker
│   ├── models.py           — User / Session / Job
│   ├── schemas.py          — Pydantic DTO
│   ├── auth.py             — bcrypt, sessions, claim
│   ├── deps.py             — get_session, get_current_user, CSRF
│   ├── ratelimit.py        — slowapi + БД-квоты для upload
│   ├── worker.py           — очередь, тред-воркер, ETA, recovery
│   ├── pipeline.py         — ASR (копия из transcribe/, не трогать без причины)
│   ├── cleanup.py          — фоновая очистка по retention
│   ├── utils.py            — фоматтеры + tz-aware helper
│   └── routers/
│       ├── auth.py
│       ├── upload.py
│       ├── jobs.py
│       ├── transcripts.py
│       └── health.py
├── tests/
│   ├── conftest.py
│   ├── test_auth.py
│   ├── test_anon_claim.py
│   ├── test_upload.py
│   ├── test_jobs.py
│   └── test_cleanup.py
├── data/                   — SQLite + загрузки + тексты (gitignored)
├── Dockerfile
├── docker-compose.yml
├── Caddyfile
├── requirements.txt
├── .env.example
└── README.md
```

## TODO (не на сегодня)

- Email-верификация (поле `users.email_verified_at` уже зарезервировано).
- OAuth (Google) — отдельная таблица `oauth_accounts`.
- SSE-эндпоинт `/api/jobs/stream` для live-обновлений вместо polling.
- Alembic-миграции (сейчас `Base.metadata.create_all` на старте — годится пока схема стабильна).
