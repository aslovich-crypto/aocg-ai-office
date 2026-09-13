# -*- coding: utf-8 -*-
"""Выпуск, отзыв и список ключей интеграции — на живой базе (шаг 3, заход ⑧).

⚠️ ЖИВАЯ БАЗА, А НЕ ДВОЙНИК, И ПО СУЩЕСТВУ. Предмет проверки — что уникальный
индекс префикса настоящий, что счёт живых считает SQL, что отзыв действует
на проверку ключа НЕМЕДЛЕННО и что выпущенным ключом можно тут же войти.
Двойник всё это изображал бы, то есть проверял бы зеркало.

⚠️ ГЛАВНОЕ, ЧТО ЗДЕСЬ ЗАКРЕПЛЕНО: СЕКРЕТ НЕ ВОССТАНОВИМ. Он приходит один раз
при выпуске и больше не появляется нигде — ни в списке, ни в базе. Это свойство
УСТРОЙСТВА (в базе хеш), и проверка ищет секрет по всей строке целиком, а не
по названным колонкам: проверка «в имени нет секрета» промолчала бы, появись
завтра колонка с ним рядом.
"""

from datetime import datetime, timezone

import pytest

from app import integration_keys as ик
from app.routers import keys as рк

ВЫПУСК = "/api/integration-keys/"


async def _орг(db):
    """Организация и оба смотрящих фикстур.

    ⚠️ ЧЕЛОВЕК ОБЯЗАН СУЩЕСТВОВАТЬ В БАЗЕ: `integration_keys.created_by` —
    настоящий внешний ключ. У двойника подменённый смотрящий жил сам по себе,
    здесь нет, и это ровно то, ради чего живой контур заводился.
    """
    await db.добавить_организацию(id=1, name="АОЦГ")
    await db.обеспечить_пользователя(id=1, first_name="Админ", role="admin")
    await db.обеспечить_пользователя(id=2, first_name="Иван", role="employee")


def _тело(name="1С бюро", **ещё):
    return dict({"name": name}, **ещё)


# ── ПРАВА ────────────────────────────────────────────────────────────────
# ⚠️⚠️ РОЛЬ ПЕРЕКЛЮЧАЕТСЯ ЯВНО, А НЕ ВЫБОРОМ ФИКСТУРЫ-КЛИЕНТА, И ЭТО КУПЛЕНО
# ПРЯМО ЗДЕСЬ. Первая редакция брала в один тест `client_accountant` и
# `client_employee` разом. Обе фикстуры подменяют ОДНУ И ТУ ЖЕ зависимость
# `get_current_user` на уровне приложения, поэтому побеждает та, что создана
# последней: обе половины теста шли под одной ролью, а вторая не проверялась
# вовсе. Поймал это единственный тест, где роли давали РАЗНЫЙ ответ, —
# остальные были зелены и молчали.


@pytest.mark.asyncio
async def test_выпуск_только_администратору(client, as_role, db):
    """Бухгалтер список видит, а ключ не выпускает: выпуск ВЫДАЁТ доступ."""
    await _орг(db)
    for роль in ("accountant", "employee"):
        as_role(роль)
        ответ = await client.post(ВЫПУСК, json=_тело())
        assert ответ.status_code == 403, "%s выпустил ключ: %s" % (роль, ответ.text)


@pytest.mark.asyncio
async def test_список_виден_администратору_и_бухгалтеру(client, as_role, db):
    await _орг(db)
    for роль in ("admin", "accountant"):
        as_role(роль)
        assert (await client.get(ВЫПУСК)).status_code == 200, роль


@pytest.mark.asyncio
async def test_список_сотруднику_не_виден(client, as_role, db):
    """Заведомо разная пара к проверке выше: сотрудник получает отказ."""
    await _орг(db)
    as_role("employee", user_id=2)
    assert (await client.get(ВЫПУСК)).status_code == 403


@pytest.mark.asyncio
async def test_отзыв_только_администратору(client, as_role, db):
    await _орг(db)
    выпущен = (await client.post(ВЫПУСК, json=_тело())).json()
    as_role("accountant")
    ответ = await client.post("%s%d/revoke" % (ВЫПУСК, выпущен["id"]))
    assert ответ.status_code == 403
    # И ключ остался живым — отказ не должен быть отказом «на вид».
    assert await ик.найти_живой(db.pool, выпущен["prefix"]) is not None


# ── ВЫПУСК ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_выпущенным_ключом_можно_войти_сразу(client, db):
    """⚠️ СКВОЗНАЯ ПРОВЕРКА. Выпуск, разбор и сверка секрета — три разных
    места; сойтись они обязаны без посредника, иначе ключ красив на экране
    и не работает в 1С."""
    await _орг(db)
    выпущен = (await client.post(ВЫПУСК, json=_тело())).json()
    префикс, секрет = ик.разобрать(выпущен["key"])
    assert префикс == выпущен["prefix"]
    строка = await ик.найти_живой(db.pool, префикс)
    assert строка is not None
    assert строка["secret_hash"] == ик.хеш_секрета(секрет)


@pytest.mark.asyncio
async def test_секрет_только_ascii(client, db):
    """Находка захода ⑤: кириллица в заголовке HTTP роняет запрос ещё
    в клиенте, до всякой проверки, и выглядит как «интеграция не работает»."""
    await _орг(db)
    ключ = (await client.post(ВЫПУСК, json=_тело())).json()["key"]
    assert ключ.isascii(), "в ключе не-ASCII: заголовок с ним не отправится"
    ключ.encode("ascii")  # то же самое, но падением, а не утверждением


@pytest.mark.asyncio
async def test_префикс_нужной_длины_и_секрет_не_короткий(client, db):
    await _орг(db)
    выпущен = (await client.post(ВЫПУСК, json=_тело())).json()
    префикс, секрет = выпущен["key"].split(ик.РАЗДЕЛИТЕЛЬ, 1)
    assert len(префикс) == ик.ДЛИНА_ПРЕФИКСА
    assert len(секрет) >= 32, "секрет стал короче — перебор дешевле"


@pytest.mark.asyncio
async def test_два_ключа_подряд_различны(client, db):
    """Один и тот же секрет у двух ключей означал бы, что случайности нет."""
    await _орг(db)
    первый = (await client.post(ВЫПУСК, json=_тело("первый"))).json()
    второй = (await client.post(ВЫПУСК, json=_тело("второй"))).json()
    assert первый["key"] != второй["key"]
    assert первый["prefix"] != второй["prefix"]


@pytest.mark.asyncio
async def test_третий_живой_ключ_не_выпускается(client, db):
    """Два — чтобы поворачивать ключ, не останавливая интеграцию. Третий уже
    не поворот, а размножение дверей."""
    await _орг(db)
    assert (await client.post(ВЫПУСК, json=_тело("первый"))).status_code == 200
    assert (await client.post(ВЫПУСК, json=_тело("второй"))).status_code == 200
    третий = await client.post(ВЫПУСК, json=_тело("третий"))
    assert третий.status_code == 409, третий.text
    assert "Отзовите" in третий.json()["detail"]


@pytest.mark.asyncio
async def test_после_отзыва_место_освобождается(client, db):
    """Вторая половина к проверке выше: предел про ЖИВЫЕ, а не про все."""
    await _орг(db)
    первый = (await client.post(ВЫПУСК, json=_тело("первый"))).json()
    await client.post(ВЫПУСК, json=_тело("второй"))
    await client.post("%s%d/revoke" % (ВЫПУСК, первый["id"]))
    assert (await client.post(ВЫПУСК, json=_тело("третий"))).status_code == 200


@pytest.mark.asyncio
async def test_предел_считается_по_своей_организации(client, db):
    """Чужие ключи наш предел не съедают."""
    await _орг(db)
    await db.добавить_организацию(id=777, name="Чужая")
    await db.добавить_пользователя(id=9, first_name="Чужой", role="admin", org_id=777)
    for н in (1, 2):
        await db.pool.execute(
            "INSERT INTO integration_keys "
            "(org_id, prefix, secret_hash, name, created_by) "
            "VALUES (777, $1, 'x', 'чужой', 9)",
            "aocgchuzh%d" % н,
        )
    assert (await client.post(ВЫПУСК, json=_тело())).status_code == 200


# ── СРОК ЖИЗНИ ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_срок_ставится_по_умолчанию_на_год(client, db):
    await _орг(db)
    выпущен = (await client.post(ВЫПУСК, json=_тело())).json()
    assert выпущен["expires_at"] is not None, "выпущен бессрочный ключ"
    assert выпущен["days_left"] in (364, 365)


@pytest.mark.asyncio
async def test_бессрочный_ключ_выпустить_нельзя(client, db):
    """⚠️ ГЛАВНЫЙ СТОРОЖ СРОКА. Вечная ссылка — это дверь, а здесь дверь лежит
    в чужой базе. `null` и ноль дней отвергаются оба."""
    await _орг(db)
    for негодное in (None, 0, -1, ПОТОЛОК := 366):
        ответ = await client.post(ВЫПУСК, json=_тело(expires_days=негодное))
        assert ответ.status_code == 422, "принят срок %r" % негодное
    assert ПОТОЛОК == рк.ПОТОЛОК_СРОКА_ДНЕЙ + 1


@pytest.mark.asyncio
async def test_свой_срок_принимается(client, db):
    await _орг(db)
    выпущен = (await client.post(ВЫПУСК, json=_тело(expires_days=30))).json()
    assert выпущен["days_left"] in (29, 30)


@pytest.mark.asyncio
async def test_истёкший_ключ_не_живой_и_место_не_занимает(client, db):
    await _орг(db)
    первый = (await client.post(ВЫПУСК, json=_тело("первый"))).json()
    await client.post(ВЫПУСК, json=_тело("второй"))
    await db.pool.execute(
        "UPDATE integration_keys SET expires_at = NOW() - interval '1 day' WHERE id=$1",
        первый["id"],
    )
    assert (await client.post(ВЫПУСК, json=_тело("третий"))).status_code == 200
    список = (await client.get(ВЫПУСК)).json()
    истёкший = [к for к in список if к["id"] == первый["id"]][0]
    assert истёкший["is_active"] is False
    assert истёкший["days_left"] == 0, "осталось отрицательное число дней"


# ── ЧАСЫ БАЗЫ ────────────────────────────────────────────────────────────
# ⚠️ Замечание владельца 13.09.2026 к первой редакции: is_active считался
# по часам контейнера, а проверка ключа сверяет срок с NOW() базы. У порога
# истечения список говорил бы «живой», а 1С получала бы 401.


@pytest.mark.asyncio
async def test_остаток_меньше_суток_не_ноль(client, db):
    """«0 дней» у живого ключа читается как «истёк». Остаток — сутками вверх."""
    await _орг(db)
    выпущен = (await client.post(ВЫПУСК, json=_тело())).json()
    await db.pool.execute(
        "UPDATE integration_keys SET expires_at = NOW() + interval '1 hour' WHERE id=$1",
        выпущен["id"],
    )
    строка = (await client.get(ВЫПУСК)).json()[0]
    assert строка["is_active"] is True
    assert строка["days_left"] == 1, (
        "час до истечения показан как %r дней" % строка["days_left"]
    )


@pytest.mark.asyncio
async def test_живость_остаток_и_срок_по_часам_базы(client, db, monkeypatch):
    """Часам приложения ставим 2035 год. Если хоть одно из трёх — is_active,
    days_left, expires_at — считается в питоне, оно уедет вслед за ними."""
    await _орг(db)

    class Врущие(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2035, 1, 1, tzinfo=tz or timezone.utc)

    monkeypatch.setattr(рк, "datetime", Врущие, raising=False)
    выпущен = (await client.post(ВЫПУСК, json=_тело())).json()
    по_базе = await db.pool.fetchval(
        "SELECT expires_at BETWEEN NOW() + interval '364 days' "
        "AND NOW() + interval '366 days' FROM integration_keys WHERE id=$1",
        выпущен["id"],
    )
    assert по_базе, "срок выпуска посчитан часами приложения"
    строка = (await client.get(ВЫПУСК)).json()[0]
    assert строка["is_active"] is True, "живость посчитана часами приложения"
    assert строка["days_left"] == 365, "остаток посчитан часами приложения"


# ── СПИСОК ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_состав_строки_списка_поимённо(client, db):
    """⚠️ ПИСАНЫЙ ДОГОВОР, А НЕ СВЕРКА С КОНСТАНТОЙ ИЗ КОДА. Обе стороны
    из одного источника — тавтология (седьмое наблюдение 13а.18)."""
    await _орг(db)
    await client.post(ВЫПУСК, json=_тело())
    строка = (await client.get(ВЫПУСК)).json()[0]
    assert set(строка) == {
        "id",
        "prefix",
        "name",
        "created_at",
        "expires_at",
        "last_used_at",
        "use_count",
        "revoked_at",
        "days_left",
        "is_active",
    }, "состав строки списка изменился: %s" % sorted(строка)


@pytest.mark.asyncio
async def test_секрета_в_списке_нет_ни_в_каком_виде(client, db):
    """Ищем по ВСЕЙ строке ответа, а не по названным полям."""
    await _орг(db)
    ключ = (await client.post(ВЫПУСК, json=_тело())).json()["key"]
    секрет = ключ.split(ик.РАЗДЕЛИТЕЛЬ, 1)[1]
    ответ = await client.get(ВЫПУСК)
    assert секрет not in ответ.text, "секрет виден в списке"
    assert "secret_hash" not in ответ.text
    assert ик.хеш_секрета(секрет) not in ответ.text, "хеш секрета уехал наружу"


@pytest.mark.asyncio
async def test_секрет_не_восстановим_и_после_выпуска(client, db):
    """⚠️ СВОЙСТВО УСТРОЙСТВА, А НЕ ДИСЦИПЛИНЫ ЭКРАНА: в базе лежит хеш."""
    await _орг(db)
    ключ = (await client.post(ВЫПУСК, json=_тело())).json()["key"]
    секрет = ключ.split(ик.РАЗДЕЛИТЕЛЬ, 1)[1]
    строка = await db.pool.fetchrow("SELECT * FROM integration_keys")
    целиком = " ".join(str(з) for з in dict(строка).values())
    assert секрет not in целиком, "секрет осел в базе — его можно восстановить"


@pytest.mark.asyncio
async def test_список_только_своей_организации(client, db):
    await _орг(db)
    await db.добавить_организацию(id=777, name="Чужая")
    await db.добавить_пользователя(id=9, first_name="Чужой", role="admin", org_id=777)
    await db.pool.execute(
        "INSERT INTO integration_keys "
        "(org_id, prefix, secret_hash, name, created_by) "
        "VALUES (777, 'aocgchuzhoy1', 'x', 'чужой', 9)"
    )
    await client.post(ВЫПУСК, json=_тело("свой"))
    список = (await client.get(ВЫПУСК)).json()
    assert [к["name"] for к in список] == ["свой"]


@pytest.mark.asyncio
async def test_живые_выше_отозванных(client, db):
    await _орг(db)
    первый = (await client.post(ВЫПУСК, json=_тело("первый"))).json()
    await client.post("%s%d/revoke" % (ВЫПУСК, первый["id"]))
    await client.post(ВЫПУСК, json=_тело("второй"))
    имена = [к["name"] for к in (await client.get(ВЫПУСК)).json()]
    assert имена == ["второй", "первый"], "отозванный завалил собой живой"


# ── ОТЗЫВ ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_отзыв_действует_немедленно(client, db):
    """Отзыв, который не отзывает сразу, — не отзыв."""
    await _орг(db)
    выпущен = (await client.post(ВЫПУСК, json=_тело())).json()
    префикс = выпущен["prefix"]
    assert await ик.найти_живой(db.pool, префикс) is not None
    ответ = await client.post("%s%d/revoke" % (ВЫПУСК, выпущен["id"]))
    assert ответ.status_code == 200
    assert ответ.json()["is_active"] is False
    assert await ик.найти_живой(db.pool, префикс) is None


@pytest.mark.asyncio
async def test_повторный_отзыв_не_переписывает_время(client, db):
    """Иначе вторым нажатием затирается единственная запись о том, когда
    доступ закрыли."""
    await _орг(db)
    выпущен = (await client.post(ВЫПУСК, json=_тело())).json()
    первый = (await client.post("%s%d/revoke" % (ВЫПУСК, выпущен["id"]))).json()
    второй = await client.post("%s%d/revoke" % (ВЫПУСК, выпущен["id"]))
    assert второй.status_code == 404
    после = (await client.get(ВЫПУСК)).json()[0]
    assert после["revoked_at"] == первый["revoked_at"]


@pytest.mark.asyncio
async def test_чужой_ключ_отозвать_нельзя(client, db):
    """И отказ тот же, что у несуществующего, — номер можно перебрать."""
    await _орг(db)
    await db.добавить_организацию(id=777, name="Чужая")
    await db.добавить_пользователя(id=9, first_name="Чужой", role="admin", org_id=777)
    чужой = await db.pool.fetchval(
        "INSERT INTO integration_keys "
        "(org_id, prefix, secret_hash, name, created_by) "
        "VALUES (777, 'aocgchuzhoy1', 'x', 'чужой', 9) RETURNING id"
    )
    отказ = await client.post("%s%d/revoke" % (ВЫПУСК, чужой))
    несуществующий = await client.post("%s999999/revoke" % ВЫПУСК)
    assert отказ.status_code == несуществующий.status_code == 404
    assert отказ.json() == несуществующий.json()
    живой = await db.pool.fetchval(
        "SELECT revoked_at IS NULL FROM integration_keys WHERE id=$1", чужой
    )
    assert живой, "чужой ключ всё-таки отозвали"


@pytest.mark.asyncio
async def test_два_одновременных_выпуска_не_дают_третьего(client, db):
    """⚠️ ГОНКА, А НЕ ТЕОРИЯ. Счёт живых в коде без блокировки её не закрывает:
    два запроса читают «сейчас один» каждый и заводят ТРЕТИЙ живой ключ.

    Замок берётся на строку организации в той же сделке, что и вставка.
    Проверка бьёт настоящим параллелизмом — два запроса разом на живом пуле;
    на двойнике изобразить это нечем.
    """
    import asyncio

    await _орг(db)
    await client.post(ВЫПУСК, json=_тело("первый"))
    ответы = await asyncio.gather(
        client.post(ВЫПУСК, json=_тело("второй")),
        client.post(ВЫПУСК, json=_тело("третий")),
    )
    коды = sorted(о.status_code for о in ответы)
    assert коды == [200, 409], "оба запроса прошли: %s" % коды
    живых = await db.pool.fetchval(
        "SELECT count(*) FROM integration_keys WHERE revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > NOW())"
    )
    assert живых == 2, "живых ключей стало %d" % живых


@pytest.mark.asyncio
async def test_выпуск_встаёт_на_замок_строки_организации(client, db):
    """⚠️ ЗАМОК МЕРЯЕТСЯ ПРЯМО, А НЕ ЧЕРЕЗ ВЕРОЯТНОСТЬ.

    Проверка «два запроса разом» замок НЕ ловит: снятие `FOR UPDATE` её
    не покраснило — две сделки успевают разойтись во времени сами, и гонка
    просто не случается. Это и есть класс «прибор зелен, а защиты нет».

    Здесь замок берётся ИЗВНЕ и удерживается: пока чужая сделка держит строку
    организации, выпуск обязан ЖДАТЬ. Снимут `FOR UPDATE` — запрос пройдёт
    насквозь, и проверка покраснеет сразу.

    ⚠️ И ИМЕННО `FOR KEY SHARE`, А НЕ `FOR UPDATE`, И ЭТО РЕШАЮЩАЯ ДЕТАЛЬ.
    Первая редакция держала `FOR UPDATE` — и снятие замка в коде её НЕ
    покраснило: вставка ключа сама берёт на строку организации `FOR KEY SHARE`
    ради внешнего ключа, а он с `FOR UPDATE` конфликтует. Запрос ждал бы
    в любом случае, и проверка мерила бы внешний ключ вместо замка.
    `FOR KEY SHARE` со вставкой НЕ конфликтует — ждать запрос будет
    ТОЛЬКО из-за нашего `FOR UPDATE`.
    """
    import asyncio

    await _орг(db)
    async with db.pool.acquire() as соединение:
        async with соединение.transaction():
            await соединение.fetchval(
                "SELECT id FROM organizations WHERE id=1 FOR KEY SHARE"
            )
            задача = asyncio.create_task(client.post(ВЫПУСК, json=_тело("под замком")))
            await asyncio.sleep(0.5)
            assert not задача.done(), "выпуск прошёл, не дождавшись замка"
    ответ = await задача
    assert ответ.status_code == 200, ответ.text
