const OPERATION_NAMES = {
  usage: "额度", usageQuota: "周额度", models: "模型目录", subscription: "订阅信息",
  resetCredits: "重置卡明细", codexAccess: "访问能力", account: "账号验证",
  token: "登录凭据", credentials: "登录凭据",
};

export function refreshDiagnosticMessage(value) {
  const message = String(value || "").trim();
  if (/连接 chatgpt 超时|timed out|timeout/i.test(message)) {
    return "连接超时，已保留上次数据；稍后可刷新。";
  }
  if (/unexpected_eof_while_reading|eof occurred in violation of protocol|临时中断了安全连接|remote end closed connection|connection reset by peer/i.test(message)) {
    return "网络连接临时中断，已保留上次数据；稍后可刷新。";
  }
  return message;
}

export function accountRefreshDiagnostics(account) {
  return [["error", account?.refreshErrors], ["warning", account?.refreshWarnings]]
    .flatMap(([severity, entries]) => Object.entries(entries && typeof entries === "object" ? entries : {})
      .filter(([, value]) => String(value || "").trim())
      .map(([operation, value]) => ({ operation, severity, label: OPERATION_NAMES[operation] || "刷新",
        message: refreshDiagnosticMessage(value) })));
}

export function diagnosticSummary(entries) {
  return entries.map(item => `${item.label}：${item.message}`).join("；");
}
