#!/usr/bin/env bash
# Запускать НА nuc: bash /home/lexeler/stt/deploy/install-on-nuc.sh
#
# Что делает:
#   1) ставит Docker + docker compose (если не стоят)
#   2) создаёт app/.env с свежим SECRET_KEY и APP_ORIGIN под прод
#   3) поднимает контейнеры (app + caddy) через docker compose
#   4) открывает 8080 в UFW
#   5) ставит cron на DuckDNS DDNS и бэкап БД
#
# Идемпотентно — можно запускать заново после rsync с ноута, чтобы
# пересобрать фронт-статику и перезапустить контейнеры.

set -euo pipefail

PROJECT=/home/lexeler/stt
DEPLOY="$PROJECT/deploy"
DUCKDNS_DOMAIN=texpin
DUCKDNS_TOKEN=4978664a-d51e-4367-9189-689e5936198d

if [ "$EUID" -eq 0 ]; then
  echo "Запускай НЕ от root, скрипт сам зовёт sudo там где нужно."
  exit 1
fi

echo "=== 0. Утилиты для мониторинга ==="
PKGS=""
command -v sensors >/dev/null 2>&1 || PKGS="$PKGS lm-sensors"
command -v bc >/dev/null 2>&1 || PKGS="$PKGS bc"
command -v sqlite3 >/dev/null 2>&1 || PKGS="$PKGS sqlite3"
if [ -n "$PKGS" ]; then
    sudo apt-get update -qq
    sudo apt-get install -y --no-install-recommends $PKGS
fi
# При первом запуске sensors-detect задаёт вопросы → запускаем неинтерактивно
if [ ! -f /etc/sensors3.conf ] && command -v sensors-detect >/dev/null 2>&1; then
    yes "" | sudo sensors-detect 2>/dev/null >/dev/null || true
fi

echo "=== 1. Docker ==="
if ! command -v docker >/dev/null 2>&1; then
  echo "  устанавливаю Docker..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"
  echo
  echo "  ДОБАВИЛ ТЕБЯ В ГРУППУ docker."
  echo "  ⚠ Перелогинься (выйди из ssh и зайди заново) и запусти этот скрипт ещё раз."
  exit 0
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "  устанавливаю docker compose plugin..."
  sudo apt-get update -qq
  sudo apt-get install -y docker-compose-plugin
fi

echo
echo "=== 2. .env с свежим SECRET_KEY ==="
cd "$PROJECT"
if [ ! -f app/.env ] || ! grep -q "APP_ORIGIN=https://texpin.duckdns.org" app/.env; then
  SECRET=$(openssl rand -base64 48 | tr -d '\n=' | head -c 64)
  sed "s|__REPLACE_WITH_OPENSSL_RAND__|$SECRET|" "$DEPLOY/env.production" > app/.env
  chmod 600 app/.env
  echo "  → app/.env создан"
else
  echo "  → app/.env уже существует, не перезаписываю"
fi

echo
echo "=== 3. data/ ==="
mkdir -p app/data/{uploads,transcripts,backup}

echo
echo "=== 4. Сборка и запуск контейнеров ==="
echo "  ⚠ Первая сборка скачивает torch (~700MB) и собирает GigaAM. Может занять 5-15 минут."
echo
docker compose -f "$PROJECT/docker-compose.yml" up -d --build

echo
echo "=== 5. UFW: открыть 8080 ==="
if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 8080/tcp >/dev/null
  echo "  → 8080/tcp разрешён в UFW"
fi

echo
echo "=== 6. Cron: DuckDNS DDNS + бэкап БД ==="
TMPCRON=$(mktemp)
crontab -l 2>/dev/null > "$TMPCRON" || true

if ! grep -q "duckdns.org/update" "$TMPCRON"; then
  echo "*/5 * * * * curl -fsS 'https://www.duckdns.org/update?domains=$DUCKDNS_DOMAIN&token=$DUCKDNS_TOKEN&ip=' > /home/lexeler/duckdns.log 2>&1" >> "$TMPCRON"
  echo "  → DuckDNS DDNS cron добавлен"
fi

if ! grep -q "app-.*\.db.*backup" "$TMPCRON"; then
  cat >> "$TMPCRON" <<'CRONEOF'
0 3 * * * docker exec texpin-app sh -c 'sqlite3 /opt/app/data/app.db ".backup /opt/app/data/backup/app-$(date +\%F).db" && find /opt/app/data/backup -name "app-*.db" -mtime +7 -delete'
CRONEOF
  echo "  → Backup cron (3:00 ежедневно, хранит 7 дней) добавлен"
fi

crontab "$TMPCRON"
rm -f "$TMPCRON"

echo
echo "=== 7. Финальная проверка ==="
sleep 3
docker compose -f "$PROJECT/docker-compose.yml" ps

echo
echo "Готово!"
echo
echo "Через 30-60 секунд (модель GigaAM грузится):"
echo "  curl -k https://texpin.duckdns.org:8080/healthz"
echo "  → {\"status\":\"ok\",\"model_loaded\":true,\"queue_depth\":0}"
echo
echo "Логи:"
echo "  docker compose -f $PROJECT/docker-compose.yml logs -f app"
echo "  docker compose -f $PROJECT/docker-compose.yml logs -f caddy"
echo
echo "Открой в браузере: https://texpin.duckdns.org:8080"
