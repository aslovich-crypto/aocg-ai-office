# -*- coding: utf-8 -*-
"""Инварианты, которые держит САМА БАЗА, — впервые под тестом (T36, сессия 2).

До живого контура эти правила не проверялись НИЧЕМ: FakePool индексов
не имеет, а sql_check в CI только поднимал схему, не проверяя, что
ограничения действительно отвергают плохие данные. Правило могло молча
исчезнуть из init_db — прогон остался бы зелёным.

Тесты бьют НАПРЯМУЮ в базу, минуя API, и это осознанно: инвариант —
последний рубеж, он обязан держать даже тот код, который ещё не написан.
Путь через API проверяет роутер; здесь проверяется сам рубеж.
"""

import asyncpg
import pytest
from datetime import date


@pytest.mark.asyncio
async def test_один_чек_не_живёт_в_двух_отчётах(db, seeded):
    """uq_report_items_receipt_id — правило ПРО ДЕНЬГИ (см. init_db):
    один чек в двух авансовых отчётах = двойное возмещение сотруднику
    и задвоение расхода в налоговом учёте (ст. 252 НК РФ). До индекса
    правило держалось ТОЛЬКО на фронте."""
    await db.добавить_отчёт(id=2, title="Второй отчёт", user_id=2)
    await db.pool.execute(
        "INSERT INTO report_items (report_id, receipt_id) VALUES (1, 1)"
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.pool.execute(
            "INSERT INTO report_items (report_id, receipt_id) VALUES (2, 1)"
        )


@pytest.mark.asyncio
async def test_одна_почта_один_человек_без_оглядки_на_регистр(db, seeded):
    """uq_users_email_lower (T105/T118): до 31.08.2026 в базе жили две строки
    с одним адресом, и вход брал ПЕРВУЮ ПОПАВШУЮСЯ — верный пароль мог
    не подойти. Индекс по lower(email): Ivan@ и ivan@ — один человек."""
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.добавить_пользователя(
            id=77, first_name="Двойник", email="IVAN@example.com"
        )


@pytest.mark.asyncio
async def test_пустая_почта_не_блокирует_вторую_пустую(db, seeded):
    """Обратная сторона частичности индекса: безпочтовых людей в базе
    может быть много — обычный UNIQUE разрешил бы РОВНО ОДНОГО на всю
    базу и отверг бы второго при заведении."""
    await db.добавить_пользователя(id=10, first_name="Первый", email="")
    await db.добавить_пользователя(id=11, first_name="Второй", email="")
    assert await db.число_пользователей() == 3  # Иван из seeded + двое


@pytest.mark.asyncio
async def test_фискальный_документ_уникален_ПАРОЙ_фн_и_фд(db, seeded):
    """receipts_kkt_fn_fd_unique: ФН один на кассу и ОБЩИЙ для всех её
    чеков — уникален документ только парой (ФН, ФД). Одиночный UNIQUE
    по kkt_fn отвергал бы второй чек с той же кассы."""
    await db.добавить_чек(
        id=201,
        org="Касса",
        amount=10,
        date=date(2026, 6, 1),
        kkt_fn="FN-1",
        fd_num="100",
    )
    # Второй чек с ТОЙ ЖЕ кассы, другой документ — обязан пройти.
    await db.добавить_чек(
        id=202,
        org="Касса",
        amount=20,
        date=date(2026, 6, 2),
        kkt_fn="FN-1",
        fd_num="101",
    )
    # Тот же документ повторно — обязан быть отвергнут.
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.добавить_чек(
            id=203,
            org="Касса",
            amount=10,
            date=date(2026, 6, 1),
            kkt_fn="FN-1",
            fd_num="100",
        )


@pytest.mark.asyncio
async def test_отчёт_без_автора_не_живёт(db, seeded):
    """reports.user_id NOT NULL (REP-AUTHOR): авансовый отчёт — документ
    КОНКРЕТНОГО сотрудника, «отчёта без автора» не бывает. У двойника
    отчёт без автора жил (и в seeded таким и лежал)."""
    with pytest.raises(asyncpg.NotNullViolationError):
        await db.pool.execute(
            "INSERT INTO reports (title, status, org_id) "
            "VALUES ('Безымянный', 'Черновик', 1)"
        )
