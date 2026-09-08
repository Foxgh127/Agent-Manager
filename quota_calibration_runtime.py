"""Background, coalesced calibration sampling; card reads never scan history."""
from __future__ import annotations
import hashlib
import json
import threading
import time
import agent_manager_core as core
import quota_estimation


def combined_observation(account: dict, snapshot: dict) -> dict:
    live = (snapshot.get("codexSessions") or {}).get("liveCoverage") or {}
    account_id = str(account.get("id") or "")
    gateway_rows = snapshot.get("byAccount") or []
    row = next((item for item in gateway_rows if isinstance(item, dict) and str(item.get("accountId") or "") == account_id), {})
    def counter(value):
        try: return max(0, int(value or 0))
        except (ValueError, TypeError, OverflowError): return 0
    tokens = counter((live.get("accounts") or {}).get(account_id)) + counter(
        row.get("totalTokens", counter(row.get("inputTokens")) + counter(row.get("outputTokens"))))
    missing = counter(row.get("usageMissingCount"))
    generation = str(snapshot.get("counterGeneration") or "")
    valid_gateway = bool((snapshot.get("coverage") or {}).get("counterGenerationValid") and generation)
    # Historical missing reports cancel out in the baseline. A newly missing
    # report changes this account's epoch and prevents bridging that interval.
    epoch = hashlib.sha256(json.dumps([live.get("epoch"),generation,missing]).encode()).hexdigest()
    return {
        "accountId":account_id, "cumulativeTokens":tokens,
        "observedAt":live.get("observedAt") or core.now_iso(),
        "coverageComplete":bool(live.get("complete") and live.get("epoch") and valid_gateway),
        "coverageEpoch":epoch, "usageMissingCount":0,
        "scope":"native_direct_main_plus_gateway_reported",
    }


class QuotaCalibrationSampler:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.last_key = None
        self.last_at = 0.0

    @staticmethod
    def eligible(accounts):
        now = time.time()
        result = []
        for account in accounts:
            if not isinstance(account, dict) or account.get("authMode") != "chatgpt" or account.get("sourceType") != "codex_auth":
                continue
            usage = account.get("usage") if isinstance(account.get("usage"),dict) else {}
            errors = account.get("refreshErrors") if isinstance(account.get("refreshErrors"),dict) else {}
            timestamp = core._parsed_datetime(usage.get("updatedAt"))
            if timestamp and 0 <= now - timestamp.timestamp() <= quota_estimation.MAX_ALIGNMENT_SECONDS and not errors.get("usage"):
                result.append(account)
        return result

    def schedule(self, accounts=None):
        if getattr(self.runtime,"_closed",False) or getattr(self.runtime,"_restart_prepared",False):
            return
        if getattr(self.runtime,"configuration_session",{}).get("status") in {"waiting","starting"}:
            return
        accounts = self.eligible(accounts if accounts is not None else core.load_settings().get("accounts",[]))
        if not accounts: return
        key = tuple(sorted((str(a["id"]),str(a.get("fingerprint") or ""),str(a["usage"].get("updatedAt"))) for a in accounts))
        with self.lock:
            if key == self.last_key or time.monotonic() - self.last_at < 20 or (self.thread and self.thread.is_alive()):
                return
            self.last_key = key
            self.last_at = time.monotonic()
            self.stop.clear()
            self.thread = threading.Thread(target=self._run,args=(accounts,),name="quota-calibration-sampler",daemon=True)
            self.thread.start()

    def _run(self, accounts):
        try:
            snapshot = self.runtime.web2api.usage_snapshot()
            if self.stop.is_set() or getattr(self.runtime,"_closed",False): return
            for account in accounts:
                quota_estimation.observe(account,combined_observation(account,snapshot))
            if not (snapshot.get("codexSessions") or {}).get("liveCoverage",{}).get("complete"):
                with self.lock:
                    self.last_key = None
        except Exception:
            # Estimates are optional observability, never an authentication or
            # startup failure. Existing summaries remain available for reads.
            pass

    def decorate(self, accounts):
        selected = [a for a in accounts if isinstance(a,dict) and a.get("authMode") == "chatgpt" and a.get("sourceType") == "codex_auth"]
        try:
            # Public account records deliberately omit fingerprints. Always
            # resolve the private sampling identity locally, without returning
            # it to the browser or training a second public-only identity.
            settings = core.read_json(core.SETTINGS_FILE, {})
            by_id = {str(a.get("id")):a for a in settings.get("accounts",[]) if isinstance(a,dict)}
            trusted = []
            for public in selected:
                current = by_id.get(str(public.get("id")))
                if current and all(not public.get(k) or not current.get(k) or str(public[k]).casefold() == str(current[k]).casefold() for k in ("email","accountId")):
                    trusted.append(current)
            summaries = quota_estimation.read_summaries(trusted)
            for account in selected:
                account["quotaEstimate"] = summaries.get(str(account["id"]))
            self.schedule(trusted)
        except Exception:
            for account in selected:
                account["quotaEstimate"] = {"status":"pending","reason":"calibration_store_unavailable"}

    def close(self):
        self.stop.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
