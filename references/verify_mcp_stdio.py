#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实 stdio 端到端验证：用 MCP client 连上同目录的 mcp_server.py，发 call_tool 走完整协议层。

放在 references/ 下，与 mcp_server.py 同目录。前置：已安装 mcp
    pip install -r mcp_requirements.txt   # 或：uv run --with mcp python references/verify_mcp_stdio.py
运行：
    python3 references/verify_mcp_stdio.py

它会真实拉起 server 子进程、通过 stdio 传输调用工具，验证：
  - 11 个 MCP 工具 + 1 个 Prompt 注册齐全
  - 双源信任：user_explicit 直接落盘 core 钉死；self_inferred 低置信挂 pending 不进检索
  - 双向遗忘：forget 后检索归零
"""
import asyncio
import os
import sys
import tempfile

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")
# 运行 server 的解释器：默认用当前 python（需已装 mcp），可用 MT_PYTHON 覆盖
PY = os.environ.get("MT_PYTHON", sys.executable)


def _txt(res):
    return "".join(
        getattr(c, "text", "") for c in res.content if getattr(c, "text", None) is not None
    )


async def main():
    refs = tempfile.mkdtemp(prefix="mt_verify_")
    params = StdioServerParameters(
        command=PY,
        args=[SERVER],
        env={**os.environ, "MEMORY_TRIGGER_REFS_DIR": refs},
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            tools = await sess.list_tools()
            print("TOOLS(%d):" % len(tools.tools), [t.name for t in tools.tools])
            prompts = await sess.list_prompts()
            print("PROMPTS(%d):" % len(prompts.prompts), [p.name for p in prompts.prompts])

            res = await sess.call_tool("memory_init", {"mode": "local", "refs_dir": refs})
            print("INIT:", _txt(res))

            res = await sess.call_tool("memory_write", {
                "entity": "用户", "kind": "relationship", "value": "我的伴侣",
                "refs_dir": refs, "source": "user_explicit", "confidence": 1.0, "core": "true"})
            print("WRITE_CORE:", _txt(res))

            res = await sess.call_tool("memory_write", {
                "entity": "观影", "kind": "preference", "value": "用户爱看科幻片",
                "refs_dir": refs, "source": "self_inferred", "confidence": 0.3})
            print("WRITE_LOW:", _txt(res))

            res = await sess.call_tool("memory_search", {"query": "观影", "refs_dir": refs})
            print("SEARCH_LOW(应 results_count=0):", _txt(res))

            res = await sess.call_tool("memory_write", {
                "entity": "观影", "kind": "preference", "value": "用户爱看科幻片",
                "refs_dir": refs, "source": "self_inferred", "confidence": 0.95})
            res = await sess.call_tool("memory_search", {"query": "观影", "refs_dir": refs})
            print("SEARCH_HIGH(应 >=1):", _txt(res))

            res = await sess.call_tool("memory_stats", {"refs_dir": refs})
            print("STATS:", _txt(res))

            res = await sess.call_tool("memory_forget", {"entity_or_id": "观影", "refs_dir": refs})
            print("FORGET:", _txt(res))
            res = await sess.call_tool("memory_search", {"query": "观影", "refs_dir": refs})
            print("SEARCH_AFTER_FORGET(应 results_count=0):", _txt(res))

    print("REFS_DIR:", refs)
    print("VERIFY_DONE")


if __name__ == "__main__":
    asyncio.run(main())
