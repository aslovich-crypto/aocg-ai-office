"""Переотправка письма подтверждения (S-83).

ПОВОД. До этой ручки потерянное письмо означало ТУПИК: вход отдаёт 403
«Подтвердите email», попросить новое письмо негде. Обход существовал —
пройти восстановление пароля, которое с 27.08 подтверждает адрес, — но
знал о нём только тот, кто читал код.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, И ПОЧЕМУ ИМЕННО ЭТО:
  • письмо уходит тому, кому оно нужно, и НЕ уходит остальным;
  • ответ ОДИНАКОВ во всех случаях — ручка отвечает до входа, по ней
    перебирают адреса, и разные ответы выдали бы список наших клиентов;
  • новый токен ГАСИТ прежний — иначе десять запросов дают десять живых
    ключей от одной учётной записи;
  • бюджет писем ОБЩИЙ с восстановлением пароля.
"""

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def неподтверждённый(db):
    db.organizations.append(dict(id=1, name="ООО Ромашка", inn=None, type="company"))
    row = dict(
        id=7,
        first_name="Анна",
        last_name="Петрова",
        email="anna@example.com",
        password_hash="хеш",
        role="admin",
        org_id=1,
        is_active=True,
        is_email_verified=False,
        email_verify_token="старый-ключ",
        created_at=datetime.now(timezone.utc),
        email_verify_expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
    )
    db.users.append(row)
    db._uid = 7
    return row


async def _зов(client, адрес):
    return await client.post("/api/auth/resend-verification", json={"email": адрес})


@pytest.mark.asyncio
async def test_новый_токен_выдан_и_старый_погашен(client, неподтверждённый):
    """Главное: тупик закрыт — токен обновился, значит письмо ушло новое."""
    r = await _зов(client, "anna@example.com")
    assert r.status_code == 200, r.text
    assert неподтверждённый["email_verify_token"] != "старый-ключ", (
        "токен обязан смениться — иначе письмо уйдёт со ссылкой, которая "
        "у человека уже есть и, возможно, просрочена"
    )
    assert неподтверждённый["email_verify_token"], "и он не должен быть пустым"


@pytest.mark.asyncio
async def test_срок_у_нового_токена_свежий(client, неподтверждённый):
    """72 часа считаются от ПЕРЕОТПРАВКИ, а не от регистрации.

    ⚠️ Иначе переотправка была бы бессмысленной ровно в том случае, ради
    которого она нужна: у человека, чья ссылка протухла, новая рождалась бы
    мёртвой.
    """
    неподтверждённый["email_verify_expires_at"] = datetime.now(
        timezone.utc
    ) - timedelta(hours=1)
    await _зов(client, "anna@example.com")
    срок = неподтверждённый["email_verify_expires_at"]
    assert срок > datetime.now(timezone.utc) + timedelta(hours=71), (
        f"срок обязан отсчитываться заново, а он {срок}"
    )


@pytest.mark.asyncio
async def test_подтверждённому_письмо_не_уходит(client, неподтверждённый):
    """Кому слать нечего — тому не шлём, но ответ он получает тот же."""
    неподтверждённый["is_email_verified"] = True
    неподтверждённый["email_verify_token"] = None
    r = await _зов(client, "anna@example.com")
    assert r.status_code == 200
    assert неподтверждённый["email_verify_token"] is None, (
        "подтверждённому аккаунту новый токен не нужен"
    )


@pytest.mark.asyncio
async def test_отключённому_письмо_не_уходит(client, неподтверждённый):
    """Уволенный не должен получать ссылку-вход в организацию."""
    неподтверждённый["is_active"] = False
    await _зов(client, "anna@example.com")
    assert неподтверждённый["email_verify_token"] == "старый-ключ", (
        "отключённой записи токен не переиздаём"
    )


@pytest.mark.asyncio
async def test_ответ_одинаков_для_всех_четырёх_случаев(client, неподтверждённый):
    """Снаружи случаи неразличимы — иначе ручка выдаёт список наших адресов.

    ⚠️ Сравниваются РОВНО тела ответов и коды. Разведёт кто-нибудь
    формулировки «адрес не найден» и «уже подтверждён» — тест покраснеет.
    """
    неизвестный = await _зов(client, "нет-такого@example.com")

    подтверждённый_ответ = None
    неподтверждённый["is_email_verified"] = True
    подтверждённый_ответ = await _зов(client, "anna@example.com")

    неподтверждённый["is_email_verified"] = False
    неподтверждённый["is_active"] = False
    отключённый = await _зов(client, "anna@example.com")

    неподтверждённый["is_active"] = True
    обычный = await _зов(client, "anna@example.com")

    # ⚠️ Пятый случай дописан ПОСЛЕ мутации, которая его отсутствие вскрыла:
    # ответ при исчерпанном бюджете тоже обязан совпадать, иначе он сообщает,
    # что на ЭТОТ адрес письма уже просили — то есть что адрес интересен.
    for _ in range(5):
        await _зов(client, "anna@example.com")
    исчерпан = await _зов(client, "anna@example.com")

    ответы = [неизвестный, подтверждённый_ответ, отключённый, обычный, исчерпан]
    коды = {о.status_code for о in ответы}
    тела = {о.text for о in ответы}
    assert коды == {200}, f"коды различаются: {коды}"
    assert len(тела) == 1, f"тела различаются и выдают, что у нас есть: {тела}"


@pytest.mark.asyncio
async def test_бюджет_писем_общий_с_восстановлением(client, неподтверждённый, db):
    """3 письма в час на адрес — ОБЩИЕ на обе ручки, а не по три на каждую.

    ⚠️ Раздельные счётчики дали бы шесть писем в час на один адрес. Тест
    тратит бюджет через forgot-password и проверяет, что переотправка его
    видит.
    """
    for _ in range(4):
        await client.post(
            "/api/auth/forgot-password", json={"email": "anna@example.com"}
        )
    неподтверждённый["email_verify_token"] = "старый-ключ"
    await _зов(client, "anna@example.com")
    assert неподтверждённый["email_verify_token"] == "старый-ключ", (
        "бюджет исчерпан восстановлением — переотправка обязана его видеть"
    )
