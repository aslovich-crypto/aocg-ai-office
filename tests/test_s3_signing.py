"""Подпись SigV4 — проверка ОФИЦИАЛЬНЫМИ контрольными примерами AWS.

ПОЧЕМУ ИМЕННО ТАК, А НЕ «у меня заработало» (требование владельца, №3):
ошибка в порядке полей канонического запроса ломается МОЛЧА — хранилище
отвечает общей ошибкой подписи, по которой не видно, что именно не так.
Значит проверять надо не «получилась ли какая-нибудь подпись», а совпадение
с ответом, известным заранее.

ОТКУДА ПРИМЕРЫ. `tests/fixtures/sigv4/` — файлы из aws4_testsuite (набор
самой AWS, лицензия и NOTICE лежат рядом). На каждый случай четыре файла:
  .req   — исходный HTTP-запрос
  .creq  — каким обязан получиться канонический запрос
  .sts   — какой обязана получиться строка для подписи
  .authz — заголовок Authorization с ЭТАЛОННОЙ подписью
Фикстуры лежат В РЕПОЗИТОРИИ, а не внутри теста: инструмент переписывают,
ответ должен пережить переписывание (правило T30).

ЧЕТЫРЕ ВЫБРАННЫХ СЛУЧАЯ бьют по разным местам разбора:
  get-vanilla                      — базовый;
  get-vanilla-query-order-key-case — сортировка параметров запроса;
  get-header-value-trim            — обрезка и схлопывание пробелов в значениях;
  get-unreserved                   — процентное кодирование пути.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.storage import s3

FIXTURES = Path(__file__).parent / "fixtures" / "sigv4"

# Реквизиты набора AWS — они же напечатаны в самих файлах примеров.
SUITE_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
SUITE_REGION = "us-east-1"
SUITE_SERVICE = "service"

CASES = [
    "get-vanilla",
    "get-vanilla-query-order-key-case",
    "get-header-value-trim",
    "get-unreserved",
]


def _read(case: str, ext: str) -> str:
    return (FIXTURES / f"{case}.{ext}").read_text(encoding="utf-8")


def _parse_request(raw: str):
    """Разбор .req: строка запроса, заголовки, тело.

    Разбор намеренно строгий: дубль заголовка роняет тест, а не молча
    берёт последний. Случая с дублем среди выбранных нет — если появится,
    тест обязан об этом сказать, а не подстроиться.
    """
    lines = raw.split("\n")
    method, target, _ = lines[0].split(" ", 2)
    path, _, query_string = target.partition("?")

    headers: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    for line in lines[1:]:
        if in_body:
            body_lines.append(line)
            continue
        if line == "":
            in_body = True
            continue
        name, _, value = line.partition(":")
        key = name.lower().strip()
        assert key not in headers, (
            f"дубль заголовка {key} — разбор фикстуры надо расширять осознанно"
        )
        headers[key] = value
    query = []
    if query_string:
        for pair in query_string.split("&"):
            k, _, v = pair.partition("=")
            query.append((k, v))
    return method, path, query, headers, "\n".join(body_lines)


@pytest.mark.parametrize("case", CASES)
def test_canonical_request_matches_aws_fixture(case):
    method, path, query, headers, body = _parse_request(_read(case, "req"))
    payload_hash = s3.sha256_hex(body.encode("utf-8"))
    creq, _ = s3.build_canonical_request(method, path, query, headers, payload_hash)
    assert creq == _read(case, "creq")


@pytest.mark.parametrize("case", CASES)
def test_string_to_sign_matches_aws_fixture(case):
    method, path, query, headers, body = _parse_request(_read(case, "req"))
    creq, _ = s3.build_canonical_request(
        method, path, query, headers, s3.sha256_hex(body.encode("utf-8"))
    )
    expected = _read(case, "sts")
    amz_date = expected.split("\n")[1]
    scope = expected.split("\n")[2]
    assert s3.build_string_to_sign(amz_date, scope, creq) == expected


@pytest.mark.parametrize("case", CASES)
def test_signature_matches_aws_fixture(case):
    sts = _read(case, "sts")
    date_stamp = sts.split("\n")[2].split("/")[0]
    signature = s3.calculate_signature(
        SUITE_SECRET, date_stamp, SUITE_REGION, sts, service=SUITE_SERVICE
    )
    expected = re.search(r"Signature=([0-9a-f]{64})", _read(case, "authz")).group(1)
    assert signature == expected


def test_empty_payload_constant_is_really_sha256_of_nothing():
    # Константа встречается и в подписи GET, и в контрольных примерах —
    # если она разъедется, подпись сломается везде сразу.
    assert s3.sha256_hex(b"") == s3.EMPTY_PAYLOAD_SHA256


# ─────────────────────── подписанная ссылка на чтение ───────────────────────

CFG = s3.S3Config(
    endpoint="https://s3.example.ru",
    bucket="aocg-receipts",
    region="ru-1",
    access_key="AKIDEXAMPLE",
    secret_key=SUITE_SECRET,
)


def _fixed_now():
    from datetime import datetime, timezone

    return datetime(2026, 8, 12, 7, 30, 0, tzinfo=timezone.utc)


def test_presigned_url_has_required_parts_and_no_content_type():
    url = s3.presign_get_url(
        CFG, "receipts/1/abc.jpg", expires_in=300, now=_fixed_now()
    )
    assert url.startswith("https://s3.example.ru/aocg-receipts/receipts/1/abc.jpg?")
    for part in (
        "X-Amz-Algorithm=AWS4-HMAC-SHA256",
        "X-Amz-Credential=AKIDEXAMPLE%2F20260812%2Fru-1%2Fs3%2Faws4_request",
        "X-Amz-Date=20260812T073000Z",
        "X-Amz-Expires=300",
        "X-Amz-SignedHeaders=host",
    ):
        assert part in url
    assert re.search(r"X-Amz-Signature=[0-9a-f]{64}$", url)
    # Требование документации Timeweb: Content-Type в подписанной ссылке
    # не передаётся — «может привести к ошибке».
    assert "Content-Type" not in url and "content-type" not in url


def test_presigned_signature_depends_on_secret():
    other = s3.S3Config(**{**CFG.__dict__, "secret_key": SUITE_SECRET + "x"})
    a = s3.presign_get_url(CFG, "k.jpg", now=_fixed_now())
    b = s3.presign_get_url(other, "k.jpg", now=_fixed_now())
    assert a != b, "подпись обязана зависеть от секрета"


def test_presigned_expiry_is_rejected_when_not_positive():
    with pytest.raises(ValueError):
        s3.presign_get_url(CFG, "k.jpg", expires_in=0, now=_fixed_now())


def test_header_order_does_not_matter_but_values_do():
    # Заголовки подписываются отсортированными: порядок вставки не влияет,
    # а значение — влияет. Без этого можно «починить» сортировку так,
    # что подпись перестанет зависеть от данных.
    h1 = {"host": "s3.example.ru", "x-amz-date": "20260812T073000Z"}
    h2 = {"x-amz-date": "20260812T073000Z", "host": "s3.example.ru"}
    a, _ = s3.build_canonical_request("GET", "/b/k", [], h1, "UNSIGNED-PAYLOAD")
    b, _ = s3.build_canonical_request("GET", "/b/k", [], h2, "UNSIGNED-PAYLOAD")
    assert a == b
    h3 = {"host": "other.example.ru", "x-amz-date": "20260812T073000Z"}
    c, _ = s3.build_canonical_request("GET", "/b/k", [], h3, "UNSIGNED-PAYLOAD")
    assert a != c


def test_config_from_env_returns_none_until_storage_configured(monkeypatch):
    # Не настроенное хранилище — рабочее состояние, а не ошибка: до заведения
    # бакета приложение обязано работать по-старому.
    for name in (
        "S3_ENDPOINT",
        "S3_BUCKET",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_REGION",
    ):
        monkeypatch.delenv(name, raising=False)
    assert s3.S3Config.from_env() is None

    monkeypatch.setenv("S3_ENDPOINT", "https://s3.example.ru/")
    monkeypatch.setenv("S3_BUCKET", "aocg-receipts")
    monkeypatch.setenv("S3_ACCESS_KEY", "id")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    cfg = s3.S3Config.from_env()
    assert cfg is not None
    assert cfg.endpoint == "https://s3.example.ru"  # хвостовой слэш срезан
    assert cfg.host == "s3.example.ru"
    assert cfg.region == "ru-1"  # умолчание
