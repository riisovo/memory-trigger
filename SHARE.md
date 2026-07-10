# memory-kit —— 让 AI 真正记住你、且不前后矛盾

一个**可分享**的记忆方案，核心解决：AI 长期记忆里「新旧偏好打架、召回串台」的问题。
由 Plato 审改自一份记忆触发器模板，经实跑验证（见 `test_ramen.py`，10/10 通过）。

---

## 两种形态，按你的 AI 选

| 你的 AI | 用哪个 | 说明 |
|---|---|---|
| 纯网页 / App 聊天 AI（有长期记忆功能） | **MEMORY_RULES.md** | 零代码，贴进记忆设定即用 |
| 能跑代码 / 接 MCP 的 AI / Agent | **references/write_pipeline.py** | 代码强制，命中率最高 |

两版实现同一套「不打架」逻辑，只是载体不同。

---

## 零代码版（MEMORY_RULES.md）

把文件里【给你的 AI 的指令】整段复制，粘贴到你 AI 的「长期记忆 / 自定义指令 / 系统提示」里。
支持任何有记忆功能的聊天产品。无环境要求。

---

## 工程版（references/write_pipeline.py）

需要 Python 3.10+ 与本地文件权限（或挂在 MCP server 里）。

```bash
python references/write_pipeline.py init local ./mem
python references/write_pipeline.py write 拉面 preference "喜欢吃拉面" --sentiment pos ./mem
python references/write_pipeline.py write 那家拉面店 preference "再也不想吃" --sentiment neg ./mem
python references/write_pipeline.py search 拉面 ./mem
```

特点：
- 实体归一化（`references/aliases.json` + 反向子串）
- 偏好翻转（旧值 `superseded` 不删，只返回 `active`）
- 事件 / 偏好分层（`kind` 字段：preference / event）
- 原子写 + fcntl 锁 + WAL + 崩溃恢复（`recover`）
- UUID 防撞

验证：`python test_ramen.py`

---

## 包成 MCP server（给能接 MCP 的 AI 即插即用）

`references/write_pipeline.py` 当前是 CLI。要让 Claude Desktop 等支持 MCP 的客户端挂载，把它封装为 MCP server（用官方 `mcp` sdk 暴露 `write` / `search` / `recover` 工具）即可。封装后任何支持 MCP 的客户端一键挂载，记忆引擎即插即用。

---

## 文件清单

- `MEMORY_RULES.md` —— 零代码守则版（分享首选）
- `references/write_pipeline.py` —— 工程版管线
- `references/aliases.json` —— 别名归一化表示例
- `test_ramen.py` —— 验证用例（拉面场景 + recover 修复）
- `SHARE.md`（本文件）—— 发布说明

---

## 许可

自由分享、修改、转发。署名随意，改坏不赔 😏
