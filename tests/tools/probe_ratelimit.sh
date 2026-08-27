#!/bin/bash
# ПРОБА: почему ограничитель частоты не срабатывает на ПРОДЕ (T53).
#
# ПОВОД. 27.08.2026 замер с ноутбука дал шесть 401 подряд на /api/auth/login,
# то есть предел 5/мин не сработал. В тот же день замер на РЕАЛЬНОМ приложении
# локально (боевые лимиты, app.main:app, TestClient) дал 429 ровно на шестом.
# Значит наш код ограничивает, а прод — нет, и разница где-то между ними.
#
# ЧТО УЖЕ ЗАКРЫТО ЗАМЕРОМ И ЗДЕСЬ НЕ ПРОВЕРЯЕТСЯ:
#   • middleware подключён (слой 1 из 2, CORS внешний) — замер по app.user_middleware;
#   • путь распознаётся: _is_strict_auth("/api/auth/login") -> True;
#   • предел у живого экземпляра = 5, окно 60 с;
#   • путь на проде НЕ переписан: тело ответа было
#     {"detail":"Неверный логин или пароль"} — это наш обработчик логина,
#     значит запрос дошёл именно до /api/auth/login, а не до чужого пути.
#
# ЧТО РАЗДЕЛЯЕТ ЭТА ПРОБА — ТРИ ОСТАВШИЕСЯ ВЕРСИИ:
#   Ⓐ middleware на проде вообще не работает (выкачен старый код);
#   Ⓑ ключ ограничителя НЕСТАБИЛЕН — каждый запрос выглядит новым клиентом;
#   Ⓒ инстансов приложения БОЛЬШЕ ОДНОГО, счётчики в памяти у каждого свои
#      (docstring middleware прямо говорит: «надёжно работает в одном
#      инстансе, для нескольких — Redis»).
#
# ЗАПУСК:  bash tests/tools/probe_ratelimit.sh
# Идёт ~1.5 минуты. Имена переменных латиницей — правило CLAUDE.md.

set -u

BASE="${AOCG_API:-https://api.aocgai.ru}"
BODY='{"phone_or_email":"probe-not-a-user@example.invalid","password":"neverusedpassword"}'
FIXED_XFF="198.51.100.7"   # RFC 5737, один и тот же на все запросы

echo "═══ ① МЕТКА ПРОГОНА ═══"
echo "   МСК: $(TZ=Europe/Moscow date '+%d.%m.%Y %H:%M:%S')"
echo "   цель: ${BASE}"

echo
echo "═══ ② КОНТРОЛЬ КАНАЛА ═══"
IFACE=$(route get 8.8.8.8 2>/dev/null | awk '/interface:/{print $2}')
echo "   интерфейс: ${IFACE:-не определён}"
case "$IFACE" in
  utun*|ppp*|ipsec*) echo "   ✗ VPN — выключить и повторить"; exit 1;;
esac
if curl -s -o /dev/null --max-time 6 "https://203.0.113.1/" 2>/dev/null; then
  echo "   ✗ заведомо мёртвый адрес ОТВЕТИЛ — прибор сломан"; exit 1
fi
echo "   ✓ заведомо мёртвый 203.0.113.1 не соединился"
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "${BASE}/health")
[ "$HEALTH" = "200" ] || { echo "   ✗ /health -> ${HEALTH}, цель не отвечает"; exit 1; }
echo "   ✓ /health -> 200"

echo
echo "═══ ③ ЗАМЕР Ⓐ: РАБОТАЕТ ЛИ MIDDLEWARE НА ПРОДЕ ВООБЩЕ ═══"
echo "   Ищем заголовки, которые ставит ТОЛЬКО наш _apply_headers."
HEADERS=$(curl -s -D - -o /dev/null --max-time 15 "${BASE}/health")
echo "$HEADERS" | grep -iE "^(x-frame-options|referrer-policy|x-content-type-options|strict-transport-security|server|via):" | sed 's/^/     /'
MARK=$(echo "$HEADERS" | grep -ic "^x-frame-options: *DENY")
MARK2=$(echo "$HEADERS" | grep -ic "^referrer-policy: *strict-origin-when-cross-origin")
if [ "$MARK" -ge 1 ] && [ "$MARK2" -ge 1 ]; then
  echo "   ✓ ОБА наших заголовка на месте → middleware НА ПРОДЕ РАБОТАЕТ."
  echo "     Версия Ⓐ снята."
else
  echo "   ✗ НАШИХ ЗАГОЛОВКОВ НЕТ → middleware на проде НЕ отрабатывает."
  echo "     Это и есть ответ: выкачен код без него либо слой не собрался."
  echo "     Дальше мерить нечего, остальные замеры пропускаем."
  exit 1
fi

echo
echo "═══ ④ ЗАМЕР Ⓑ: ОДИН И ТОТ ЖЕ X-Forwarded-For на шести входах ═══"
echo "   Если ключ нестабилен БЕЗ заголовка, то С одинаковым заголовком"
echo "   он станет стабильным и 429 появится на шестом."
FIX=""
for I in $(seq 1 6); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 -X POST "${BASE}/api/auth/login" \
    -H "Content-Type: application/json" -H "X-Forwarded-For: ${FIXED_XFF}" -d "$BODY")
  echo "   #${I} (XFF ${FIXED_XFF}) -> ${CODE}"
  FIX="${FIX}${CODE} "
done
echo "   коды: ${FIX}"
case "$FIX" in
  *429*) echo "   ✓ 429 ЕСТЬ → с одинаковым ключом предел работает."
         echo "     Значит БЕЗ заголовка ключ РАЗНЫЙ на каждом запросе (версия Ⓑ),"
         echo "     и попутно доказано: заголовок ДОХОДИТ до приложения —"
         echo "     площадка его не затирает.";;
      *) echo "   ✗ 429 НЕТ и с одинаковым ключом → дело не в ключе.";;
esac

echo
echo "═══ ⑤ пауза 70 с ═══"
sleep 70

echo
echo "═══ ⑥ ЗАМЕР Ⓒ: ОБЩИЙ ЛИМИТ 60/мин на не-auth пути ═══"
echo "   Бьём в /api/users/me без токена (401 от авторизации, но через лимитер)."
echo "   Останавливаемся на первом 429, чтобы не копить превышения и не словить бан."
HIT=0
for I in $(seq 1 75); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "${BASE}/api/users/me")
  if [ "$CODE" = "429" ]; then HIT=$I; break; fi
done
if [ "$HIT" -gt 0 ]; then
  echo "   ✓ 429 на запросе #${HIT} (предел 60/мин)."
  if [ "$HIT" -gt 65 ]; then
    N=$(( (HIT + 59) / 60 ))
    echo "   ⚠️ Это ЗАМЕТНО ПОЗЖЕ 61 — похоже на ${N} инстанса(ов) приложения:"
    echo "     счётчики в памяти у каждого свои, запросы делятся между ними."
    echo "     Тогда auth-предел 5 на инстанс просто не достигается шестью"
    echo "     запросами — это версия Ⓒ."
  else
    echo "   → общий предел держится на ОДНОМ инстансе, версия Ⓒ маловероятна."
  fi
else
  echo "   ✗ 75 запросов и ни одного 429 — общий предел 60/мин ТОЖЕ не работает."
  echo "     Значит ограничитель не считает вообще: смотреть переменные"
  echo "     SECURITY_RATE_LIMIT / SECURITY_AUTH_RATE_LIMIT в панели Timeweb —"
  echo "     заданное там значение перекрывает умолчание кода."
fi

echo
echo "═══ ⑦ ЧТО ПОСМОТРЕТЬ РУКАМИ В ПАНЕЛИ TIMEWEB ═══"
echo "   ① переменные SECURITY_RATE_LIMIT, SECURITY_AUTH_RATE_LIMIT,"
echo "      SECURITY_AUTO_BAN_THRESHOLD — заданы ли и какими значениями;"
echo "   ② сколько ИНСТАНСОВ/реплик у приложения AOCG-AI-001_app_prod."
echo "   Обе цифры — из панели, скриптом их не достать."
