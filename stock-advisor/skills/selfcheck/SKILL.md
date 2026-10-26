---
name: selfcheck
version: 1.0.0
---

# selfcheck

Skill 运行时自检。用于确认以下四件事：

1. **参数传递**：stdin 的 `{"params": ...}` 被完整收到；
2. **工作目录**：子进程 cwd 是 skill 自己的目录；
3. **环境隔离**：`DB_*` / `PG*` / `DATABASE_URL` 等数据库凭据没有泄漏；
4. **控制机制**：`sleep_seconds` 配合运行时的 `timeout_seconds` 能验证超时被杀。

## 参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `echo` | any | 原样回显 |
| `sleep_seconds` | number | 0-3 秒，模拟耗时 |
| `fail` | boolean | 为 true 时返回 `ok=false`，验证业务失败路径 |

## 说明

这个 skill 不访问网络、不读数据库、不写文件，是接入新 skill 时的参照实现。
复制 `skills/_template/` 到 `skills/<your-skill>/` 开始写自己的封装。
