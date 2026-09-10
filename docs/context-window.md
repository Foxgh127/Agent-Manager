# 上下文窗口与可用预算

1.1.0 区分模型总上下文、最大输入和 Codex 实际可用预算；调度中心显示设置值及预计可用值。

## 1M 设置仍显示 828K

已核对的故障状态是：`model_context_window=1000000` 已保存，但生成的 Astra 模型目录仍为 `max_context_window=872000`，`effective_context_window_percent=95`。

Codex 先将请求窗口限制为 `min(配置值, 模型目录上限)`，再计算可用比例，因此得到 `872000×95%=828400`。配置文件保存成功并不代表模型目录的上限同步成功。[Codex 配置覆盖源码](https://github.com/openai/codex/blob/main/codex-rs/models-manager/src/model_info.rs)、[可用窗口计算](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/openai_models.rs)

最新核实的 Astra、Sol/Terra/Luna 模型资料另有 **922,000 最大输入**和 **128,000 最大输出**。1,050,000 是总上下文，不能全部当作输入。此前仅按 1M×95% 得到 950K 的说明漏掉了输入限制，本版本纠正这一点。[Astra 模型资料](https://developers.openai.com/api/docs/models/gpt-6-astra)、[Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)

现在显式扩展窗口时，管理目录上限最多扩展到已核实的输入范围，并保留 Codex 原生预留比例。对于原目录 872K、设置 1M、输入参考上限 922K、可用比例 95% 的情况，预计可用为 `922000×95%=875900`。更高的原生来源元数据不会被离线参考值随意覆盖；第三方 Provider 不借用官方同名模型的规格。恢复模型默认后移除托管扩展。

界面增加预计可用容量与预留说明；配置健康检查也检查目录上限，避免只检查 TOML 就误判同步完成。旧任务保留的统计不会原地更新，需要在本地试用版同步后让 Codex 重新加载配置。

压缩点是配置值，Codex 还会按有效模型窗口约束实际触发阈值。计费是否进入长上下文档位使用请求实际输入量判断，和这个配置值不是同一个概念。

测试覆盖配置/目录联动、输入上限、预留比例、重置默认、官方别名与第三方来源隔离。验证不调用收费模型；真实任务还需在同步后让 Codex 重新加载配置。
