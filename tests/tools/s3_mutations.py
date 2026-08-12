#!/usr/bin/env python3
"""МУТАЦИИ ПОДПИСИ SigV4 — проверка, что тесты действительно ловят (T11).

ЗАЧЕМ. Зелёный прогон сам по себе не отличает «всё верно» от «ничего не
проверено». Тесты подписи объявляют, что ловят четыре разных класса ошибок
(порядок полей, сортировку параметров, обрезку значений, кодирование пути)
плюс вывод ключа. Каждый заявленный класс ломается здесь ОТДЕЛЬНО: одна
успешная мутация подтверждает одну ветку, а не сторож целиком.

ДВА ТРЕБОВАНИЯ, КУПЛЕННЫЕ ДОРОГО И ЗАПИСАННЫЕ В CLAUDE.md:
  * мутация засчитывается, только если РЕАЛЬНО ПРИМЕНИЛАСЬ — цель обязана
    существовать ровно один раз, и текст файла обязан измениться;
  * мутация обязана требовать КОНКРЕТНЫЙ ТЕКСТ в выводе, а не просто
    «красный» — иначе тест краснеет по другой причине, и проверенной
    считается способность, которую никто не проверял.

ЗАПУСК:  ./venv/bin/python tests/tools/s3_mutations.py
Выход 0 — все мутации пойманы; 1 — какая-то прошла незамеченной.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
S3 = ROOT / "app" / "storage" / "s3.py"
RECEIPTS = ROOT / "app" / "routers" / "receipts.py"
OCR = ROOT / "app" / "routers" / "ocr.py"
CONFTEST = ROOT / "tests" / "conftest.py"

SIGN_TESTS = "tests/test_s3_signing.py"
PHOTO_TESTS = "tests/test_photo_storage.py"


def pytest_cmd(target: str):
    return [str(ROOT / "venv" / "bin" / "python"), "-m", "pytest", target, "-q"]


# (имя, файл, что заменить, на что, какой текст ОБЯЗАН появиться в выводе, какие тесты гонять)
MUTATIONS = [
    (
        "M1 порядок полей канонического запроса",
        S3,
        "            canonical_uri(path),\n            canonical_query(query),",
        "            canonical_query(query),\n            canonical_uri(path),",
        "test_canonical_request_matches_aws_fixture",
        SIGN_TESTS,
    ),
    (
        "M2 сортировка параметров запроса",
        S3,
        "    encoded = sorted((uri_encode(k), uri_encode(v)) for k, v in items)",
        "    encoded = [(uri_encode(k), uri_encode(v)) for k, v in items]",
        "get-vanilla-query-order-key-case",
        SIGN_TESTS,
    ),
    (
        "M3 схлопывание пробелов в значении заголовка",
        S3,
        '        k.lower().strip(): _WS_RUN.sub(" ", v.strip()) for k, v in headers.items()',
        "        k.lower().strip(): v.strip() for k, v in headers.items()",
        "get-header-value-trim",
        SIGN_TESTS,
    ),
    (
        "M4 набор незарезервированных символов при кодировании",
        S3,
        r'_UNRESERVED = re.compile(r"[^A-Za-z0-9\-._~]")',
        r'_UNRESERVED = re.compile(r"[^A-Za-z0-9\-._]")',
        "get-unreserved",
        SIGN_TESTS,
    ),
    (
        "M5 вывод ключа подписи (последний шаг)",
        S3,
        '    return _hmac_sha256(k_service, "aws4_request")',
        '    return _hmac_sha256(k_service, "aws4_Request")',
        "test_signature_matches_aws_fixture",
        SIGN_TESTS,
    ),
    (
        "M6 Content-Type попал в подписанную ссылку",
        S3,
        '        ("X-Amz-SignedHeaders", "host"),',
        '        ("X-Amz-SignedHeaders", "host"),\n        ("Content-Type", "image/jpeg"),',
        "test_presigned_url_has_required_parts_and_no_content_type",
        SIGN_TESTS,
    ),
    # ── стык трёх источников фото: то, что ломается ТИХО (шаг 3) ──
    (
        "M7 порядок источников перевёрнут: адрес побеждает ключ",
        RECEIPTS,
        '    if row["photo_key"]:\n        cfg = s3.S3Config.from_env()',
        '    if row["photo_url"]:\n        return RedirectResponse(url=row["photo_url"], status_code=302)\n'
        '    if row["photo_key"]:\n        cfg = s3.S3Config.from_env()',
        "test_photo_key_wins_over_url_and_base64",
        PHOTO_TESTS,
    ),
    (
        "M8 подписанная ссылка попадает в кэш (снят no-store)",
        RECEIPTS,
        '            url=url, status_code=302, headers={"Cache-Control": "no-store"}',
        "            url=url, status_code=302",
        "test_photo_key_alone_redirects_to_signed_url",
        PHOTO_TESTS,
    ),
    (
        "M9 при отказе хранилища втихую вернулись к base64",
        OCR,
        '        result["photo_saved"] = False\n        return result',
        '        result["photo_saved"] = False\n        result["photo_base64"] = image_b64\n        return result',
        "test_ocr_survives_storage_failure_and_says_so",
        PHOTO_TESTS,
    ),
    (
        "M10 ключ без настроенного хранилища отдаёт 404 вместо 503",
        RECEIPTS,
        'raise HTTPException(status_code=503, detail="Storage is not configured")',
        'raise HTTPException(status_code=404, detail="No photo for this receipt")',
        "test_photo_key_without_configured_storage_is_503_not_404",
        PHOTO_TESTS,
    ),
    (
        "M11 снимок продолжает ездить в браузер вместе с ключом",
        OCR,
        '    result["photo_key"] = key\n    result["photo_saved"] = True',
        '    result["photo_key"] = key\n    result["photo_base64"] = image_b64\n    result["photo_saved"] = True',
        "test_ocr_puts_photo_into_storage_and_stops_echoing_base64",
        PHOTO_TESTS,
    ),
    (
        "M12 зеркало FakePool не видит photo_key (ворота данных)",
        CONFTEST,
        '                    photo_key=r.get("photo_key"),',
        "                    photo_key=None,",
        "test_photo_key_alone_redirects_to_signed_url",
        PHOTO_TESTS,
    ),
]


def run_tests(target: str = SIGN_TESTS) -> tuple[int, str]:
    proc = subprocess.run(pytest_cmd(target), cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    code, out = run_tests()
    if code != 0:
        print(
            "ИСХОДНЫЙ ПРОГОН УЖЕ КРАСНЫЙ — мутации бессмысленны, сначала почини тесты"
        )
        print(out[-2000:])
        return 1
    print("исходный прогон зелёный, начинаем ломать\n")

    failures = []
    for name, path, old, new, expected_text, target in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        count = original.count(old)
        if count != 1:
            print(
                "✗ %s — ЦЕЛЬ НЕ НАЙДЕНА или неоднозначна (вхождений: %d). Мутация НЕ ВЫПОЛНЕНА."
                % (name, count)
            )
            failures.append(name)
            continue
        mutated = original.replace(old, new)
        if mutated == original:
            print("✗ %s — текст не изменился, мутация НЕ ВЫПОЛНЕНА" % name)
            failures.append(name)
            continue
        path.write_text(mutated, encoding="utf-8")
        try:
            code, out = run_tests(target)
        finally:
            path.write_text(original, encoding="utf-8")

        if code == 0:
            print("✗ %s — тесты ОСТАЛИСЬ ЗЕЛЁНЫМИ, поломка не поймана" % name)
            failures.append(name)
        elif expected_text not in out:
            print(
                "✗ %s — тесты покраснели, но НЕ ПО ТОЙ ПРИЧИНЕ: в выводе нет %r"
                % (name, expected_text)
            )
            failures.append(name)
        else:
            print("✓ %s — поймано, в выводе есть %r" % (name, expected_text))

    code, out = run_tests()
    print(
        "\nвозврат исходного состояния: %s"
        % ("прогон снова зелёный" if code == 0 else "ПРОГОН КРАСНЫЙ, разобраться")
    )
    if code != 0:
        return 1
    print(
        "мутаций всего: %d, поймано: %d"
        % (len(MUTATIONS), len(MUTATIONS) - len(failures))
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
