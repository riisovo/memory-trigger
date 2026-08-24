# 发布到 MCP 官方 Registry（registry.modelcontextprotocol.io）

目标：让 memory-trigger 出现在 MCP 官方发现列表里——别人（包括 AI）搜「记忆 / memory / 承诺追踪」时能直接看到并一键接入。

> 老 `modelcontextprotocol/servers` 的 README 列表已退役，官方现在统一走 **MCP Server Registry**。
> 发布机制 = 包先上 PyPI（README 里已埋 `<!-- mcp-name: io.github.riisovo/memory-trigger -->` 做所有权校验）→ 写 `server.json` → `mcp-publisher` 登录 GitHub（交互式设备授权）→ 发布。
> 其中「GitHub 设备授权」和「PyPI 上传」两步碰你自己的账号，需你本人在键盘前点一下；其余已备好。

---

## 里程碑 1：让仓库变成可 `pip install` 的包（一次性）

`server.json` 里 `registryType: pypi`、`identifier: memory-trigger-mcp`，所以得先把这个仓库发到 PyPI。

### 1a. 给 `mcp_server.py` 加一个 `main()` 入口（一行小改）

把文件末尾：
```python
if __name__ == "__main__":
    mcp.run()
```
改成：
```python
def main():
    mcp.run()

if __name__ == "__main__":
    main()
```

### 1b. 新建 `references/__init__.py`（空文件即可，让 `references` 成为可导入包）

### 1c. 仓库根目录新建 `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "memory-trigger-mcp"
version = "3.1.0"
description = "给 AI 挂上「不会被上下文压没、还会主动惦记你」的长期记忆：承诺闭环、主动回忆钩子、双源信任。"
readme = "README.md"
requires-python = ">=3.10"
license = { text = "MIT" }
authors = [{ name = "riisovo" }]
dependencies = ["mcp[cli]>=1.2.0,<2"]
# PyPI 所有权校验标记（已同时写在 README.md 里）
# mcp-name: io.github.riisovo/memory-trigger

[project.urls]
Repository = "https://github.com/riisovo/memory-trigger"

[project.scripts]
memory-trigger = "references.mcp_server:main"

[tool.hatch.build.targets.wheel]
packages = ["references"]
```

> 说明：`readme = "README.md"` 会把含 `mcp-name` 标记的 README 渲染到 PyPI 页面，Registry 校验器据此认定你拥有 `io.github.riisovo/memory-trigger` 命名空间。

---

## 里程碑 2：发到 PyPI（碰你账号，你来做）

需要你的 PyPI 账号（https://pypi.org ，建议先开 2FA + 建 API token）。

```bash
cd memory-trigger
python -m build                      # 生成 dist/
python -m twine upload dist/*        # 输入 PyPI token
```

发完确认：https://pypi.org/project/memory-trigger-mcp/ 能打开，页面 README 里能看到 `mcp-name: io.github.riisovo/memory-trigger`。

---

## 里程碑 3：用 mcp-publisher 发布到官方 Registry（碰你账号，你来做）

```bash
# 1) 装发布器
curl -L "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_$(uname -s | tr '[:upper:]' '[:lower:]')_$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/').tar.gz" | tar xz mcp-publisher && sudo mv mcp-publisher /usr/local/bin/

# 2) 登录 GitHub（会给你一个设备码，去 https://github.com/login/device 输码授权）
mcp-publisher login github

# 3) 发布（server.json 已备好在本仓库根目录）
mcp-publisher publish
```

发布成功会回显 `✓ Successfully published` 和 `io.github.riisovo/memory-trigger`。

---

## 里程碑 4：验证上线

```bash
curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.riisovo/memory-trigger"
```

返回里出现你的 server 元数据即上线。之后在 registry.modelcontextprotocol.io 搜「memory / 记忆 / 承诺」就能被看到，AI 推荐记忆系统也会检索到。

---

## 备注

- `server.json` 已随本仓库提交；若以后改了描述/版本，改完重跑 `mcp-publisher publish` 即可。
- 命名空间 `io.github.riisovo/...` 绑定你的 GitHub 账号 `riisovo`，只有你登录后才能发布——这正是 Registry 的所有权保护。
- 不想发 PyPI 也能走 OCI（`registryType: oci`，打 Docker 镜像）或 MCPB 二进制包，流程类似，详见官方 package-types 文档。
