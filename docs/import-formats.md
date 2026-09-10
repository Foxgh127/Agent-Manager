# 账号与 API 接入

先做本地格式预览，确认后导入；模型、权限和余额由对应来源的接口验证。格式可解析不等于远端凭据有效。

| 材料 | 支持范围 |
| --- | --- |
| 官方 Codex auth.json / 完整 OAuth 导出 | 保存真实令牌与身份，支持本地反代及具备完整材料的桌面账号切换 |
| 明确标注 Codex/OAuth 的短期 access token | 可供本地反代使用；缺少 refresh 时不自动续期，过期后重新登录；缺 id_token 不伪造桌面身份 |
| API Key + Base URL | 接入兼容 OpenAI 请求协议的上游；支持常见字段别名、JSON 包装和 key=value 文本 |
| 中转站网页登录 | 通过支持的站点接口读取同账号的可用 API Key；网页验证由用户在登录窗口完成 |
| JSON 数组、连续 JSON、NDJSON、常见嵌套导出 | 分项预览；错误条目不会吞掉其他正常账号，重复和冲突分别报告 |
| 常见第三方账号导出 | 提取 OAuth/API 凭据与必要身份，兼容既有 CPA、Cockpit、Sub2API、9Router 等输入；内部统一使用本项目结构 |
| 裸 JWT、浏览器会话、孤立 refresh token | 不凭外观假定 Codex 推理权限；材料不足时提示补充来源或重新授权，不虚构续期链 |

API 文本示例：

```text
base_url=https://api.example.com/v1
api_key=sk-example
```

明确的短期 OAuth 导出示例：

```json
{"type":"codex","access_token":"真实访问令牌","account_id":"对应工作区账号 ID"}
```

示例不包含有效凭据。对方服务必须支持所使用的端点和模型；本地导入不会把任意网站 Cookie 或任意厂商原生协议自动转换为可用 API。

同一令牌字段的别名冲突、重复 JSON 字段和相互矛盾的身份会被拒绝。管理、入口密钥和其他产品的运行配置不会作为上游账号递归导入；包装深度和数量均有上限。导出使用原生凭据格式，不附带其他项目的路由、管理或遥测设置。

设计核对采用 CPA 固定提交的 [token storage](https://github.com/router-for-me/CLIProxyAPI/blob/d1a024e9400bc65bd78ccd908945cf2eacc2835e/internal/auth/codex/token.go)、[逐文件导入](https://github.com/router-for-me/CLIProxyAPI/blob/d1a024e9400bc65bd78ccd908945cf2eacc2835e/internal/api/handlers/management/auth_files_crud.go) 和 [刷新行为](https://github.com/router-for-me/CLIProxyAPI/blob/d1a024e9400bc65bd78ccd908945cf2eacc2835e/internal/runtime/executor/codex_executor_auth.go)。这里只借鉴可互操作行为，没有内嵌其凭据仓库。
