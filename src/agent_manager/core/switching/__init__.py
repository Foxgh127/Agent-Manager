"""
Switching services - Account switching and configuration management

This module provides functionality for switching between Codex accounts,
managing configurations, and handling session synchronization.
"""
from __future__ import annotations

# Progress reporting
from .progress import _SwitchProgressReporter

# Core switching functions
from .core import (
    _prepare_account_switch_target,
    _account_model_source,
    switch_codex_account,
)

# Validation, waiting, and session repair
from .validation import (
    wait_for_codex_runtime_ready,
    _probe_configuration_matches,
    _provider_auth_overrides,
    _clear_provider_auth_overrides,
    _switch_runtime_model_matches,
    _official_route_has_overrides,
    _ensure_switch_gateway,
    _repair_switch_session_visibility,
    _restore_switch_session_visibility,
    _official_account_target_is_active,
    _apply_official_account_configuration,
)

# Official account integration
from .official import (
    _rollback_failed_switch,
    switch_codex_account_and_launch,
)

# Provider utilities
from .providers import (
    provider_by_id,
    _provider_portal_preset,
    _url_hostname,
    detect_provider_portal_preset,
    _validated_provider_url,
    _validated_provider_portal_url,
    provider_portal_url,
    _provider_url_origin,
    _SameOriginRedirectHandler,
    _open_same_origin_request,
    _effective_url_proxies,
    _validated_provider_related_url,
)

__all__ = [
    "_SwitchProgressReporter",
    "_prepare_account_switch_target",
    "_account_model_source",
    "switch_codex_account",
    "wait_for_codex_runtime_ready",
    "_probe_configuration_matches",
    "_provider_auth_overrides",
    "_clear_provider_auth_overrides",
    "_switch_runtime_model_matches",
    "_official_route_has_overrides",
    "_ensure_switch_gateway",
    "_repair_switch_session_visibility",
    "_restore_switch_session_visibility",
    "_official_account_target_is_active",
    "_apply_official_account_configuration",
    "_rollback_failed_switch",
    "switch_codex_account_and_launch",
    "provider_by_id",
    "_provider_portal_preset",
    "_url_hostname",
    "detect_provider_portal_preset",
    "_validated_provider_url",
    "_validated_provider_portal_url",
    "provider_portal_url",
    "_provider_url_origin",
    "_SameOriginRedirectHandler",
    "_open_same_origin_request",
    "_effective_url_proxies",
    "_validated_provider_related_url",
]
