memory-trigger 补丁包：升到 v2.8.3（含 v2.8.2 entity 修复 + v2.8.3 三十 bug 加固）
=============================================================================

本 zip 含两份补丁，请按你当前部署的版本选对应的一份 local apply：

  • 停在 v2.8.0 的用户  → memory-trigger-v2.8.0-v2.8.3-combined.patch
  • 停在 v2.8.1 的用户  → memory-trigger-v2.8.1-v2.8.3-combined.patch

（GitHub main / 线上克隆当前已是 **v2.8.3 终态**，新部署直接 clone/pull 主线即可；
停在 v2.8.0 / v2.8.1 的外部老部署才需要本补丁（绝大多数是 v2.8.1 那份）。两份都已在本地用
 git apply --check 验证可干净 apply。）

包含改动（相对你当前版本）
-------------------------
1. references/mcp_server.py
   - v2.8.2：entity 写入必填校验 + 首次触达自动 selfcheck 自愈 + 暴露 memory_selfcheck 工具
   - v2.8.3：权限/写入错误直白中文提示
2. references/write_pipeline.py（核心加固）
   - 数据安全(P0)：损坏文件隔离+从备份恢复、WAL upsert 幂等去重(根治无限重放)、
     recover 不再破坏他进程 flock 互斥、search 锁内读改写、备份 write-through(零丢失)、
     严格 JSON(禁裸 NaN/Infinity，兼容非 Python 客户端)、同日归档文件名冲突修复
   - 脏数据鲁棒(P1)：所有方括号直取改 .get 兜底、value/importance/created 缺字段全程 try、
     锁损坏回退 TimeoutError 而非 UnboundLocalError、wellness 数值校验
   - 语义精确(P2)：空/纯空格 entity 拒绝、normalize_entity 不再子串吞并、search 不改写查询词
     /不倒库、forget 清理 suppressed 状态、confidence 越界拒绝

如何应用
--------
方法 A（git 仓库，推荐）：
    cd <你的 memory-trigger 目录>
    git apply /path/to/memory-trigger-v2.8.X-v2.8.3-combined.patch
    # 或：patch -p1 < /path/to/memory-trigger-v2.8.X-v2.8.3-combined.patch

方法 B（非 git / 无法 apply）：按 diff 的 @@ 行号，把 + 行手动合入对应文件。

应用后无需重启；下一次 memory 操作即走新逻辑。

验证结论（开发仓库已实测）
--------------------------
- 极限压测 5 轮共 182 例：修复后 182/182 PASS，0 BUG。
- 反向对照：同一套 182 例打在修复前 v2.8.1 上 → 29 BUG（证明压测确实能抓真 bug，非假绿）。
- 原有单测：test_dual_source 20/20 + references/test_dual_source 27/27 全过。
- MCP stdio 端到端：11 个工具 + 完整写入/检索/遗忘生命周期正常。

注意
----
- 补丁只给外部部署本地升级用。开发仓库 memory-trigger-v2.7 已是 v2.8.3 终态，
  勿对其 apply（会 patch does not apply）。
- 想直接拿最新版的人，重新 clone / pull 主线即可（已发布 v2.8.3）。
