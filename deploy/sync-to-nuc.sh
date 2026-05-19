#!/usr/bin/env bash
# Запускать на ноуте. Синхронизирует серверный код на nuc и перезапускает
# контейнеры (если они там уже подняты). Фронта больше нет — только API.
#
#   ./deploy/sync-to-nuc.sh                 # rsync + restart на nuc
#   ./deploy/sync-to-nuc.sh --no-restart    # не дёргать контейнеры
#   ./deploy/sync-to-nuc.sh --dry-run       # показать что будет изменено, не пушить
#
# СОХРАННОСТЬ ДАННЫХ НА СЕРВЕРЕ:
# rsync идёт с --delete (нужно, чтобы удалённые в локалке файлы пропадали и
# на сервере). Это бомба для ручных правок на сервере. Защита:
#   1) ВСЁ что удаляется/перезаписывается сначала кладётся в
#      /home/lexeler/stt-deploy-backups/YYYY-MM-DD-HHMMSS/ на nuc.
#   2) Если бэкап непустой — скрипт громко предупреждает в конце.
#   3) Бэкапы старше 30 дней удаляются автоматически.
#
# Восстановить файл с сервера:
#   ssh nuc 'cp -a /home/lexeler/stt-deploy-backups/<timestamp>/deploy/Caddyfile \
#                  /home/lexeler/stt/deploy/Caddyfile'

set -euo pipefail

PROJECT=/home/lexeler/stt
REMOTE="nuc:/home/lexeler/stt/"
BACKUP_ROOT="/home/lexeler/stt-deploy-backups"
STAMP="$(date +%F-%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"

DO_RESTART=1
DRY_RUN=0
for arg in "$@"; do
  case $arg in
    --no-restart) DO_RESTART=0 ;;
    --dry-run) DRY_RUN=1 ;;
    --no-build) ;;  # legacy no-op (frontend removed)
  esac
done

# Очистка старых бэкапов (> 30 дней) на сервере — чтобы не разрастались.
# Тихо игнорируем если папки backup_root ещё нет.
ssh nuc "mkdir -p '$BACKUP_DIR' && \
         find '$BACKUP_ROOT' -mindepth 1 -maxdepth 1 -type d -mtime +30 \
              -exec rm -rf {} + 2>/dev/null || true"

RSYNC_OPTS=(
  -azh
  --delete
  --backup
  --backup-dir="$BACKUP_DIR"
  --exclude='.venv/'
  --exclude='app/data/'
  --exclude='app/.env'
  --exclude='__pycache__/'
  --exclude='.pytest_cache/'
  --exclude='.git/'
  --info=progress2
  --itemize-changes
)
if [ $DRY_RUN -eq 1 ]; then
  RSYNC_OPTS+=(--dry-run)
  echo
  echo "==> DRY-RUN: показываю что бы изменилось, ничего не пушу"
else
  echo
  echo "==> rsync $PROJECT → $REMOTE  (backup → $BACKUP_DIR)"
fi

# Captureим вывод чтобы потом увидеть были ли удаления/перезаписи.
RSYNC_LOG=$(mktemp -p /home/lexeler rsync-log.XXXXXX)
trap 'rm -f "$RSYNC_LOG"' EXIT

rsync "${RSYNC_OPTS[@]}" "$PROJECT/" "$REMOTE" | tee "$RSYNC_LOG"

if [ $DRY_RUN -eq 1 ]; then
  echo
  echo "Dry-run завершён. Запусти без --dry-run чтобы реально применить."
  exit 0
fi

# Что было перезаписано/удалено? Смотрим что попало в backup-dir.
BACKUP_CONTENTS=$(ssh nuc "find '$BACKUP_DIR' -type f 2>/dev/null | head -50")
if [ -n "$BACKUP_CONTENTS" ]; then
  echo
  echo "⚠️  ВНИМАНИЕ: следующие файлы на сервере были ПЕРЕЗАПИСАНЫ или УДАЛЕНЫ"
  echo "   (если правил их вручную на nuc — твои правки сохранены в backup):"
  echo "$BACKUP_CONTENTS" | sed "s|$BACKUP_DIR/|   - |"
  echo
  echo "   Восстановить:  ssh nuc 'cp -a $BACKUP_DIR/<path> /home/lexeler/stt/<path>'"
else
  # Пустой backup-dir — удалим чтобы не плодить мусор.
  ssh nuc "rmdir '$BACKUP_DIR' 2>/dev/null || true"
fi

if [ $DO_RESTART -eq 1 ]; then
  echo
  echo "==> restart on nuc (если docker compose уже там)"
  ssh nuc 'cd /home/lexeler/stt && \
           if docker compose ps -q app 2>/dev/null | grep -q .; then \
             docker compose up -d --build; \
           else \
             echo "  Контейнеры ещё не поднимались. Запусти deploy/install-on-nuc.sh"; \
           fi'
fi

echo
echo "Готово."
