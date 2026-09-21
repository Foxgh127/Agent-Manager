from agent_manager.core.i18n import I18n, set_locale, t


def test_translation_lookup_and_formatting() -> None:
    service = I18n("zh-CN")
    assert service.t("errors.account_not_found") == "账号不存在"
    assert "1" in service.t("errors.batch_request_range", min=1, max=200)


def test_translation_falls_back_to_key_and_switches_locale() -> None:
    service = I18n("zh-CN")
    assert service.t("missing.path") == "missing.path"
    service.set_locale("en-US")
    assert service.t("errors.account_not_found") == "Account not found"


def test_global_translation_locale_can_be_restored() -> None:
    set_locale("en-US")
    assert t("errors.account_not_found") == "Account not found"
    set_locale("zh-CN")
    assert "账号" in t("errors.account_not_found")
