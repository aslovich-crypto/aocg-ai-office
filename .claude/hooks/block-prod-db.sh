#!/usr/bin/env bash
# PreToolUse(Bash) guard: блокирует ИСПОЛНЕНИЕ деструктивного SQL против боевой БД.
#
# Гейт инспекции: если команда ничего не исполняет — нет интерпретатора
# (python/psql/dropdb) и нет пайпа в sh|bash — это чистое чтение
# (git show/diff/log/blame, grep, cat, head, tail, less), пропускаем, даже если
# читаемый файл полон SQL. Иначе сканируем команду + содержимое запускаемых
# .py/.sql и блокируем деструктив, если он подключён к проду
# (railway / asyncpg / DATABASE_URL / psql / up.railway.app).
set -euo pipefail

cmd=$(jq -r '.tool_input.command // ""')

# Гейт: есть ли в команде исполнитель кода/SQL? Нет → чистая инспекция → пропуск.
# Ловит: python / python3 / psql / dropdb где угодно (вкл. `| python`, `| psql`,
# `./venv/bin/python`), и пайп в шелл `| sh` / `| bash`.
if ! printf '%s' "$cmd" | grep -iqE 'python[0-9]?|psql|dropdb|\|[[:space:]]*(sh|bash)\b'; then
  exit 0
fi

# Команда что-то исполняет — собираем текст для проверки: сама команда +
# содержимое запускаемых .py/.sql (SQL часто лежит внутри скрипта).
scan="$cmd"
for f in $(printf '%s' "$cmd" | grep -oE "[^[:space:]\"']+\.(py|sql)" | sort -u); do
  [ -f "$f" ] && scan="$scan
$(cat "$f" 2>/dev/null)"
done

if printf '%s' "$scan" | grep -iqE 'DROP[[:space:]]+(TABLE|INDEX|DATABASE)|DELETE[[:space:]]+FROM|TRUNCATE|dropdb' \
   && printf '%s' "$scan" | grep -iqE 'railway|DATABASE_PUBLIC_URL|DATABASE_URL|asyncpg|psql|up\.railway\.app'; then
  printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Блокировано хук-защитой прод-БД: исполнение деструктивного SQL (DROP/DELETE/TRUNCATE) против боевой базы. Если миграция действительно нужна — выполни её осознанно, временно отключив этот хук."}}'
fi
exit 0
