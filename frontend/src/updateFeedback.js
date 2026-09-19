// Technical diagnostics remain in backend logs, not native hover tooltips.
export function updateFailureMessage(error, { installation = false } = {}) {
  if (!error) return "";
  const text = String(error);
  if (/verified ready|New manager is still running|ready window/i.test(text)) return "上次更新启动确认未完成，请重新检查";
  if (/not safely exited|application has not safely exited|退出未完成|未能安全退出/i.test(text)) {
    return "旧版本已保留：软件尚未安全退出，请关闭残留窗口后重试";
  }
  if (installation) return "上次更新未完成，请重试";
  if (/metadata_too_large|元数据超过读取上限|更新元数据|manifest|清单|更新说明字段无效/i.test(text)) {
    return "发布方的更新清单无效，请稍后重试";
  }
  if (/timeout|超时/i.test(text)) return "检查超时，请重试";
  if (/network|fetch|网络|连接/i.test(text)) return "连接失败，请重试";
  return "检查未成功，请重试";
}
