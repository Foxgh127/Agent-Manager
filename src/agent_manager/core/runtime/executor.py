"""Command execution"""
from __future__ import annotations
from agent_manager import core as _core

def run_codex_capture(args: list[str], timeout: int = 60, env: dict | None = None) -> _core.subprocess.CompletedProcess:
    flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0) if _core.os.name == "nt" else 0
    # Agent Manager can itself be launched from a captured/non-interactive
    # terminal where TERM=dumb is inherited.  Codex Doctor treats that marker
    # as a terminal failure even though this helper intentionally captures all
    # output and never needs terminal capabilities.  Do not let the parent's
    # presentation hint turn an otherwise healthy configuration red.
    child_env = dict(_core.os.environ if env is None else env)
    if str(child_env.get("TERM") or "").casefold() == "dumb":
        child_env.pop("TERM", None)
    return _core.subprocess.run(
        _core.codex_prefix() + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=child_env,
        creationflags=flags,
    )

