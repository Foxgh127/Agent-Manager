// Technical diagnostics remain in backend logs, not native hover tooltips.
export function updateFailureMessage(error, { installation = false } = {}) {
  if (!error) return "";
  const text = String(error);
  if (/verified ready|New manager is still running|ready window/i.test(text)) return "上次更新启动确认未完成，请重新检查";
  if (installation) return "上次更新未完成，请重试";
  if (/timeout|超时/i.test(text)) return "检查超时，请重试";
  if (/network|fetch|网络|连接/i.test(text)) return "连接失败，请重试";
  return "检查未成功，请重试";
}
