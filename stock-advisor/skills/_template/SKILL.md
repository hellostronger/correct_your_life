---
name: my-skill
version: 1.0.0
---

# my-skill

一句话说明这个 skill 做什么。

## 上游

- 项目：
- 版本/commit：
- 文档：

## 输入（stdin）

运行时会把参数包成一个对象从 stdin 传入：

```json
{"params": {"query": "..."}}
```

## 输出（stdout）

**必须只输出一个 JSON 对象**，不要打印日志、进度条、颜色码：

```json
{
  "ok": true,
  "data": {},
  "citations": ["https://..."],
  "warnings": []
}
```

- `ok=false` 表示业务失败（例如上游限流、没有结果），`data` 里写原因；
- 每条结论都要能在 `citations` 里找到来源；没有来源就不要下结论；
- `warnings` 用来提示数据过期、口径差异、部分失败；
- 失败就明确失败，不要用空数组伪装成功。

## 约束

- 只读：不得写本仓库、不得写生产库、不产生交易动作；
- 敏感信息只从环境变量读取，并写进 `skill.json` 的 `env_allow`；
- 数据库凭据（`DB_*` / `PG*` / `DATABASE_URL`）不会被传入，也不允许依赖；
- 单次运行默认 120 秒、输出默认 64 KB，超过即失败；
- 上游代码不复制进本仓库，只调用它的 CLI/HTTP/MCP。
