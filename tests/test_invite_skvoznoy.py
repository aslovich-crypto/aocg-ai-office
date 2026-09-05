# -*- coding: utf-8 -*-
"""Сквозной проход приглашения: выписали → перешли → зарегистрировались → вошли.

⚠️ ЗАЧЕМ ОТДЕЛЬНЫМ ФАЙЛОМ И ЗАЧЕМ ВООБЩЕ (T104, этап ⑥). Каждое звено цепочки
проверено по отдельности и все проверки зелёные — а цепочки целиком не проходил
НИКТО. Замер 31.08.2026: `invite/create` и `register-by-invite` встречаются
вместе в `test_invites_acl.py`, но `auth/login` там не вызывается ни разу; вход
проверяется в `test_public_endpoints.py`, где нет создания приглашения. **Между
звеньями и живут поломки этого дня:** роль `manager`, которой не бывает, белый
экран на отказе, беспарольная строка вместо человека — ни одна из них не
ломала отдельного звена.

⚠️ ЧЕГО ЭТОТ ТЕСТ НЕ ЗАМЕНЯЕТ, И ЭТО НАДО ЗНАТЬ: он идёт по двойнику БД, а не
по живой базе, и не касается ни письма, ни экрана. Живая приёмка («нажал →
пришло письмо → открылась ссылка») остаётся за владельцем. См. [[T135]] —
прибора, проходящего путь целиком против живой базы, у нас нет ни одного.
"""

import pytest


@pytest.fixture
def орг(db):
    """⚠️ Своя копия, а не импорт из `test_invites_acl`: фикстуры между файлами
    таскать нельзя, а без организации приглашение выписать не на что."""
    db.organizations.append(
        dict(id=1, name="ООО Ромашка", inn="7700000000", type="company")
    )
    return db


@pytest.mark.asyncio
async def test_приглашение_проходит_путь_целиком(client, db, seeded, орг):
    """Админ выписал → ссылка вернулась → по ней завёлся человек → он вошёл."""
    # ① ВЫПИСАЛИ. Роль не по умолчанию — иначе проверка «роль приезжает
    #    из приглашения» пройдёт на совпадении, а не на переносе.
    создано = await client.post(
        "/api/invite/create",
        json={
            "role": "accountant",
            "email": "novichok@example.com",
            "first_name": "Новичок",
            "last_name": "Приглашённый",
            "expires_hours": 72,
            "max_uses": 1,
        },
    )
    assert создано.status_code == 200, создано.text
    тело = создано.json()
    токен = тело["token"]
    assert тело["invite_url"].endswith(токен), "ссылка обязана вести на этот токен"
    assert тело["role"] == "accountant"

    # ② ПЕРЕШЛИ ПО ССЫЛКЕ. Это делает неавторизованный человек — ручка
    #    обязана отвечать без токена доступа.
    проверка = await client.get(f"/api/invite/validate/{токен}")
    assert проверка.status_code == 200, проверка.text

    # ③ ЗАРЕГИСТРИРОВАЛИСЬ. Роль и организация приходят ИЗ ПРИГЛАШЕНИЯ,
    #    а не из тела запроса — навязать своё нельзя.
    рег = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": токен,
            "email": "novichok@example.com",
            "password": "парольдлинный",
            "first_name": "Новичок",
            "role": "admin",  # попытка навязать — обязана быть проигнорирована
        },
    )
    assert рег.status_code == 200, рег.text

    новый = [u for u in db.users if u.get("email") == "novichok@example.com"]
    assert len(новый) == 1, "человек должен был завестись ровно один"
    новый = новый[0]
    assert новый["role"] == "accountant", "роль обязана прийти из приглашения"
    assert новый["org_id"] == 1, "организация обязана прийти из приглашения"
    # ⚠️ ГЛАВНОЕ ОТЛИЧИЕ ОТ СНЕСЁННОГО ПУТИ: у человека ЕСТЬ ПАРОЛЬ.
    # `POST /api/users/` заводила строку без него, и войти по ней было нельзя.
    assert новый["password_hash"], "человек без пароля — это снесённый путь"

    # ④ ВОШЛИ. Тот же пароль, что задан при регистрации.
    # ⚠️ ПОЛЕ НАЗЫВАЕТСЯ ИНАЧЕ, ЧЕМ ПРИ РЕГИСТРАЦИИ: там `email`, здесь
    # `phone_or_email`. Это и поймал сквозной проход с первого запуска —
    # по отдельности оба звена зелёные, а стык между ними никто не проверял.
    вход = await client.post(
        "/api/auth/login",
        json={"phone_or_email": "novichok@example.com", "password": "парольдлинный"},
    )
    assert вход.status_code == 200, вход.text
    assert вход.json().get("access_token"), "вход обязан выдать токен"


@pytest.mark.asyncio
async def test_ссылка_сгорела_после_прохода(client, db, seeded, орг):
    """⚠️ ОБРАТНАЯ СТОРОНА: пройдя путь, ссылка не должна пускать второго.

    Без этой проверки предыдущий тест был бы зелёным и при ссылке, которой
    можно пользоваться бесконечно, — а это раздача доступа в организацию.
    """
    создано = await client.post(
        "/api/invite/create",
        json={
            "role": "employee",
            "email": "first@example.com",
            "max_uses": 1,
            "expires_hours": 24,
        },
    )
    токен = создано.json()["token"]
    первый = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": токен,
            "email": "first@example.com",
            "password": "парольдлинный",
        },
    )
    assert первый.status_code == 200, первый.text

    второй = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": токен,
            "email": "second@example.com",
            "password": "парольдлинный",
        },
    )
    assert второй.status_code == 400, второй.text
    assert not [u for u in db.users if u.get("email") == "second@example.com"]
