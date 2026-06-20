"""Тесты маскирования ПД (aocg_security.masking).

Маскирование — security-критичная функция (152-ФЗ): фискальные/платёжные
реквизиты и секреты не должны утекать в логи. Эти тесты фиксируют контракт
вывода, чтобы регрессия в маскировании ловилась автоматически.
"""

from aocg_security.masking import mask_card, mask_fn, mask_inn, mask_log_dict


# --- mask_inn ----------------------------------------------------------------


def test_mask_inn_shows_only_last4():
    # Новый формат (v0.1.2): префикс (код региона + ИФНС) скрыт.
    assert mask_inn("7707083893") == "****3893"


def test_mask_inn_strips_whitespace():
    assert mask_inn("770 708 3893") == "****3893"


def test_mask_inn_empty_is_blank():
    assert mask_inn("") == ""
    assert mask_inn(None) == ""


def test_mask_inn_too_short_is_stars():
    assert mask_inn("123") == "***"
    assert mask_inn("12345") == "***"


def test_mask_inn_never_leaks_prefix():
    # Прямая защита от регрессии к старому формату '770****93'.
    masked = mask_inn("7707083893")
    assert "770" not in masked
    assert masked.startswith("****")


# --- mask_card ---------------------------------------------------------------


def test_mask_card_shows_last4():
    assert mask_card("1234567812345678") == "****5678"


def test_mask_card_from_last4_field():
    assert mask_card("5678") == "****5678"


def test_mask_card_too_short():
    assert mask_card("12") == "****"
    assert mask_card("") == "****"


# --- mask_fn -----------------------------------------------------------------


def test_mask_fn_hidden_when_present():
    assert mask_fn("9999078902004554") == "[fn:скрыт]"


def test_mask_fn_blank_when_empty():
    assert mask_fn("") == ""
    assert mask_fn(None) == ""


# --- mask_log_dict: скаляры --------------------------------------------------


def test_mask_log_dict_masks_inn_aliases():
    src = {"inn": "7707083893", "org_inn": "7707083893", "userInn": "7707083893"}
    out = mask_log_dict(src)
    assert out == {"inn": "****3893", "org_inn": "****3893", "userInn": "****3893"}


def test_mask_log_dict_masks_secrets_fully():
    src = {
        "password": "hunter2",
        "access_token": "abc.def.ghi",
        "refresh_token": "r-123",
        "api_key": "sk-xxx",
        "authorization": "Bearer xyz",
        "passport": "4509 123456",
        "snils": "112-233-445 95",
    }
    out = mask_log_dict(src)
    assert all(v == "***" for v in out.values())


def test_mask_log_dict_masks_fiscal_keys():
    src = {
        "fiscalDriveNumber": "9999078902004554",
        "fiscalDocumentNumber": "12345",
        "fiscalSign": "1234567890",
        "kkt_fn": "9999078902004554",
    }
    out = mask_log_dict(src)
    assert all(v == "[fn:скрыт]" for v in out.values())


def test_mask_log_dict_masks_operator_and_cashier():
    src = {"operator": "Иванов И.И.", "cashier": "Петров П.П."}
    out = mask_log_dict(src)
    assert out == {"operator": "[скрыт]", "cashier": "[скрыт]"}


def test_mask_log_dict_passes_through_non_sensitive():
    src = {"sum": 1500, "name": "Кофе", "qty": 2}
    assert mask_log_dict(src) == src


# --- mask_log_dict: списки и вложенность -------------------------------------


def test_mask_log_dict_masks_scalar_lists_elementwise():
    # v0.1.2: элементы списка под чувствительным ключом маскируются поэлементно.
    src = {"inns": ["7707083893", "7708123456"], "tokens": ["t1", "t2"]}
    out = mask_log_dict(src)
    assert out == {"inns": ["****3893", "****3456"], "tokens": ["***", "***"]}


def test_mask_log_dict_masks_list_of_dicts():
    src = {"items": [{"org_inn": "7707083893", "sum": 100}]}
    out = mask_log_dict(src)
    assert out == {"items": [{"org_inn": "****3893", "sum": 100}]}


def test_mask_log_dict_recurses_nested_dict():
    src = {"raw_data": {"userInn": "7707083893", "fiscalSign": "123"}}
    out = mask_log_dict(src)
    assert out == {"raw_data": {"userInn": "****3893", "fiscalSign": "[fn:скрыт]"}}


def test_mask_log_dict_does_not_mutate_source():
    src = {"inn": "7707083893", "nested": {"password": "x"}}
    mask_log_dict(src)
    assert src == {"inn": "7707083893", "nested": {"password": "x"}}


def test_mask_log_dict_non_dict_returned_as_is():
    assert mask_log_dict("plain") == "plain"
    assert mask_log_dict(None) is None
