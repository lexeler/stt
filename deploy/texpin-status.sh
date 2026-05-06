#!/usr/bin/env bash
# Быстрая сводка по состоянию сервера: температура, нагрузка, контейнеры,
# здоровье API, размер БД.
#
# Запускать на nuc:
#   bash /home/lexeler/stt/deploy/texpin-status.sh

set -u

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }
hr()   { printf -- "—%.0s" $(seq 1 60); printf "\n"; }

bold "[ Температура CPU ]"
if command -v sensors >/dev/null 2>&1; then
    # Берём максимум из всех зон
    tmax=$(sensors 2>/dev/null | grep -oE '\+[0-9.]+°C' | tr -d '+°C' | sort -n | tail -1)
    if [ -n "$tmax" ]; then
        if (( $(echo "$tmax > 85" | bc -l) )); then
            bad "макс ${tmax}°C — горячо, дросселирует"
        elif (( $(echo "$tmax > 75" | bc -l) )); then
            warn "макс ${tmax}°C — нагрелся под нагрузкой"
        else
            ok "макс ${tmax}°C — норма"
        fi
        sensors 2>/dev/null | grep -E "Package|Core" | sed 's/^/    /'
    else
        warn "sensors не отдаёт температуры"
    fi
else
    warn "lm-sensors не установлен. sudo apt install lm-sensors && sudo sensors-detect"
fi

hr
bold "[ Загрузка системы ]"
read -r load1 load5 load15 _ < /proc/loadavg
cores=$(nproc)
echo "  load: $load1 (1m) / $load5 (5m) / $load15 (15m), $cores ядер"
echo "  uptime: $(uptime -p)"

hr
bold "[ Память ]"
free -h | awk 'NR==1 {print "  " $0} NR==2 {printf "  %s\n", $0}'

hr
bold "[ Диск ]"
df -h / /home 2>/dev/null | awk 'NR==1 {print "  " $0} NR>1 {print "  " $0}'

hr
bold "[ Контейнеры ]"
if command -v docker >/dev/null 2>&1; then
    docker compose -f /home/lexeler/stt/docker-compose.yml ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null \
        | sed 's/^/  /'
    echo
    docker stats --no-stream --format "  {{.Name}}: CPU {{.CPUPerc}} | MEM {{.MemUsage}} ({{.MemPerc}})" \
        $(docker compose -f /home/lexeler/stt/docker-compose.yml ps -q) 2>/dev/null
else
    bad "docker не установлен"
fi

hr
bold "[ API ]"
resp=$(curl -m 3 -s https://texpin.duckdns.org:8080/healthz 2>/dev/null)
if echo "$resp" | grep -q '"status":"ok"'; then
    ok "$resp"
else
    bad "/healthz не отвечает: ${resp:-(нет ответа)}"
fi

hr
bold "[ БД и хранилище ]"
db=/home/lexeler/stt/app/data/app.db
if [ -f "$db" ]; then
    size=$(du -h "$db" | cut -f1)
    rows=$(sqlite3 "$db" 'SELECT COUNT(*) FROM jobs' 2>/dev/null || echo "?")
    echo "  БД: $db — $size, $rows джоб"
else
    warn "БД нет: $db"
fi

uploads_size=$(du -sh /home/lexeler/stt/app/data/uploads 2>/dev/null | cut -f1)
trans_size=$(du -sh /home/lexeler/stt/app/data/transcripts 2>/dev/null | cut -f1)
backup_size=$(du -sh /home/lexeler/stt/app/data/backup 2>/dev/null | cut -f1)
echo "  uploads/:    ${uploads_size:-?}"
echo "  transcripts/: ${trans_size:-?}"
echo "  backup/:     ${backup_size:-?}"

hr
bold "[ Очередь задач ]"
queue_depth=$(echo "$resp" | grep -oE '"queue_depth":[0-9]+' | grep -oE '[0-9]+')
if [ -n "$queue_depth" ]; then
    if [ "$queue_depth" -gt 5 ]; then
        warn "в очереди $queue_depth джоб — сервер не успевает"
    else
        ok "в очереди: $queue_depth"
    fi
fi

active=$(sqlite3 "$db" "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')" 2>/dev/null || echo "?")
done_count=$(sqlite3 "$db" "SELECT COUNT(*) FROM jobs WHERE status='done'" 2>/dev/null || echo "?")
echo "  активных: $active   готовых: $done_count"
