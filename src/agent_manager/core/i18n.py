"""Small, dependency-free translation service for backend messages."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


_DEFAULT_LOCALE = "zh-CN"
_FALLBACK_LOCALE = "en-US"


class _MissingValue(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class I18n:
    def __init__(self, locale: str = _DEFAULT_LOCALE, *, base_dir: str | Path | None = None):
        self._lock = threading.RLock()
        self.base_dir = Path(base_dir) if base_dir is not None else Path(__file__).with_name("translations")
        self.locale = self._normalize_locale(locale)
        self.translations: dict[str, Any] = {}
        self._fallback: dict[str, Any] = {}
        self.load_translations()

    @staticmethod
    def _normalize_locale(locale: object) -> str:
        raw = str(locale or _DEFAULT_LOCALE).strip().replace("_", "-")
        if not raw:
            return _DEFAULT_LOCALE
        language, _, region = raw.partition("-")
        return language.casefold() + (f"-{region.upper()}" if region else "")

    def _load_file(self, locale: str) -> dict[str, Any]:
        path = self.base_dir / f"{locale}.json"
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def load_translations(self) -> None:
        with self._lock:
            self.translations = self._load_file(self.locale)
            self._fallback = self._load_file(_FALLBACK_LOCALE) if self.locale != _FALLBACK_LOCALE else {}

    @staticmethod
    def _lookup(document: dict[str, Any], key: str) -> Any:
        current: Any = document
        for segment in str(key).split("."):
            if not isinstance(current, dict) or segment not in current:
                return None
            current = current[segment]
        return current

    def t(self, key: str, **kwargs: Any) -> str:
        with self._lock:
            value = self._lookup(self.translations, key)
            if value is None:
                value = self._lookup(self._fallback, key)
        if not isinstance(value, str):
            return str(key)
        try:
            return value.format_map(_MissingValue(kwargs))
        except (ValueError, KeyError):
            return value

    def set_locale(self, locale: str) -> None:
        with self._lock:
            self.locale = self._normalize_locale(locale)
            self.load_translations()


_i18n = I18n()


def t(key: str, **kwargs: Any) -> str:
    return _i18n.t(key, **kwargs)


def set_locale(locale: str) -> None:
    _i18n.set_locale(locale)
