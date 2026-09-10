# -*- coding: utf-8 -*-
"""События уведомлений при смене статуса отчёта (T159) — на живой базе.

⚠️ ПОЧЕМУ ЗДЕСЬ, А НЕ НА ДВОЙНИКЕ. Проверяется, что строка события легла
в базу С ПРАВИЛЬНЫМ АДРЕСАТОМ — то есть ровно то, что двойник изображал бы
по нашему же описанию. Адресат вычисляется запросом (кто автор отчёта, кто
управляющие организации), и подменять этот запрос толкованием — значит
проверять собственную выдумку (класс T136).
"""

from datetime import date, timedelta, timezone
from datetime import datetime as _dt

import pytest
import pytest_asyncio

from app.routers import auth as модуль_входа
from app.routers import reports as модуль_отчётов


@pytest_asyncio.fixture
async def контора(db):
    """Админ (id=1, он же смотрящий), бухгалтер и сотрудник-автор."""
    await db.добавить_пользователя(
        id=1, first_name="Админ", role="admin", email="admin@example.com"
    )
    await db.добавить_пользователя(
        id=5, first_name="Бух", role="accountant", email="buh@example.com"
    )
    await db.добавить_пользователя(
        id=2,
        first_name="Иван",
        last_name="Петров",
        role="employee",
        email="ivan@example.com",
    )
    await db.добавить_отчёт(
        id=1, title="Отчёт за май", user_id=2, total=5000, created=date(2026, 5, 10)
    )
    return db


async def события(db, кому=None):
    условие = " WHERE user_id=$1" if кому else ""
    строки = await db.pool.fetch(
        f"SELECT * FROM notifications{условие} ORDER BY id", *([кому] if кому else [])
    )
    return [dict(с) for с in строки]


@pytest.mark.asyncio
async def test_отклонение_без_причины_отвергается(client, db, контора):
    """⚠️ ТРЕБОВАНИЕ ВЛАДЕЛЬЦА: без причины уведомление не экономит ничего.

    Человек всё равно пойдёт выяснять, что не так, — значит письмо и точка
    были потрачены зря. Поэтому отказ без причины не принимается вовсе.
    """
    r = await client.patch("/api/reports/1", json={"status": "Отклонён"})
    assert r.status_code == 400, r.text
    assert "причину" in r.json()["detail"].lower()
    assert await события(db) == [], "события быть не должно — отказ не состоялся"


@pytest.mark.asyncio
async def test_отклонение_с_причиной_доходит_до_автора(client, db, контора):
    """Главное событие: деньги не вернули, и человек узнаёт об этом сразу."""
    r = await client.patch(
        "/api/reports/1",
        json={"status": "Отклонён", "reason": "нет чека на 1200 ₽"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["reject_reason"] == "нет чека на 1200 ₽", "причина живёт в отчёте"

    строки = await события(db)
    assert len(строки) == 1, "ровно одно событие — автору"
    (событие,) = строки
    assert событие["user_id"] == 2, "адресат — автор отчёта, а не тот, кто отклонил"
    assert событие["kind"] == "report_rejected"
    assert событие["body"] == "нет чека на 1200 ₽", "причина уходит в уведомление"
    assert событие["report_id"] == 1
    assert событие["read_at"] is None, "новое событие непрочитано"


@pytest.mark.asyncio
async def test_одобрение_доходит_до_автора(client, db, контора):
    r = await client.patch("/api/reports/1", json={"status": "Одобрен"})
    assert r.status_code == 200, r.text
    (событие,) = await события(db)
    assert событие["user_id"] == 2 and событие["kind"] == "report_approved"


@pytest.mark.asyncio
async def test_отправка_на_проверку_будит_всех_управляющих(
    client_employee, db, контора
):
    """⚠️ КОМУ — ОТВЕТ ВЛАДЕЛЬЦА: всем, у кого есть право видеть отчёт.

    И каждому СВОЯ строка: иначе «бухгалтер прочёл» гасило бы точку
    у администратора.
    """
    r = await client_employee.patch("/api/reports/1", json={"status": "На проверке"})
    assert r.status_code == 200, r.text
    строки = await события(db)
    assert {с["user_id"] for с in строки} == {1, 5}, "админ и бухгалтер"
    assert all(с["kind"] == "report_submitted" for с in строки)
    assert all("Иван Петров" in (с["body"] or "") for с in строки), (
        "управляющему нужно знать, ЧЕЙ отчёт пришёл"
    )


@pytest.mark.asyncio
async def test_себе_событие_не_пишется(client, db, контора):
    """⚠️ ПРАВИЛО ВЛАДЕЛЬЦА: уведомление, которое человек может предсказать
    сам, обесценивает остальные.

    Здесь отчёт принадлежит САМОМУ смотрящему (админу): он же его и одобряет.
    Событие в этом случае — шум, и его быть не должно.
    """
    await db.добавить_отчёт(id=7, title="Свой отчёт", user_id=1, total=100)
    r = await client.patch("/api/reports/7", json={"status": "Одобрен"})
    assert r.status_code == 200, r.text
    assert await события(db) == []


@pytest.mark.asyncio
async def test_чужая_организация_событий_не_получает(client, db, контора):
    """org-scope: управляющий чужой организации не адресат наших событий."""
    await db.добавить_пользователя(
        id=90, first_name="Чужой", role="admin", org_id=777, email="x@example.com"
    )
    await client.patch("/api/reports/1", json={"status": "Отклонён", "reason": "мимо"})
    строки = await события(db)
    assert all(с["user_id"] != 90 for с in строки)
    assert all(с["org_id"] == 1 for с in строки)


@pytest.mark.asyncio
async def test_погашенный_управляющий_событий_не_получает(client_employee, db, контора):
    """⚠️ ДЫРУ НАШЛА МУТАЦИЯ, А НЕ ЧТЕНИЕ КОДА (04.09.2026).

    Снятое из запроса `AND is_active = true` не покраснело НИ НА ОДНОМ
    из шести тестов: уволенный бухгалтер продолжал бы получать отчёты
    своей бывшей организации, и заметить это было бы нечем.
    """
    await db.погасить(5)
    r = await client_employee.patch("/api/reports/1", json={"status": "На проверке"})
    assert r.status_code == 200, r.text
    строки = await события(db)
    assert {с["user_id"] for с in строки} == {1}, "только живой админ"


# ───────────────────── ручки колокольчика ────────────────────────────────────


@pytest.mark.asyncio
async def test_список_отдаёт_только_свои_события(client, db, контора):
    """⚠️ ЧУЖИХ УВЕДОМЛЕНИЙ НЕ ВИДИТ НИКТО, включая администратора.

    Событие — личная почта, а не общий журнал; смотрящий здесь админ,
    и он НЕ должен видеть событие сотрудника.
    """
    await client.patch(
        "/api/reports/1", json={"status": "Отклонён", "reason": "нет чека"}
    )
    r = await client.get("/api/notifications/")
    assert r.status_code == 200, r.text
    тело = r.json()
    assert тело["items"] == [] and тело["unread"] == 0, тело


@pytest.mark.asyncio
async def test_адресат_видит_событие_и_точку(client, as_role, db, контора):
    """⚠️ ОДИН КЛИЕНТ, РОЛЬ МЕНЯЕТСЯ ЯВНО. Две клиентские фикстуры в одном
    тесте перетирают общую подмену `get_current_user`, и оба запроса уходят
    от последнего смотрящего — первая редакция теста так и делала: админ
    «отклонял» отчёт, будучи сотрудником, получал 403, и событие не рождалось
    вовсе."""
    await client.patch(
        "/api/reports/1", json={"status": "Отклонён", "reason": "нет чека на 1200 ₽"}
    )
    as_role("employee", user_id=2)
    r = await client.get("/api/notifications/")
    тело = r.json()
    assert тело["unread"] == 1, "точка обязана загореться"
    (событие,) = тело["items"]
    assert событие["kind"] == "report_rejected"
    assert событие["body"] == "нет чека на 1200 ₽", "причина видна в списке"
    assert событие["read"] is False


@pytest.mark.asyncio
async def test_открытие_списка_гасит_точку(client, as_role, db, контора):
    """Решение владельца: прочитанность — открытием списка, а не поштучно."""
    await client.patch(
        "/api/reports/1", json={"status": "Отклонён", "reason": "нет чека"}
    )
    as_role("employee", user_id=2)
    прочитано = await client.post("/api/notifications/read")
    assert прочитано.json() == {"read": 1}
    тело = (await client.get("/api/notifications/")).json()
    assert тело["unread"] == 0, "точка погасла"
    assert тело["items"][0]["read"] is True, "но само событие осталось в списке"


@pytest.mark.asyncio
async def test_чужие_события_не_гасятся(client, as_role, db, контора):
    """Пометка прочитанным трогает только свои строки."""
    await db.pool.execute(
        "INSERT INTO notifications (user_id, org_id, kind, title) "
        "VALUES (1, 1, 'report_submitted', 'Чужое')"
    )
    await client.patch("/api/reports/1", json={"status": "Одобрен"})
    as_role("employee", user_id=2)
    await client.post("/api/notifications/read")
    чужое = await db.pool.fetchrow(
        "SELECT read_at FROM notifications WHERE user_id=1 AND title='Чужое'"
    )
    assert чужое["read_at"] is None, "сотрудник погасил событие администратора"


# ═══ ВТОРОЙ КАНАЛ: ПИСЬМА ══════════════════════════════════════════════════
#
# ⚠️ ДО 10.09.2026 ПИСЕМ НЕ КАСАЛСЯ НИ ОДИН ТЕСТ. Замер того дня: поиск
# `send_report_status_email|send_report_submitted_email|send_invite_accepted_email`
# по всему каталогу `tests/` давал НОЛЬ совпадений. Постановка T159 при этом
# требует ровно обратного: «событие одно, каналов два; разведи их — появятся
# два списка того, что считается событием, и разойдутся они молча».
# Прибор был у одного канала из двух, и половина работы держалась на памяти.
#
# ⚠️ ПРОВЕРЯЕМ ФАКТ ВЫЗОВА, А НЕ SMTP. Настоящая отправка — чужой сервер;
# её живьём принимает владелец (приёмка 10.09.2026 прошла, письмо об
# отклонении дошло с причиной). Здесь стережём то, что в нашей власти:
# что вызов ПОСТАВЛЕН, с тем адресом и с той причиной.
#
# ⚠️ ФОНОВЫЕ ЗАДАЧИ ПОД ASGITransport ИСПОЛНЯЮТСЯ — проверено отдельно
# 10.09.2026, иначе весь этот раздел был бы зелёным вхолостую.


@pytest.fixture
def письма(monkeypatch):
    """Перехват обоих писем об отчёте. Возвращает список кортежей аргументов."""
    ушло = []
    monkeypatch.setattr(
        модуль_отчётов,
        "send_report_status_email",
        lambda *а: ушло.append(("статус",) + а),
    )
    monkeypatch.setattr(
        модуль_отчётов,
        "send_report_submitted_email",
        lambda *а: ушло.append(("на проверку",) + а),
    )
    return ушло


@pytest.mark.asyncio
async def test_письмо_об_отклонении_уходит_с_причиной(client, db, контора, письма):
    """⚠️ ТО САМОЕ ПИСЬМО, которое владелец увидел живьём 10.09.2026."""
    r = await client.patch(
        "/api/reports/1", json={"status": "Отклонён", "reason": "нет чека на 1200 ₽"}
    )
    assert r.status_code == 200, r.text
    assert len(письма) == 1, "ровно одно письмо — автору отчёта"
    вид, адрес, название, статус, причина, _ссылка = письма[0]
    assert вид == "статус"
    assert адрес == "ivan@example.com", "адресат письма — автор, а не отклонивший"
    assert статус == "Отклонён"
    assert причина == "нет чека на 1200 ₽", (
        "причина обязана доехать ВТОРЫМ каналом тоже: человек читает почту, "
        "а не колокольчик"
    )
    assert название == "Отчёт за май"


@pytest.mark.asyncio
async def test_письмо_об_одобрении_уходит(client, db, контора, письма):
    """⚠️ ЭТО СОБЫТИЕ ЖИВЬЁМ НЕ ПРОВЕРЯЛОСЬ (граница приёмки 10.09.2026).

    Ветка кода та же, что у отклонения, — но «та же ветка» это довод,
    а не замер. Здесь замер.
    """
    r = await client.patch("/api/reports/1", json={"status": "Одобрен"})
    assert r.status_code == 200, r.text
    assert len(письма) == 1
    вид, адрес, _название, статус, причина, _ссылка = письма[0]
    assert (вид, адрес, статус) == ("статус", "ivan@example.com", "Одобрен")
    assert причина == "", "у одобрения причины нет, и пустая строка — не None"


@pytest.mark.asyncio
async def test_письмо_на_проверку_уходит_всем_управляющим(
    client_employee, db, контора, письма
):
    """Столько же писем, сколько строк события: один список на оба канала."""
    r = await client_employee.patch("/api/reports/1", json={"status": "На проверке"})
    assert r.status_code == 200, r.text
    адреса = sorted(п[1] for п in письма)
    assert адреса == ["admin@example.com", "buh@example.com"], (
        "письмо каждому управляющему; автору себе — не пишем"
    )
    строки = await события(db)
    assert len(строки) == len(письма), (
        "число писем обязано совпадать с числом событий — иначе каналы "
        "разошлись, а это ровно то, чего требует не допускать постановка"
    )


# ═══ ПОВТОРНОЕ НАЖАТИЕ ═════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_повторный_тот_же_статус_не_плодит_событий(client, db, контора, письма):
    """⚠️ ПОВТОР ПРИ ПЛОХОЙ СЕТИ — ОБЫЧНОЕ ДЕЛО, А НЕ ОШИБКА ЧЕЛОВЕКА.

    Решение владельца 10.09.2026 (вариант ⓐ): статус переписываем молча,
    событие и письмо НЕ шлём. Против 409 довод прямой: «нажал, ответ
    не дошёл, нажал снова — и получил ошибку на ВЕРНОМ действии».
    """
    п = {"status": "Отклонён", "reason": "нет чека"}
    первый = await client.patch("/api/reports/1", json=п)
    второй = await client.patch("/api/reports/1", json=п)
    assert первый.status_code == 200 and второй.status_code == 200, "повтор безобиден"
    assert первый.json()["receiptIds"] == второй.json()["receiptIds"], (
        "ответ ТОЙ ЖЕ формы: фронт кладёт его в список, и вторая форма "
        "разъехалась бы с базой на пустом месте"
    )
    assert len(await события(db)) == 1, "второе нажатие событий не плодит"
    assert len(письма) == 1, "и писем тоже"


# ═══ СОБЫТИЕ №4: ПО ПРИГЛАШЕНИЮ ЗАВЕЛАСЬ УЧЁТНАЯ ЗАПИСЬ ════════════════════
#
# ⚠️ ДО 10.09.2026 ЭТОГО СОБЫТИЯ НЕ БЫЛО ВОВСЕ, И ЭТО КЛАСС T149 «СДЕЛАЛИ
# И НЕ ПОДКЛЮЧИЛИ»: вид `invite_accepted` был объявлен (`app/notifications.py`),
# письмо `send_invite_accepted_email` было написано (`app/email_service.py`) —
# а вызовов не было ни одного. Аудит 06.09 заглянул в модуль, увидел
# объявление и счёл работу сделанной.


@pytest_asyncio.fixture
async def приглашение(db):
    """Ссылка на конкретный адрес; срок — завтра, одно применение."""
    await db.pool.execute(
        """INSERT INTO invite_links
             (token, org_id, role, created_by, expires_at, max_uses, email)
           VALUES ($1,1,'employee',1,$2,1,$3)""",
        "прогл",
        _dt.now(timezone.utc) + timedelta(days=1),
        "novy@example.com",
    )
    return db


async def _завестись(client):
    return await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "прогл",
            "email": "novy@example.com",
            "password": "парольдлинный",
            "first_name": "Пётр",
            "last_name": "Сидоров",
        },
    )


@pytest.mark.asyncio
async def test_приглашение_принято_будит_управляющих_при_выключенной_почте(
    client, db, контора, приглашение, monkeypatch
):
    """Ветка auto_verify: человек вошёл сразу."""
    monkeypatch.setattr(модуль_входа, "email_enabled", lambda: False)
    ушло = []
    monkeypatch.setattr(
        модуль_входа, "send_invite_accepted_email", lambda *а: ушло.append(а)
    )
    r = await _завестись(client)
    assert r.status_code == 200, r.text
    строки = await события(db)
    assert len(строки) == 2, "по строке админу и бухгалтеру"
    assert {с["user_id"] for с in строки} == {1, 5}
    assert {с["kind"] for с in строки} == {"invite_accepted"}
    assert "Пётр Сидоров" in строки[0]["title"], "управляющему нужно ИМЯ, а не id"
    assert строки[0]["body"] == "Завёл учётную запись по приглашению", (
        "⚠️ ТЕКСТ ЧЕСТНЫЙ (требование владельца 10.09.2026): при включённой "
        "почте человек в этот момент ещё НЕ ВОШЁЛ, и «принял приглашение» "
        "было бы неправдой в половине случаев"
    )
    assert sorted(а[0] for а in ушло) == ["admin@example.com", "buh@example.com"]


@pytest.mark.asyncio
async def test_приглашение_принято_будит_управляющих_и_при_включённой_почте(
    client, db, контора, приглашение, monkeypatch
):
    """⚠️ ВТОРАЯ ВЕТКА — ТА, ЧТО РАБОТАЕТ НА ПРОДЕ.

    Здесь ручка отвечает `{"verified": False}` и уходит ранним `return` мимо
    `_auth_payload`. Вызов события стоит ДО развилки именно поэтому: поставь
    его после — работал бы ровно наоборот тому, как читается глазами.
    """
    monkeypatch.setattr(модуль_входа, "email_enabled", lambda: True)
    monkeypatch.setattr(модуль_входа, "send_verification_email", lambda *а: True)
    ушло = []
    monkeypatch.setattr(
        модуль_входа, "send_invite_accepted_email", lambda *а: ушло.append(а)
    )
    r = await _завестись(client)
    assert r.status_code == 200 and r.json().get("verified") is False, r.text
    строки = await события(db)
    assert len(строки) == 2, "событие пишется и до подтверждения почты"
    assert len(ушло) == 2, "и письма тоже"


@pytest.mark.asyncio
async def test_завёдшийся_админ_себе_события_не_пишет(client, db, контора, monkeypatch):
    """Приглашение на роль администратора: новый человек сам управляющий."""
    monkeypatch.setattr(модуль_входа, "email_enabled", lambda: False)
    monkeypatch.setattr(модуль_входа, "send_invite_accepted_email", lambda *а: True)
    await db.pool.execute(
        """INSERT INTO invite_links
             (token, org_id, role, created_by, expires_at, max_uses, email)
           VALUES ('адм',1,'admin',1,$1,1,'novy@example.com')""",
        _dt.now(timezone.utc) + timedelta(days=1),
    )
    r = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "адм",
            "email": "novy@example.com",
            "password": "парольдлинный",
            "first_name": "Пётр",
            "last_name": "Сидоров",
        },
    )
    assert r.status_code == 200, r.text
    строки = await события(db)
    assert {с["user_id"] for с in строки} == {1, 5}, (
        "себе не пишем: новый админ и так знает, что завёлся"
    )


# ═══ ГАШЕНИЕ: ТОЛЬКО ТО, ЧТО ЧЕЛОВЕК ВИДЕЛ ═════════════════════════════════
#
# ⚠️ ДО 10.09.2026 ГАСИЛОСЬ ВСЁ НЕПРОЧИТАННОЕ БЕЗ ПРЕДЕЛА, а список отдаёт
# двадцать. У человека с двадцатью пятью событиями открытие шторки помечало
# прочитанными все двадцать пять — пять из них на экране не побывали.
# Вернуть их нечем: точка погасла, в списке они двадцать первые.
# Решение владельца: гасить только показанные, листания не делать.


async def _событий(db, сколько, кому=2):
    """N событий подряд; чем больше id, тем новее (порядок выдачи — DESC)."""
    for н in range(сколько):
        await db.pool.execute(
            "INSERT INTO notifications (user_id, org_id, kind, title) "
            "VALUES ($1,1,'report_rejected',$2)",
            кому,
            f"Событие {н + 1}",
        )


@pytest.mark.asyncio
async def test_гасятся_только_присланные(client_employee, db, контора):
    """Клиент говорит, что нарисовал, — гасим ровно это."""
    await _событий(db, 25)
    все = await события(db, кому=2)
    видимые = [с["id"] for с in все[-20:]]  # двадцать новейших
    r = await client_employee.post("/api/notifications/read", json={"ids": видимые})
    assert r.status_code == 200, r.text
    assert r.json()["read"] == 20, "погашено ровно двадцать показанных"
    осталось = [с for с in await события(db, кому=2) if с["read_at"] is None]
    assert len(осталось) == 5, (
        "пять невыведенных обязаны остаться непрочитанными: "
        "пометить невиденное — потерять уведомление навсегда"
    )


@pytest.mark.asyncio
async def test_пустое_тело_гасит_первые_двадцать(client_employee, db, контора):
    """⚠️ ЗАКОННЫЙ ПУТЬ, А НЕ ЗАБЫВЧИВОСТЬ КЛИЕНТА.

    Так ведёт себя старый клиент и любой сторонний вызов. Поведение хуже
    точного — есть щель между GET и POST, — но не лживее прежнего: гасим
    ровно столько, сколько отдаём.
    """
    await _событий(db, 25)
    r = await client_employee.post("/api/notifications/read")
    assert r.status_code == 200, r.text
    assert r.json()["read"] == 20
    осталось = [с for с in await события(db, кому=2) if с["read_at"] is None]
    assert len(осталось) == 5


@pytest.mark.asyncio
async def test_чужой_id_погасить_нельзя(client_employee, db, контора):
    """user_id в условии остаётся при обоих путях."""
    await db.pool.execute(
        "INSERT INTO notifications (user_id, org_id, kind, title) "
        "VALUES (1,1,'report_submitted','Чужое')"
    )
    (чужое,) = await события(db, кому=1)
    r = await client_employee.post(
        "/api/notifications/read", json={"ids": [чужое["id"]]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["read"] == 0, "присланный чужой id просто не находится"
    (оно,) = await события(db, кому=1)
    assert оно["read_at"] is None, "чужое событие осталось непрочитанным"


# ═══ ЭКРАНИРОВАНИЕ В ПИСЬМАХ ═══════════════════════════════════════════════
#
# ⚠️ ГИГИЕНА, А НЕ БЕЗОПАСНОСТЬ ВЫСОКОГО ПОЛЁТА (слово владельца 10.09.2026).
# Название отчёта и причину печатает человек, а письмо собирается СТРОКОЙ:
# угловая скобка рвёт вёрстку письма, перевод строки в теме подделывает
# заголовки и роняет сборку исключением ДО всякой защиты — `_send` собирает
# письмо вне `try`.
#
# ⚠️ НА ЭКРАНЕ ЭТОЙ ДЫРЫ НЕТ, и это проверено: React ставит текст узлом
# и экранирует сам, `dangerouslySetInnerHTML` во всём `src/` — ноль.


def test_угловая_скобка_в_теле_письма_экранируется():
    from app.email_service import тело

    assert тело("Отчёт <май> & Ко") == "Отчёт &lt;май&gt; &amp; Ко"
    assert тело(None) == "", "пустое значение не должно давать «None» в письме"


def test_перевод_строки_в_теме_схлопывается():
    from app.email_service import тема

    # Ровно та подделка, ради которой правка: вторая строка стала бы
    # отдельным заголовком письма.
    assert тема("Отчёт\nBcc: чужой@пример") == "Отчёт Bcc: чужой@пример"
    assert тема("Отчёт\r\n\tза май") == "Отчёт за май"


@pytest.mark.asyncio
async def test_письмо_с_угловой_скобкой_в_причине_не_ломается(
    client, db, контора, письма, monkeypatch
):
    """Сквозной случай: человек пишет скобку — письмо уходит целым."""
    ушло = []
    monkeypatch.setattr(
        модуль_отчётов,
        "send_report_status_email",
        lambda *а: ушло.append(а),
    )
    r = await client.patch(
        "/api/reports/1",
        json={"status": "Отклонён", "reason": "нет чека <на 1200 ₽>"},
    )
    assert r.status_code == 200, r.text
    assert len(ушло) == 1, "письмо ушло, а не упало на сборке"
    # ⚠️ В АРГУМЕНТ ПРИЧИНА ИДЁТ СЫРОЙ — экранирование живёт в сборщике
    # письма, а не в вызывающем. Иначе экранированное уехало бы и в текст
    # события на экране, где React экранирует второй раз: человек прочёл бы
    # «нет чека &lt;на 1200 ₽&gt;».
    assert ушло[0][3] == "нет чека <на 1200 ₽>"
    (событие,) = await события(db)
    assert событие["body"] == "нет чека <на 1200 ₽>", "на экране — как написали"
