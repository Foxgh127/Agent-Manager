"""
Runtime services - Codex CLI and Desktop runtime management

Handles Node.js discovery, CLI installation, desktop integration,
and Codex application launching.
"""
from __future__ import annotations

# Path utilities
from .path_utils import (
    _split_runtime_path,
    _dedupe_runtime_paths,
    _windows_registry_path_values,
    _refresh_windows_process_path,
)

# Node.js discovery
from .node_discovery import (
    _node_runtime_search_directories,
    _discover_node_npm_runtime,
    _npm_global_prefix,
    _locate_npm_codex_cli,
    _safe_codex_cli_command,
)

# CLI management
from .cli_management import (
    _verify_codex_cli,
    _registry_json,
    _windows_codex_platform,
    _manager_downloaded_codex_candidates,
    _safe_runtime_archive_destination,
    _download_official_codex_runtime,
    install_codex_cli_latest,
    _desktop_managed_codex_candidates,
    _is_desktop_managed_codex_path,
    _is_manager_downloaded_codex_path,
    _codex_cli_override_path,
    _codex_cli_override_diagnosis,
    _clear_unsafe_codex_cli_override,
)

# Runtime status and deployment
from .status import (
    codex_prefix,
    codex_runtime_status,
    _user_environment_value,
    _configure_windows_runtime_path,
    deploy_codex_runtime,
    _deploy_codex_runtime_locked,
)

# Launcher
from .launcher import (
    _detect_codex_windows_app,
    _detect_codex_windows_app_locked,
    _recent_codex_workspace,
    resolve_codex_launch_plan,
    _codex_runtime_environment,
    _codex_launch_probe_prefix,
    _is_windows_store_path,
    _safe_cli_workspace_prefix,
    _codex_source_environment,
    _powershell_single_quote,
    _powershell_encoded_command,
    _launch_codex_via_package_identity,
    launch_codex_app,
)

# Command execution
from .executor import run_codex_capture

__all__ = [
    # Path utilities
    "_split_runtime_path",
    "_dedupe_runtime_paths",
    "_windows_registry_path_values",
    "_refresh_windows_process_path",
    # Node discovery
    "_node_runtime_search_directories",
    "_discover_node_npm_runtime",
    "_npm_global_prefix",
    "_locate_npm_codex_cli",
    "_safe_codex_cli_command",
    # CLI management
    "_verify_codex_cli",
    "_registry_json",
    "_windows_codex_platform",
    "_manager_downloaded_codex_candidates",
    "_safe_runtime_archive_destination",
    "_download_official_codex_runtime",
    "install_codex_cli_latest",
    "_desktop_managed_codex_candidates",
    "_is_desktop_managed_codex_path",
    "_is_manager_downloaded_codex_path",
    "_codex_cli_override_path",
    "_codex_cli_override_diagnosis",
    "_clear_unsafe_codex_cli_override",
    # Status
    "codex_prefix",
    "codex_runtime_status",
    "_user_environment_value",
    "_configure_windows_runtime_path",
    "deploy_codex_runtime",
    "_deploy_codex_runtime_locked",
    # Launcher
    "_detect_codex_windows_app",
    "_detect_codex_windows_app_locked",
    "_recent_codex_workspace",
    "resolve_codex_launch_plan",
    "_codex_runtime_environment",
    "_codex_launch_probe_prefix",
    "_is_windows_store_path",
    "_safe_cli_workspace_prefix",
    "_codex_source_environment",
    "_powershell_single_quote",
    "_powershell_encoded_command",
    "_launch_codex_via_package_identity",
    "launch_codex_app",
    # Executor
    "run_codex_capture",
]
