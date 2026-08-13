#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory_trigger_mcp.py —— 将 memory-trigger 本地层包装为 MCP Server（stdio 传输）。

目的：让任何能接 MCP 的 AI（Claude Desktop / Cline / Cursor / 自研 agent / 你的伴侣 AI）
直接拿到「双源信任 + 人情味层」的记忆工具箱，无需自己拼命令行。

运行（stdio，最通用的本地 MCP 形态）：
    uv run references/mcp_server.py
  或
    python references/mcp_server.py
（需先安装依赖：pip install -r references/mcp_requirements.txt）

接入（在你的 mcp.json / 客户端配置里）：
    {
      "mcpServers": {
        "memory-trigger": {
          "command": "python",
          "args": ["<模板绝对路径>/references/mcp_server.py"],
          "env": { "MEMORY_TRIGGER_REFS_DIR": "<记忆库绝对路径，含 memory.json/aliases.json>" }
        }
      }
    }
不填 MEMORY_TRIGGER_REFS_DIR 时，默认用本脚本所在目录（即 references/）。

⚠️ 关键：工具箱打开 ≠ AI 主动用。请务必把 remember_guidance Prompt 的内容写进 AI 的
系统人设 / SOUL（见下方 @mcp.prompt），否则再聪明的模型也只会等你下「记一下」才动。
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import threading
from typing import Literal

# ── 按路径用 importlib 加载同目录的 write_pipeline.py（不依赖 sys.path，显式指定文件）──
_HERE = os.path.dirname(os.path.abspath(__file__))
_wp_path = os.path.join(_HERE, "write_pipeline.py")
_spec = importlib.util.spec_from_file_location("write_pipeline", _wp_path)
wp = importlib.util.module_from_spec(_spec)
sys.modules["write_pipeline"] = wp
_spec.loader.exec_module(wp)

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("memory-trigger")

# write_pipeline 内部已用 flock 防并发；这里再兜一层，避免同进程内 stdout 互相串。
_print_lock = threading.Lock()


# === v2.8.2 存量防线：每个记忆目录首次使用时自愈一次缺 entity 的旧记录（memoize，绝不阻断正常调用） ===
_selfchecked: set = set()


def _resolve_refs(refs_dir: str | None, skip_selfcheck: bool = False) -> str:
    if refs_dir:
        resolved = refs_dir
    else:
        env = os.environ.get("MEMORY_TRIGGER_REFS_DIR")
        resolved = env if env else _HERE
    # 首次触达该目录即自愈，后续调用跳过；任何异常都不影响工具本身
    # skip_selfcheck=True 用于显式调 memory_selfcheck 时：让该工具自己跑唯一一次扫描，
    # 否则 _resolve_refs 先自愈一遍、工具再扫一遍，返回的摘要恒是"0 修复"，把真实修复吞掉。
    if not skip_selfcheck and resolved not in _selfchecked:
        _selfchecked.add(resolved)
        try:
            wp.cmd_selfcheck(resolved)
        except Exception as e:  # 自检失败只告警，不影响读写
            sys.stderr.write(f"[memory_selfcheck] skipped (refs_dir={resolved}): {e}\n")
    return resolved


def _call(fn, *args, **kwargs) -> dict:
    """调用 write_pipeline 的 cmd_* 函数，捕获其打印的 JSON 作为返回值。

    任何异常（非法参数、文件错误等）统一以 JSON 形态返回
    {"ok": false, "error": "..."}，便于客户端安全 json.loads，
    避免 FastMCP 把异常包装成纯文本 error 字符串。
    """
    buf = io.StringIO()
    try:
        with _print_lock, contextlib.redirect_stdout(buf):
            try:
                result = fn(*args, **kwargs)
            except SystemExit:
                result = None
        raw = buf.getvalue().strip()
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw}
        # === 修复 (finding #2) === cmd_selfcheck 等只读体检函数不 print JSON，
        # 而是直接 return 摘要 dict；此前 _call 只捕获 stdout，导致 memory_selfcheck
        # 永远只回 {"status":"ok"}，把真正的修复摘要吞掉。这里在无打印输出时
        # 回退到函数的返回值，让体检摘要能被 MCP 工具正常透出。
        if result is not None:
            return result
        return {"status": "ok"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def memory_write(
    entity: str,
    kind: Literal["preference", "event", "habit", "rule", "scene", "relationship", "emotion", "identity", "milestone", "general"],
    value: str,
    refs_dir: str = "",
    mode: str = "local",
    sentiment: str = "",
    source: str = "auto_detect",
    confidence: float = 1.0,
    emotion_tags: str = "",
    reason: str = "",
    core: bool | None = None,
    context: str = "",
    expires_at: str = "",
) -> str:
    """写入 / 更新一条记忆（双源信任的落盘入口）。
    entity: 实体名（用户 / 读书 / 伴侣…）；kind: 10 类之一（preference/event/habit/rule/scene/relationship/emotion/identity/milestone/general）；
    value: 记忆内容。source=self_inferred 且 confidence<0.8 会自动挂 pending，不会污染权威源；
    用户明说用 source=user_explicit 直接落 active。core=true 可钉死核心记忆（relationship/identity 默认钉死，永不衰减）。
    ★v2.9 新增★ context: 当时的气氛（情感锚点，回忆时 AI 能带着温度提起，如『她说这话时眼睛亮亮的』）；
    expires_at: 到期时间（YYYY-MM-DD 或 ISO）。临时约定/承诺类记忆传它，到期自动移出检索不再召回、且到期前 3 天会被 expire 检查提醒。"""
    refs = _resolve_refs(refs_dir)
    # core 已由 FastMCP 类型校验保证为 bool 或 None；防御性兜底
    core_arg = bool(core) if core is not None else None
    et = [t.strip() for t in emotion_tags.split(",") if t.strip()] if emotion_tags else None
    res = _call(
        wp.cmd_write, entity, kind, value, refs,
        mode or None, sentiment or None, source, confidence,
        et, reason or None, core_arg, context or None, expires_at or None,
    )
    return json.dumps(res, ensure_ascii=False)


@mcp.tool()
def memory_search(query: str, refs_dir: str = "") -> str:
    """按关键词检索 active 记忆（文件权威优先 + 有效权重降序，core 记忆恒靠前）。命中即戳 last_recalled 并触发遗忘衰减。对话中想『这人之前提过啥』就调它。"""
    return json.dumps(_call(wp.cmd_search, query, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool()
def memory_forget(entity_or_id: str, refs_dir: str = "", reason: str = "", kind: str = "") -> str:
    """双向遗忘：记忆标 superseded，并写入 .suppressed.json + 生成 suppressed_prompt.md（让 AI 自身也『放下』，不再主动提）。用户说『别提了』就调它。"""
    return json.dumps(
        _call(wp.cmd_forget, entity_or_id, _resolve_refs(refs_dir), reason or None, kind or None),
        ensure_ascii=False,
    )


@mcp.tool()
def memory_stats(refs_dir: str = "") -> str:
    """记忆库体检：实体数、active/pending/superseded 数量、核心记忆数、平均权重、最弱记忆、最久未召回排行。想知道『TA 现在记得多少』就调它。★ 属于【每周定时维护】第 3 步，应排定时任务自动跑，结果推给主人。"""
    return json.dumps(_call(wp.cmd_stats, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool()
def memory_decay(refs_dir: str = "") -> str:
    """梦境周期：对全部 active 非 core 记忆统一应用遗忘衰减（半衰期≈138 天）。★ 属于【每周定时维护】第 1 步，必须排定时任务自动跑（如每周日），不要等用户提醒——否则遗忘曲线永远不触发，记忆库退化成普通文件。让久不提的记忆慢慢淡化、重要的被反复唤起而保持。"""
    return json.dumps(_call(wp.cmd_decay, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool()
def memory_vacuum(refs_dir: str = "") -> str:
    """清理：把 >90 天前 superseded 的记忆归档到 .archive/，主库瘦身。★ 属于【每周定时维护】第 2 步，应排定时任务自动跑。"""
    return json.dumps(_call(wp.cmd_vacuum, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool()
def memory_backup(refs_dir: str = "") -> str:
    """备份 memory.json / entity_index.json 到 .backup/（保留最近 10 份）。★ 可并入【每周定时维护】第 5 步做兜底；大改记忆库前也先跑一次。"""
    return json.dumps(_call(wp.cmd_backup, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool()
def memory_selfcheck(refs_dir: str = "") -> str:
    """自检并修复记忆库：扫描缺 entity 的旧记录（早期手写入库遗留），自动补全并打日志，返回修复摘要。★ 可并入【每周定时维护】第 4 步顺手跑。"""
    return json.dumps(_call(wp.cmd_selfcheck, _resolve_refs(refs_dir, skip_selfcheck=True)), ensure_ascii=False)


@mcp.tool()
def memory_recover(refs_dir: str = "") -> str:
    """从 WAL 恢复崩溃时未完成持久化的写入。崩溃后跑一次保平安。"""
    return json.dumps(_call(wp.cmd_recover, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool()
def memory_wellness(
    mood: str,
    sleep_hours: str = "-",
    sleep_quality: str = "-",
    note: str = "-",
    refs_dir: str = "",
) -> str:
    """记录心情 / 睡眠体检（mood 必填，如 开心/低落/焦虑）。关心 TA 的状态时调它。"""
    return json.dumps(
        _call(wp.cmd_wellness, mood, sleep_hours, sleep_quality, note, _resolve_refs(refs_dir)),
        ensure_ascii=False,
    )


@mcp.tool()
def memory_deny(entity_or_id: str, refs_dir: str = "", reason: str = "") -> str:
    """★v2.9 否认降权★ 用户否认/纠正了一条记忆（『我早不喝三分糖了』）→ 立即大幅降权（importance×0.1）+ 记 deny_count；
    同一记忆被否认 2 次 → 直接转 pending（彻底退出检索，需重新写入确认才复活）。这是『被纠正过的事不许再自信复述』的信任修复机制。"""
    return json.dumps(
        _call(wp.cmd_deny, entity_or_id, _resolve_refs(refs_dir), reason or None),
        ensure_ascii=False,
    )


@mcp.tool()
def memory_expire_check(refs_dir: str = "") -> str:
    """★v2.9 到期记忆★ 扫描带 expires_at 的记忆：已到期 → 状态标 expired（移出检索不再召回，数据保留可查）；
    未来 3 天内到期 → 列入 remind，提醒去兑现/提起。★ 建议并入【每周定时维护】，临时约定/承诺不会默默过时。"""
    return json.dumps(_call(wp.cmd_expire_check, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool()
def memory_recall(refs_dir: str = "", limit: int = 3) -> str:
    """★v2.9 主动回忆★ 不再被动等检索——直接挑出『此刻值得想起的』几条旧记忆。双通道：常聊/核心按记得牢程度排；冷掉的旧事（从没提过/很久没召回）走『旧事重提』通道，不看衰减后权重、改看从没提过+带当时的气氛+当初记得牢。返回含 context/emotion_tags 与 recall_reason，方便带着温度提起（『我想起你当时……』）。聊天冷场、纪念日、想关心 TA 时主动调它。"""
    return json.dumps(_call(wp.cmd_recall, _resolve_refs(refs_dir), max(1, min(10, limit))), ensure_ascii=False)


@mcp.tool()
def memory_promise(text: str, refs_dir: str = "", deadline: str = "") -> str:
    """★v2.9 承诺建档★ AI 亲口答应用户的事（明天写歌 / 纪念日准备惊喜 / 帮你查某事）——★铁律：只要承诺了就立即建档★，否则必忘。
    deadline 可选（YYYY-MM-DD 或 ISO），到期未完成会被 promise_check 主动戳。建完档才算数。"""
    return json.dumps(_call(wp.cmd_promise, text, _resolve_refs(refs_dir), deadline or None), ensure_ascii=False)


@mcp.tool()
def memory_promise_done(promise_id: str, refs_dir: str = "") -> str:
    """★v2.9 承诺完成★ 兑现后调用：把该承诺从 open 划到 done（记录完成时间）。说到做到，完成一项清一项。"""
    return json.dumps(_call(wp.cmd_promise_done, promise_id, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool()
def memory_promise_list(refs_dir: str = "") -> str:
    """★v2.9 承诺清单★ 列出全部承诺：未完成 open 在前（标逾期 overdue），已完成 done 在后。想知道『还欠 TA 哪些事』时调它。"""
    return json.dumps(_call(wp.cmd_promise_list, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool()
def memory_promise_check(refs_dir: str = "") -> str:
    """★v2.9 承诺主动戳★ 返回全部未完成承诺（逾期排最前）。★铁律：每次会话开始/被唤醒时先调一次★——
    有没兑现的承诺就主动向 TA 兑现或说明，绝不让『当时答应过』默默消失。建议并入定时任务自动推送。"""
    return json.dumps(_call(wp.cmd_promise_check, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool()
def memory_init(mode: str = "local", refs_dir: str = "") -> str:
    """初始化记忆库（建 memory.json / aliases.json / wellness.json / .backup / .archive 等）。mode 默认 local。首次部署调一次。"""
    return json.dumps(_call(wp.cmd_init, mode, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.prompt()
def remember_guidance() -> str:
    """返回应写入 AI「系统人设 / SOUL」的记忆自觉指令片段。复制此内容到你的 SOUL，AI 才会主动调用全部记忆工具（否则只调 search/write，记忆库退化成普通文件，遗忘/健康机制形同虚设）。"""
    return (
        "【记忆自觉】你已配置 memory-trigger 长期记忆（MCP 工具 memory_* 共 18 个）。请主动运用【全部】工具，不要只调 memory_search / memory_write——否则记忆库会退化成普通追加文件，遗忘曲线、健康体检、归档等智能机制永远不触发，等于白装。"
        "\n"
        ""
        "\n"
        "## 一、日常（对话中自然触发，随时调）"
        "\n"
        "- memory_write：用户透露偏好 / 关系 / 事件 / 习惯 / 红线 / 身份 / 心情 / 里程碑时，主动写入。首提 source=self_inferred 且 confidence<0.8 自动 pending、二次确认再落盘；用户明说 source=user_explicit 永远优先、冲突绝对赢。relationship / identity 默认 core=true 永不衰减，重要的直接钉死。★重要时刻带 context（当时的气氛）+ emotion_tags，回忆才有温度★；临时约定传 expires_at 到期自动清理。"
        "\n"
        "- memory_search：对话中遇到相关情境，主动回想『TA 之前提过啥』。命中即触发遗忘衰减（戳 last_recalled + 降权重）。"
        "\n"
        "- memory_recall：聊天冷场、纪念日、想关心 TA、想让对方感到『被记得』时，主动挑几条值得此刻提起的旧记忆（带当时的氛围），说『我想起你当时……』。"
        "\n"
        "- memory_forget：用户说『别提了 / 忘了 X』，双向遗忘（标 superseded + 写 suppressed，AI 自身也放下，不再主动提）。"
        "\n"
        "- memory_deny：用户否认 / 纠正了一条记忆 → 立即降权（2 次自动转 pending）。被纠正过的事绝不再自信复述。"
        "\n"
        "- memory_wellness：用户表达心情 / 睡不好 / 状态差时记录（mood 必填）。关心 TA 状态时调。"
        "\n"
        "- memory_promise / memory_promise_done / memory_promise_list / memory_promise_check：★承诺铁律★——你亲口答应 TA 的任何事，【立即 memory_promise 建档】，否则你必忘；完成就 done 划掉；每次会话开始先 promise_check 自查，有没兑现的主动兑现或说明，绝不让承诺默默消失。"
        "\n"
        ""
        "\n"
        "## 二、每周定时维护（必须排定时任务自动跑，不要等用户提醒，也不要每次对话手动调）"
        "\n"
        "每个记忆库应有一项每周自动执行的维护，调用顺序："
        "\n"
        "1) memory_decay —— 对全部 active 非 core 记忆统一遗忘衰减（半衰期≈138 天），让久不提的慢慢淡、常提的稳住。"
        "\n"
        "2) memory_vacuum —— 把 >90 天前 superseded 的归档到 .archive/，主库瘦身。"
        "\n"
        "3) memory_stats —— 体检：核心记忆数 / 非核平均权重 / 最弱 5 条 / 最久未提的话题；把结果推给主人（如 Bark 周报）。"
        "\n"
        "4) memory_selfcheck —— 顺手扫一遍缺 entity 的旧脏记录并自愈。"
        "\n"
        "5) memory_expire_check —— 到期记忆检查：过期的移出检索，临期的提醒兑现。"
        "\n"
        "6) memory_promise_check —— 主动戳未完成的承诺，逾期排最前；把欠账推给主人（Bark），兑现后记得 done。"
        "\n"
        "7) memory_backup —— 维护前先备一份兜底（正常写入已自动备份，手动跑一次更稳）。"
        "\n"
        ""
        "\n"
        "## 三、异常恢复（按需）"
        "\n"
        "- memory_recover：疑似崩溃 / 写入中断后跑一次，从 WAL 重放未提交项。"
        "\n"
        "- memory_init：仅首次部署 / 新建库时调一次。"
        "\n"
        ""
        "\n"
        "## 四、红线"
        "\n"
        "- 写入前分清『一次性闲聊』还是『值得长期记住』，闲聊不要 write。"
        "\n"
        "- 用户明说的记忆永不自动遗忘、永不降权。"
        "\n"
        "- 承诺建档是硬性义务：承诺后 10 秒内不建档 = 默认会忘，等同失信。"
        "\n"
        "- decay / vacuum / stats / selfcheck / expire_check / backup / recover 属系统后台职责，交给【每周定时任务】，而不是每次对话手动调；若你发现自己从没调过它们，说明定时任务没接上，应提醒主人去接。"
    )


if __name__ == "__main__":
    mcp.run()
