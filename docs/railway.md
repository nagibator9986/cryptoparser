# Railway Deployment

## Назначение

Railway deployment запускает один long-running service:

- HTTP health endpoint на `$PORT`;
- Telegram long polling bot;
- плановую отправку дайджестов по настройкам каждого Telegram-чата;
- SQLite storage в volume `/data`.

Веб-админка не используется. Настройки выполняются командами в Telegram-группе.

## Файлы

- `Dockerfile` - production image, default command `crypto-monitor railway`.
- `railway.toml` - Railway config-as-code: Dockerfile builder, `/health`, restart policy.
- `.env.example` - список переменных для Railway Variables.
- `.dockerignore` - исключает секреты, SQLite, тесты и локальный мусор из build context.

В `Dockerfile` нет инструкции `VOLUME`: Railway ее не поддерживает. Volume нужно
создать в Railway UI и смонтировать в `/data`.

## Railway Setup

1. Создайте Railway project и подключите репозиторий.
2. Добавьте volume и смонтируйте его в `/data`.
3. В Variables добавьте значения из `.env.example`.
4. Убедитесь, что `TELEGRAM_BOT_TOKEN` и `GEMINI_API_KEY` заданы.
5. Выполните deploy.

Railway сам передает переменную `PORT`; healthcheck настроен на `/health`.
Если деплой выполняется без GitHub integration, используйте Railway CLI:

```bash
railway login
railway link
railway up
```

## Required Variables

```text
CRYPTO_MONITOR_ENV=production
CRYPTO_MONITOR_DB_PATH=/data/crypto_monitor.sqlite3
CRYPTO_MONITOR_SOURCES_FILE=/app/config/sources.example.yml
CRYPTO_MONITOR_SKILLS_ROOT=/app/crypto-monitor-skills
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
TELEGRAM_BOT_TOKEN=...
```

## После Deploy

В Telegram-группе выполните:

```text
/crypto_start
/crypto_set delivery on
/crypto_set timezone Asia/Almaty
/crypto_schedule 09:00 пн-пт
/crypto_sources
```

При необходимости включите автоматический сбор и обработку перед плановой сводкой:

```text
/crypto_set auto_collect on
/crypto_set auto_process on
```

## Healthcheck

`GET /health` возвращает `200`, если доступны:

- SQLite database path;
- skills directory;
- sources config;
- Telegram bot token;
- Gemini API key.

Секреты отображаются только как `configured`; реальные значения никогда не выводятся.

## Диагностика

Логи:

```bash
railway logs
```

Одноразовая проверка внутри Railway environment:

```bash
railway run crypto-monitor status
railway run crypto-monitor telegram-chats
railway run crypto-monitor evals --dry-run
```

Если deployment успешный, но бот не отвечает в Telegram:

1. Проверьте, что `TELEGRAM_BOT_TOKEN` задан именно у deployed service.
2. Проверьте логи на ошибки Telegram API: `401`, `404`, `409 Conflict`.
   `409 Conflict` означает, что этот же bot token уже использует другой
   `getUpdates`: второй Railway replica, второй service, старый deployment
   во время rollout или локально запущенный бот.
3. Код при старте автоматически выполняет `deleteWebhook`, чтобы long polling
   работал после предыдущих webhook-deployments.
4. Для одного Telegram bot token оставьте один Railway replica и один service.
5. В группе бот должен быть добавлен, а команды настройки должен отправлять
   администратор группы.

Локальная проверка Docker:

```bash
docker build -t crypto-monitor .
docker run --rm --env-file .env -p 8080:8080 -v "$PWD/data:/data" crypto-monitor
curl http://localhost:8080/health
```

## Production Notes

- Без Railway volume SQLite будет храниться в ephemeral filesystem и данные могут исчезнуть после redeploy.
- Для одного Telegram bot token запускайте один Railway replica, иначе long polling может конфликтовать.
- При переходе на PostgreSQL можно заменить SQLite storage, но текущая версия подготовлена под volume `/data`.
