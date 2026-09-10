"""Native Windows notifications without requiring the manager tray UI."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess


def radar_notification_text(alert: dict) -> tuple[str, str] | None:
    if not isinstance(alert, dict) or alert.get("level") not in {"A", "B", "C", "P"}:
        return None
    label = str(alert.get("severityLabel") or {"A": "预警", "B": "关注", "C": "确认", "P": "关注"}[alert["level"]])
    qualifier = "第三方预测，非官方确认" if alert["level"] == "P" else "公开来源确认" if alert.get("officialConfirmed") else "请核对原消息"
    title = f"Agent Manager · Codex 重置{label}"
    lines = [f"【{label} · {qualifier}】", str(alert.get("evidence") or "发现新的公开额度信号")[:200],
             str(alert.get("window") or "执行时间尚未确认")[:100]]
    urls = alert.get("sourceUrls")
    if isinstance(urls, list) and urls:
        lines.append(str(urls[0])[:180])
    return title, "\n".join(lines)[:600]


def notify_windows(title: str, message: str, *, runner=None) -> bool:
    """Ask the Windows shell for a native balloon/toast using a temporary icon.

    The hidden helper owns the icon for eight seconds, allowing Windows to
    deliver it even when Agent Manager has no permanent tray. OS notification
    settings may suppress banners despite a successful shell call.
    """
    if os.name != "nt":
        return False
    payload = base64.b64encode(json.dumps({"title": title[:100], "message": message[:600]}, ensure_ascii=False).encode("utf-8")).decode("ascii")
    # Only base64 data is interpolated. Neither source prose nor URLs become
    # PowerShell code or command-line switches.
    script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$payload = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json
$icon = New-Object System.Windows.Forms.NotifyIcon
try {
    $icon.Icon = [System.Drawing.SystemIcons]::Information
    $icon.Text = 'Agent Manager'
    $icon.Visible = $true
    $icon.ShowBalloonTip(8000, [string]$payload.title, [string]$payload.message, [System.Windows.Forms.ToolTipIcon]::Info)
    $until = [DateTime]::UtcNow.AddSeconds(8)
    while ([DateTime]::UtcNow -lt $until) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 100
    }
    [Console]::WriteLine('notification-submitted')
} finally {
    $icon.Visible = $false
    $icon.Dispose()
}
""".replace("__PAYLOAD__", payload)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = (runner or subprocess.run)(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0 and "notification-submitted" in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def notify_radar_alert(alert: dict) -> bool:
    content = radar_notification_text(alert)
    return notify_windows(*content) if content else False
