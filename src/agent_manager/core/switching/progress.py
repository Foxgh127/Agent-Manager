"""Progress reporter for account switching"""
from __future__ import annotations
from agent_manager import core as _core


class _SwitchProgressReporter:
    """Emit bounded, user-facing switch stages and retain real timings."""

    def __init__(self, callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.callback = callback
        self.started_at = _core.time.perf_counter()
        self.phase_started_at = self.started_at
        self.phase: str | None = None
        self.timings: dict[str, int] = {}

    def emit(
        self,
        phase: str,
        progress: int,
        message: str,
        *,
        status: str = "running",
        **details: Any,
    ) -> None:
        now = _core.time.perf_counter()
        if self.phase and self.phase != phase and self.phase not in {"completed", "failed"}:
            self.timings[self.phase] = max(0, round((now - self.phase_started_at) * 1000))
        if self.phase != phase:
            self.phase = phase
            self.phase_started_at = now
        payload = {
            "phase": phase,
            "progress": max(0, min(100, int(progress))),
            "message": str(message or ""),
            "status": status,
            "elapsedMs": max(0, round((now - self.started_at) * 1000)),
            "timings": dict(self.timings),
            **details,
        }
        if callable(self.callback):
            try:
                self.callback(payload)
            except Exception:
                # Progress reporting is observational and must never make an
                # otherwise safe account transaction fail.
                pass

    def completed(self, message: str = "Codex 已完成启动与身份回验") -> None:
        self.emit("completed", 100, message, status="completed")

    def failed(
        self,
        error: BaseException | str,
        *,
        failed_phase: str | None = None,
        message: str = "切换未完成，原账号与配置已安全恢复",
        recovery_state: str = "restored",
        can_retry: bool = True,
    ) -> None:
        failed_phase = failed_phase or self.phase
        detail = _core._redact_sensitive_text(error, limit=360)
        self.emit(
            "failed",
            100,
            message,
            status="error",
            error=detail,
            failedPhase=failed_phase,
            recoveryState=recovery_state,
            canRetry=bool(can_retry),
        )

    def summary(self) -> dict:
        return {
            "totalMs": max(0, round((_core.time.perf_counter() - self.started_at) * 1000)),
            "stages": dict(self.timings),
        }