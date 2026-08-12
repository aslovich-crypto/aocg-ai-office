"""Подпись запросов к S3-совместимому хранилищу (AWS Signature Version 4).

ЗАЧЕМ СВОЯ ПОДПИСЬ, А НЕ boto3 (решение владельца 12.08.2026, задача №3).
boto3 — библиотека Amazon. Переезд затеян ради ухода от зависимости от
американского поставщика; тянуть её ради трёх операций у российского
провайдера — противоречие. SigV4 при этом открытый стандарт: его нельзя
«отозвать», и он одинаков у всех S3-совместимых хранилищ.

ЧТО ЗДЕСЬ ЕСТЬ. Ровно три операции, которые нужны Приме:
  * put_object      — положить снимок чека в бакет;
  * presign_get_url — подписать ВРЕМЕННУЮ ссылку на чтение (бакет приватный,
    постоянного адреса у объекта нет и быть не должно);
  * delete_object   — убрать объект.

ЧЕМ ПРОВЕРЕНО. Разбор и подпись — не «у меня заработало», а официальными
контрольными примерами AWS (`tests/fixtures/sigv4/`, файлы .req/.creq/.sts/
.authz из aws4_testsuite). Причина требования записана владельцем: ошибка
в порядке полей канонического запроса ломается МОЛЧА — хранилище отвечает
общей ошибкой подписи, и по ней не видно, что именно не так.

ЧАСТНОСТЬ TIMEWEB, ИЗ ИХ ЖЕ ДОКУМЕНТАЦИИ: при генерации подписанной ссылки
Content-Type не передаётся — «может привести к ошибке». Поэтому presign_get_url
его не подписывает и не добавляет.

АДРЕСАЦИЯ — path-style (`endpoint/bucket/key`), а не `bucket.endpoint`.
Так проще и не зависит от того, покрывает ли сертификат поддомен с точкой
в имени бакета.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional, Sequence, Tuple

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"

# Пустое тело: sha256 от пустой строки. Константа вынесена, потому что
# встречается и в подписи GET, и в контрольных примерах AWS.
EMPTY_PAYLOAD_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)

_UNRESERVED = re.compile(r"[^A-Za-z0-9\-._~]")
_WS_RUN = re.compile(r"\s+")


# ─────────────────────────── примитивы подписи ───────────────────────────


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def derive_signing_key(
    secret_key: str, date_stamp: str, region: str, service: str = SERVICE
) -> bytes:
    """Ключ подписи: четыре последовательных HMAC по спецификации SigV4."""
    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def uri_encode(value: str, encode_slash: bool = True) -> str:
    """Процентное кодирование по правилам SigV4.

    Не-зарезервированными считаются A-Z a-z 0-9 - . _ ~ — всё остальное
    кодируется заглавными шестнадцатеричными. Слэш в ПУТИ не кодируется,
    в значениях параметров — кодируется.
    """

    def repl(m: "re.Match[str]") -> str:
        ch = m.group(0)
        if ch == "/" and not encode_slash:
            return ch
        return "".join("%%%02X" % b for b in ch.encode("utf-8"))

    return _UNRESERVED.sub(repl, value)


def canonical_uri(path: str) -> str:
    """Канонический путь. Каждый сегмент кодируется, слэши остаются."""
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    return "/".join(uri_encode(seg) for seg in path.split("/"))


def canonical_query(params: Sequence[Tuple[str, str]] | Mapping[str, str]) -> str:
    """Канонические параметры: закодировать, отсортировать по ключу и значению.

    Сортировка идёт по УЖЕ ЗАКОДИРОВАННЫМ парам — именно так требует
    спецификация, и именно на этом расходятся самодельные реализации
    (контрольный пример get-vanilla-query-order-key-case).
    """
    items: Iterable[Tuple[str, str]]
    items = params.items() if isinstance(params, Mapping) else params
    encoded = sorted((uri_encode(k), uri_encode(v)) for k, v in items)
    return "&".join("%s=%s" % kv for kv in encoded)


def canonical_headers(headers: Mapping[str, str]) -> Tuple[str, str]:
    """Канонические заголовки и список подписанных имён.

    Имена — в нижний регистр, значения — обрезать по краям и схлопнуть
    внутренние пробельные серии (контрольный пример get-header-value-trim).
    """
    normalized = {
        k.lower().strip(): _WS_RUN.sub(" ", v.strip()) for k, v in headers.items()
    }
    names = sorted(normalized)
    canonical = "".join("%s:%s\n" % (n, normalized[n]) for n in names)
    return canonical, ";".join(names)


def build_canonical_request(
    method: str,
    path: str,
    query: Sequence[Tuple[str, str]] | Mapping[str, str],
    headers: Mapping[str, str],
    payload_hash: str,
) -> Tuple[str, str]:
    """Канонический запрос и список подписанных заголовков.

    Порядок строк — часть подписи. Менять его нельзя: хранилище ответит
    общей ошибкой подписи, по которой причина не видна.
    """
    canon_headers, signed_headers = canonical_headers(headers)
    creq = "\n".join(
        [
            method.upper(),
            canonical_uri(path),
            canonical_query(query),
            canon_headers,
            signed_headers,
            payload_hash,
        ]
    )
    return creq, signed_headers


def credential_scope(date_stamp: str, region: str, service: str = SERVICE) -> str:
    return "/".join([date_stamp, region, service, "aws4_request"])


def build_string_to_sign(amz_date: str, scope: str, canonical_req: str) -> str:
    return "\n".join(
        [ALGORITHM, amz_date, scope, sha256_hex(canonical_req.encode("utf-8"))]
    )


def calculate_signature(
    secret_key: str,
    date_stamp: str,
    region: str,
    string_to_sign: str,
    service: str = SERVICE,
) -> str:
    key = derive_signing_key(secret_key, date_stamp, region, service)
    return hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


# ─────────────────────────── конфигурация ───────────────────────────


@dataclass(frozen=True)
class S3Config:
    endpoint: (
        str  # https://s3.timeweb.com  — берётся ИЗ ПАНЕЛИ, у Timeweb живы два адреса
    )
    bucket: str
    region: str
    access_key: str
    secret_key: str

    @property
    def host(self) -> str:
        return self.endpoint.split("://", 1)[-1].rstrip("/")

    @classmethod
    def from_env(cls) -> Optional["S3Config"]:
        """Конфигурация из окружения или None, если хранилище не настроено.

        None — рабочее состояние, а не ошибка: до заведения бакета приложение
        обязано работать по-старому (см. запасной путь в задаче №3).
        """
        endpoint = os.environ.get("S3_ENDPOINT", "").strip().rstrip("/")
        bucket = os.environ.get("S3_BUCKET", "").strip()
        access_key = os.environ.get("S3_ACCESS_KEY", "").strip()
        secret_key = os.environ.get("S3_SECRET_KEY", "").strip()
        region = os.environ.get("S3_REGION", "ru-1").strip() or "ru-1"
        if not (endpoint and bucket and access_key and secret_key):
            return None
        return cls(
            endpoint=endpoint,
            bucket=bucket,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
        )


def object_path(bucket: str, key: str) -> str:
    return "/%s/%s" % (bucket.strip("/"), key.lstrip("/"))


# ─────────────────────────── подписанная ссылка ───────────────────────────


def presign_get_url(
    cfg: S3Config, key: str, expires_in: int = 300, now: Optional[datetime] = None
) -> str:
    """Временная ссылка на ЧТЕНИЕ объекта.

    expires_in по умолчанию 5 минут: ссылка нужна ровно на время открытия
    картинки. Долгий срок жизни превращает приватный бакет в публичный
    для всякого, кому ссылка попалась на глаза.
    """
    if expires_in < 1:
        raise ValueError("expires_in должен быть положительным")
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    scope = credential_scope(date_stamp, cfg.region)

    # Content-Type здесь НЕ участвует — требование документации Timeweb.
    query = [
        ("X-Amz-Algorithm", ALGORITHM),
        ("X-Amz-Credential", "%s/%s" % (cfg.access_key, scope)),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", str(expires_in)),
        ("X-Amz-SignedHeaders", "host"),
    ]
    path = object_path(cfg.bucket, key)
    creq, _ = build_canonical_request(
        "GET", path, query, {"host": cfg.host}, "UNSIGNED-PAYLOAD"
    )
    signature = calculate_signature(
        cfg.secret_key,
        date_stamp,
        cfg.region,
        build_string_to_sign(amz_date, scope, creq),
    )
    return "%s%s?%s&X-Amz-Signature=%s" % (
        cfg.endpoint,
        canonical_uri(path),
        canonical_query(query),
        signature,
    )


def authorization_header(
    cfg: S3Config,
    method: str,
    key: str,
    headers: Mapping[str, str],
    payload_hash: str,
    date_stamp: str,
    amz_date: str,
) -> str:
    scope = credential_scope(date_stamp, cfg.region)
    creq, signed_headers = build_canonical_request(
        method, object_path(cfg.bucket, key), [], headers, payload_hash
    )
    signature = calculate_signature(
        cfg.secret_key,
        date_stamp,
        cfg.region,
        build_string_to_sign(amz_date, scope, creq),
    )
    return "%s Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (
        ALGORITHM,
        cfg.access_key,
        scope,
        signed_headers,
        signature,
    )
