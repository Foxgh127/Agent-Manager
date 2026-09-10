#!/usr/bin/env python3
"""Desktop and local web application for Agent Manager."""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
from urllib.parse import unquote, urlparse
import urllib.request
import webbrowser

from agent_manager import paths as app_paths
from agent_manager._version import VERSION, RELEASE_EPOCH
from agent_manager.platform.version_info import executable_release_epoch
from agent_manager.platform.webview import available as native_webview_available
import agent_manager.core as core
import agent_manager.updates.service as app_updates
import agent_manager.integrations.claude as claude
import agent_manager.sessions.live_selection as live_selection
import agent_manager.codex.maintenance as maintenance
import agent_manager.sessions.history as history_sync
import agent_manager.integrations.radar as radar
import agent_manager.storage.recovery as recovery
import agent_manager.storage.coordinator
import agent_manager.accounts.relay as relay_portal
import agent_manager.integrations.toolbox as toolbox
from agent_manager.gateway.service import Web2APIManager


RESOURCE_ROOT = app_paths.resource_root()
STATIC_DIR = app_paths.gui_directory()
RUNTIME_FILE = core.STATE_DIR / "app-runtime.json"
SHUTDOWN_STATUS_FILE = core.STATE_DIR / "last-app-shutdown.json"
RESTART_HANDOFF_FILE = core.STATE_DIR / "app-restart-handoff.json"
# Keep a larger ceiling for the bounded account-batch import format, while
# using a much smaller default for ordinary control-plane JSON.  A local UI
# is still an HTTP server: an authenticated but compromised browser should
# not be able to reserve tens of megabytes across every worker thread.
MAX_BODY_BYTES = 32_000_000
DEFAULT_JSON_BODY_BYTES = 8_000_000
MAX_IMPORT_BODY_BYTES = 24_000_000
MAX_CONFIG_BODY_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 32_000_000
MAX_EXISTING_INSTANCE_RESPONSE_BYTES = 65_536
MAX_RUNTIME_FILE_BYTES = 65_536
MAX_MANAGEMENT_THREADS = 32
MANAGEMENT_CLIENT_TIMEOUT_SECONDS = 15.0
MAX_OAUTH_CALLBACK_THREADS = 8
OAUTH_CALLBACK_CLIENT_TIMEOUT_SECONDS = 10.0
FORCED_EXIT_TIMEOUT_SECONDS = 45.0
MUTATION_DRAIN_TIMEOUT_SECONDS = 15.0
RESTART_HANDOFF_TIMEOUT_SECONDS = 120.0
RESTART_STAGE_TIMEOUT_SECONDS = 20.0
RESTART_REPLACEMENT_READY_SECONDS = 20.0
RESTART_REPLACEMENT_EXIT_GRACE_SECONDS = 4.0
RESTART_MUTEX_WAIT_SECONDS = 30.0
RESTART_POLL_INTERVAL_SECONDS = 0.05
NATIVE_WINDOW_RECOVERY_WAIT_SECONDS = 35.0
OAUTH_LOGIN_TIMEOUT_SECONDS = 10 * 60
UI_BOOTSTRAP_TOKEN_TTL_SECONDS = 60.0
UI_BOOTSTRAP_MAX_EXCHANGES = 3
EXISTING_INSTANCE_WAKE_ATTEMPTS = 40
EXISTING_INSTANCE_WAKE_INTERVAL_SECONDS = 0.2
ERROR_ALREADY_EXISTS = 183
RUNTIME_APP_ID = "openai-agent-manager"
CONTROL_HEADER_NAME = "X-Agent-Manager-Control"
CONTROL_QUICK_RESTART_PATH = "/api/control/quick-restart"
JOB_BREAKAWAY_ENV = "AGENT_MANAGER_JOB_BREAKAWAY_ATTEMPT"
JOB_SHELL_HANDOFF_ARG = "--agent-manager-shell-handoff"
TRUSTED_RESTART_ENV = "AGENT_MANAGER_TRUSTED_RESTART"
NATIVE_WINDOW_RECOVERY_ENV = "AGENT_MANAGER_NATIVE_WINDOW_RECOVERY"
_WINDOWS_JOB_ISOLATION_STATUS = "unknown"
_INSTANCE_MUTEX_HANDLE: int | None = None
_INSTANCE_MUTEX_KERNEL32: object | None = None



# Explicit composition of the package API and shared runtime state.
from .processes import (
    _quick_restart_token_from_argv,
    _bind_exclusive_loopback,
    _current_process_is_in_windows_job,
    _relaunch_frozen_manager_outside_parent_job,
    _consume_shell_job_handoff,
    _manager_lifecycle_is_independent,
    _close_codex_processes_safely,
    OAuthCallbackServer,
    _release_executable_name,
    _promote_and_cleanup_release_executable,
    _schedule_release_maintenance,
    _instance_mutex_name,
    _acquire_instance_mutex,
    _release_instance_mutex,
)
from .oauth import (
    OAuthDeviceLogin,
)
from .presentation import (
    _totp_api_item,
    _mail_account_api_item,
    _mail_message_api_item,
    _radar_api_section,
)
from .toolbox_runtime import (
    ToolboxRuntime,
)
from .runtime import (
    ManagerRuntime,
)
from .switching import (
    activate_web2api_for_codex,
    _activate_web2api_for_codex,
    deactivate_web2api_for_codex,
    _deactivate_web2api_for_codex,
    rotate_web2api_key_for_runtime,
    _ensure_runtime_gateway,
    save_orchestration_for_runtime,
)
from .server import (
    ManagerServer,
)
from .http import (
    RequestHandler,
)
from .discovery import (
    find_edge,
    open_browser_window,
    open_external_browser,
    _report_startup_error,
    _activation_token_hash,
    _pid_is_running,
    _runtime_identity_is_valid,
    _probe_runtime_identity,
    focus_process_window,
    request_existing_window,
    request_existing_quick_restart,
    _read_runtime_file,
)
from .location import (
    application_location_status,
    assert_application_location_idle,
    select_application_directory,
    prepare_application_relocation,
    create_application_shortcut,
    finish_application_location_handoff,
)
from .lifecycle import (
    _read_shutdown_status,
    open_existing_runtime,
    idle_watchdog,
    _cleanup_runtime,
    _write_runtime_discovery,
    _mark_ui_ready,
    _write_restart_handoff,
    _restart_handoff_payload,
    _restart_handoff_is_valid,
    _consume_restart_handoff,
    _mark_restart_child_staged,
    _arm_restart_readiness_deadline,
    _take_restart_handoff,
    _executable_version,
    _manager_build_fingerprint,
    _current_manager_build_fingerprint,
    _runtime_overlay_matches_last_apply,
    _restart_configuration_fingerprint,
    _quick_restart_can_adopt_without_apply,
    _manager_restart_command,
    _start_restarted_manager,
    _wait_for_restart_child_staged,
    _wait_for_restarted_manager,
    _launch_restarted_manager,
    _write_shutdown_status,
    _forced_exit_watchdog,
    _restart_readiness_watchdog,
    _schedule_restart_readiness_watchdog,
    _stop_server_mutations,
    _reopen_server_mutations,
    _wait_for_server_mutations,
    prepare_application_update,
    request_application_shutdown,
    request_application_restart,
    _recover_unexpected_native_window_exit,
    request_application_exit_only,
    _handle_startup_activation_failure,
    handle_native_window_closing,
    shutdown_application,
    _finalize_application_server,
    _create_startup_runtime,
    run_server,
    main,
)
