"""Приглашения: гейт роли, org-scope и одноразовость (задача S-31, часть 1).

ПОЧЕМУ ЭТО ПЕРВОЕ. Приглашение — ЕДИНСТВЕННОЕ место, где посторонний
получает доступ внутрь организации: по ссылке заводится пользователь
с ролью и org_id, взятыми ИЗ ПРИГЛАШЕНИЯ. Замер покрытия 07.08.2026
(`tests/tools/cover_routes.py`) показал, что ни одна из пяти ручек этого
контура не выполнялась в тестах НИ РАЗУ — вместе со всем `auth.py`.
Гейты в коде на вид стояли; «на вид» и «проверено» — разные вещи.

ЧТО ПРОВЕРЯЕТСЯ, ПО ОБЕИМ ПОЛОВИНАМ (правило в CLAUDE.md):
не только код ответа, но и состояние — приглашение не создано, чужое
не погашено, счётчик использований не сдвинут, токен доступа не выдан.
И рядом положительный случай: админ своей орг делает всё как раньше.

ОСОБЕННОСТЬ РУЧКИ УДАЛЕНИЯ: она возвращает `{"ok": true}` ВСЕГДА, даже
когда UPDATE не задел ни строки. Проверка по коду ответа тут не значит
ничего в принципе — только состояние приглашения.
"""

from datetime import datetime, timedelta, timezone

import pytest

import app.routers.auth as auth_router
from tests.conftest import _override_user

ЧУЖАЯ_ОРГ = 2


@pytest.fixture(autouse=True)
def без_почты(monkeypatch):
    """Почтовый провайдер выключен → регистрация сразу выдаёт токены.

    Иначе ветка зависит от переменной окружения RESEND_API_KEY, и один и тот
    же тест на машине с ключом проверял бы ДРУГОЙ путь, молча.
    """
    monkeypatch.setattr(auth_router, "email_enabled", lambda: False)


@pytest.fixture
def орг(db):
    db.organizations.append(
        dict(id=1, name="ООО Ромашка", inn="7700000000", type="company")
    )
    db.organizations.append(
        dict(id=ЧУЖАЯ_ОРГ, name="ООО Чужая", inn="7800000000", type="company")
    )
    return db


def _приглашение(
    db,
    *,
    token="tok-1",
    org_id=1,
    role="employee",
    max_uses=1,
    expires_at=None,
    is_active=True,
    uses_count=0,
):
    db._invid += 1
    row = dict(
        id=db._invid,
        token=token,
        org_id=org_id,
        role=role,
        created_by=1,
        expires_at=expires_at,
        max_uses=max_uses,
        uses_count=uses_count,
        is_active=is_active,
        created_at=datetime.now(timezone.utc),
        # ⚠️ T104: колонки получателя. Без них выдача списка падала
        # KeyError — то есть тест ловил расхождение фикстуры со схемой,
        # и это правильно: строка приглашения обязана быть полной.
        email=None,
        first_name="",
        last_name="",
        sent_at=None,
    )
    db.invite_links.append(row)
    return row


# ───────────────────────── создание: только админ ──────────────────────────


@pytest.mark.asyncio
async def test_employee_cannot_create_invite(client_employee, орг):
    r = await client_employee.post("/api/invite/create", json={"role": "employee"})
    assert r.status_code == 403, r.text
    assert орг.invite_links == [], "приглашение не должно было создаться"


@pytest.mark.asyncio
async def test_accountant_cannot_create_invite(client_accountant, орг):
    r = await client_accountant.post("/api/invite/create", json={"role": "admin"})
    assert r.status_code == 403, r.text
    assert орг.invite_links == [], "приглашение не должно было создаться"


@pytest.mark.asyncio
async def test_admin_creates_invite_in_own_org(client, орг):
    """Положительный случай + org_id берётся из ТОКЕНА, а не из тела."""
    r = await client.post(
        "/api/invite/create",
        json={"role": "accountant", "max_uses": 3, "org_id": ЧУЖАЯ_ОРГ},
    )
    assert r.status_code == 200, r.text
    выдано = r.json()
    assert выдано["role"] == "accountant"
    assert выдано["token"] and выдано["token"] in выдано["invite_url"]

    (строка,) = орг.invite_links
    assert строка["org_id"] == 1, "org_id обязан приходить из токена, а не из тела"
    assert строка["uses_count"] == 0 and строка["is_active"] is True


# ───────────────────────── список: админ + своя орг ─────────────────────────


@pytest.mark.asyncio
async def test_employee_cannot_list_invites(client_employee, орг):
    _приглашение(орг)
    r = await client_employee.get("/api/invite/list")
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_invite_list_is_org_scoped(client, орг):
    свой = _приглашение(орг, token="свой")
    _приглашение(орг, token="чужой", org_id=ЧУЖАЯ_ОРГ)
    r = await client.get("/api/invite/list")
    assert r.status_code == 200, r.text
    токены = [i["token"] for i in r.json()]
    assert токены == [свой["token"]], "в списке не должно быть приглашений чужой орг"


# ───────────────────────── гашение: админ + своя орг ────────────────────────


@pytest.mark.asyncio
async def test_employee_cannot_delete_invite(client_employee, орг):
    inv = _приглашение(орг)
    r = await client_employee.delete(f"/api/invite/{inv['token']}")
    assert r.status_code == 403, r.text
    assert inv["is_active"] is True, "приглашение не должно было погаснуть"


@pytest.mark.asyncio
async def test_admin_cannot_delete_invite_of_other_org(client, орг):
    """Ручка отвечает ok ВСЕГДА — проверять можно только состояние."""
    чужое = _приглашение(орг, token="чужой", org_id=ЧУЖАЯ_ОРГ)
    r = await client.delete(f"/api/invite/{чужое['token']}")
    assert r.status_code == 200, r.text
    assert чужое["is_active"] is True, (
        "админ одной орг погасил приглашение другой — org-scope в WHERE не работает"
    )


@pytest.mark.asyncio
async def test_admin_deletes_own_invite(client, орг):
    свой = _приглашение(орг, token="свой")
    assert (await client.delete(f"/api/invite/{свой['token']}")).status_code == 200
    assert свой["is_active"] is False


# ───────────────────────── проверка ссылки (публичная) ──────────────────────


@pytest.mark.asyncio
async def test_validate_live_invite(client, орг):
    _приглашение(орг, token="живой", role="accountant")
    r = await client.get("/api/invite/validate/живой")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["is_valid"] is True
    assert d["role"] == "accountant" and d["org_name"] == "ООО Ромашка"


@pytest.mark.asyncio
async def test_validate_rejects_dead_invites(client, орг):
    вчера = datetime.now(timezone.utc) - timedelta(hours=1)
    _приглашение(орг, token="истёкший", expires_at=вчера)
    _приглашение(орг, token="исчерпанный", max_uses=1, uses_count=1)
    _приглашение(орг, token="погашенный", is_active=False)
    for token in ("истёкший", "исчерпанный", "погашенный"):
        d = (await client.get(f"/api/invite/validate/{token}")).json()
        assert d["is_valid"] is False, f"«{token}» не должен считаться годным"


@pytest.mark.asyncio
async def test_validate_unknown_token_leaks_nothing(client, орг):
    d = (await client.get("/api/invite/validate/выдуманный")).json()
    assert d["is_valid"] is False
    assert d["role"] is None and d["org_name"] is None, (
        "по несуществующей ссылке не должно быть видно ни роли, ни названия орг"
    )


# ─────────────────── регистрация по ссылке: главное место ───────────────────


@pytest.mark.asyncio
async def test_register_by_invite_takes_role_and_org_from_invite(client, орг):
    _приглашение(орг, token="ссылка", role="employee")
    r = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "ссылка",
            "email": "Novichok@example.com",
            "password": "парольдлинный",
            "first_name": "Новичок",
            # попытка навязать своё — модель таких полей не знает, и это
            # ровно то, что здесь проверяется
            "role": "admin",
            "org_id": ЧУЖАЯ_ОРГ,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"], "при выключенной почте токен выдаётся сразу"

    (новый,) = [u for u in орг.users if u["email"] == "novichok@example.com"]
    assert новый["role"] == "employee", "роль обязана приходить из приглашения"
    assert новый["org_id"] == 1, "организация обязана приходить из приглашения"
    assert новый["password_hash"] and новый["password_hash"] != "парольдлинный"


@pytest.mark.asyncio
async def test_one_use_invite_burns_after_first_registration(client, орг):
    inv = _приглашение(орг, token="одноразовая", max_uses=1)
    первая = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "одноразовая",
            "email": "a@example.com",
            "password": "парольдлинный",
        },
    )
    assert первая.status_code == 200, первая.text
    assert inv["uses_count"] == 1 and inv["is_active"] is False

    вторая = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "одноразовая",
            "email": "b@example.com",
            "password": "парольдлинный",
        },
    )
    assert вторая.status_code == 400, вторая.text
    assert "access_token" not in вторая.json(), (
        "доступ по погашенной ссылке не выдаётся"
    )
    assert [u["email"] for u in орг.users] == ["a@example.com"], (
        "второй пользователь не должен был появиться"
    )


@pytest.mark.asyncio
async def test_expired_invite_creates_nobody(client, орг):
    inv = _приглашение(
        орг,
        token="истёкшая",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    r = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "истёкшая",
            "email": "c@example.com",
            "password": "парольдлинный",
        },
    )
    assert r.status_code == 400, r.text
    assert "access_token" not in r.json()
    assert орг.users == [] and inv["uses_count"] == 0


@pytest.mark.asyncio
async def test_taken_email_does_not_burn_invite(client, орг):
    """409 не должен расходовать ссылку — иначе опечатка сжигает приглашение."""
    inv = _приглашение(орг, token="ссылка")
    орг.users.append(
        dict(
            id=99, email="zanyat@example.com", org_id=1, role="employee", is_active=True
        )
    )
    r = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "ссылка",
            "email": "zanyat@example.com",
            "password": "парольдлинный",
        },
    )
    assert r.status_code == 409, r.text
    assert inv["uses_count"] == 0 and inv["is_active"] is True
    assert len(орг.users) == 1, "второй пользователь с тем же email не должен появиться"


@pytest.mark.asyncio
async def test_short_password_rejected_before_invite_is_spent(client, орг):
    inv = _приглашение(орг, token="ссылка")
    r = await client.post(
        "/api/auth/register-by-invite",
        json={"token": "ссылка", "email": "d@example.com", "password": "1234"},
    )
    assert r.status_code == 400, r.text
    assert орг.users == [] and inv["uses_count"] == 0


@pytest.mark.asyncio
async def test_invite_of_other_org_registers_into_that_org(client, орг):
    """Ссылка чужой орг заводит человека В ТУ орг, а не в нашу.

    Проверка того, что org_id идёт из приглашения ДО конца: подделать
    принадлежность, зная чужой токен, нельзя — но и «прилипнуть» к орг
    приглашающего тоже.
    """
    _приглашение(орг, token="чужая-ссылка", org_id=ЧУЖАЯ_ОРГ, role="accountant")
    r = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "чужая-ссылка",
            "email": "e@example.com",
            "password": "парольдлинный",
        },
    )
    assert r.status_code == 200, r.text
    (новый,) = орг.users
    assert новый["org_id"] == ЧУЖАЯ_ОРГ and новый["role"] == "accountant"


# ─────────── роль смотрящего не влияет на публичные ручки контура ───────────


@pytest.mark.asyncio
async def test_validate_and_register_need_no_admin(client, орг):
    """Обе публичные ручки работают под любой ролью — они до входа.

    Проверяется отдельно, потому что соблазн «закрыть всё админом» ломает
    именно эти две: их дёргает человек, которого в системе ещё нет.
    """
    _приглашение(орг, token="публичная")
    _override_user("employee", user_id=2)
    assert (await client.get("/api/invite/validate/публичная")).json()[
        "is_valid"
    ] is True
    r = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "публичная",
            "email": "f@example.com",
            "password": "парольдлинный",
        },
    )
    assert r.status_code == 200, r.text


# ───────────────── S-24: роль из белого списка, а не любая строка ────────────


@pytest.mark.asyncio
async def test_invite_with_unknown_role_is_rejected(client, орг):
    """Роль приглашения — Literal, а не str: «суперадмин» отвергается формой."""
    r = await client.post("/api/invite/create", json={"role": "суперадмин"})
    assert r.status_code == 422, r.text
    assert орг.invite_links == [], "приглашение с чужой ролью не должно создаться"


@pytest.mark.asyncio
async def test_invite_roles_from_whitelist_still_work(client, орг):
    """Положительная половина: все три настоящие роли выдаются как раньше."""
    for роль in ("employee", "accountant", "admin"):
        r = await client.post("/api/invite/create", json={"role": роль})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == роль
    assert [i["role"] for i in орг.invite_links] == ["employee", "accountant", "admin"]


@pytest.mark.asyncio
async def test_old_invite_with_bad_role_downgrades_to_employee(client, орг):
    """Приглашение, выданное ДО белого списка, не даёт неизвестных прав.

    Валидация модели закрывает только новые ссылки; в базе могут лежать
    старые. Неизвестная роль понижается до employee — наименьшие права,
    а не отказ: человек по ссылке не виноват, что ему выдали ерунду.
    """
    _приглашение(орг, token="старая", role="суперадмин")
    r = await client.post(
        "/api/auth/register-by-invite",
        json={"token": "старая", "email": "z@example.com", "password": "парольдлинный"},
    )
    assert r.status_code == 200, r.text
    (новый,) = [u for u in орг.users if u["email"] == "z@example.com"]
    assert новый["role"] == "employee", "неизвестная роль обязана понижаться"


# ─────────────────── T104 этап ②: приглашение знает, кому ───────────────────


@pytest.mark.asyncio
async def test_приглашение_с_почтой_шлёт_письмо_и_помнит_получателя(
    client, db, monkeypatch
):
    """⚠️ `send_invite_notification` была НАПИСАНА И НЕ ВЫЗЫВАЛАСЬ НИОТКУДА
    (T96): функция, шаблон и канал существовали, не хватало ровно
    получателя. Теперь он есть — и письмо обязано уйти.
    """
    ушло = []
    monkeypatch.setattr(
        auth_router, "send_invite_notification", lambda *a: ушло.append(a) or True
    )
    r = await client.post(
        "/api/invite/create",
        json={"role": "employee", "email": "Novyi@Example.COM", "first_name": "Пётр"},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    # почта приводится к нижнему регистру и обрезается — иначе один человек
    # заводится дважды разным написанием
    assert d["email"] == "novyi@example.com"
    assert d["first_name"] == "Пётр"
    assert d["sent_at"], "отметка об отправке обязана проставиться"
    assert d["invite_url"].endswith(d["token"])
    assert len(ушло) == 1, "письмо не ушло"
    assert ушло[0][0] == "novyi@example.com"


@pytest.mark.asyncio
async def test_приглашение_без_почты_письма_не_шлёт(client, db, monkeypatch):
    """Ссылку по-прежнему можно создать «в никуда» и передать руками."""
    ушло = []
    monkeypatch.setattr(
        auth_router, "send_invite_notification", lambda *a: ушло.append(a) or True
    )
    r = await client.post("/api/invite/create", json={"role": "employee"})
    assert r.status_code == 200, r.text
    assert r.json()["email"] is None
    assert r.json()["sent_at"] is None
    assert not ушло, "письма быть не должно — некому"


@pytest.mark.asyncio
async def test_повторная_отправка_шлёт_ТОТ_ЖЕ_токен(client, db, monkeypatch):
    """⚠️ Новый токен на каждую переотправку плодил бы живые приглашения:
    старое осталось бы действующим, и в списке росли бы дубли на одного.
    """
    ушло = []
    monkeypatch.setattr(
        auth_router, "send_invite_notification", lambda *a: ушло.append(a) or True
    )
    создано = (
        await client.post(
            "/api/invite/create", json={"role": "employee", "email": "a@b.ru"}
        )
    ).json()
    ушло.clear()

    r = await client.post(f"/api/invite/{создано['token']}/resend")
    assert r.status_code == 200, r.text
    assert len(ушло) == 1
    assert ушло[0][1].endswith(создано["token"]), "токен обязан быть ТОТ ЖЕ"
    assert len(db.invite_links) == 1, "второе приглашение заводиться не должно"


@pytest.mark.asyncio
async def test_повторная_отправка_без_почты_отказывает_честно(client, db):
    создано = (
        await client.post("/api/invite/create", json={"role": "employee"})
    ).json()
    r = await client.post(f"/api/invite/{создано['token']}/resend")
    assert r.status_code == 409, r.text
    assert "нет почты" in r.json()["detail"]


@pytest.mark.asyncio
async def test_список_показывает_статус_и_получателя(client, db):
    await client.post(
        "/api/invite/create",
        json={"role": "employee", "email": "kto@example.com", "first_name": "Анна"},
    )
    r = await client.get("/api/invite/list")
    assert r.status_code == 200, r.text
    строка = r.json()[0]
    assert строка["email"] == "kto@example.com"
    assert строка["first_name"] == "Анна"
    assert строка["статус"] == "приглашён, ожидает"
    assert строка["sent_at"], "отметка об отправке обязана быть в списке"
