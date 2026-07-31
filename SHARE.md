# 分享 / 部署指南

把 Memory Trigger 给别人用，两种方式：

## A. 发 GitHub 链接（最省事）

直接让对方：

```bash
git clone https://github.com/riisovo/memory-trigger
cd memory-trigger && bash install.sh
```

装完读 `SKILL.md` 即可。记忆存在对方自己机器，不共享。

## B. 打包 ZIP

```bash
zip -r memory-trigger.zip memory-trigger/ -x "*/.git/*" "*/.backup/*" "*/.wal.jsonl" "*/.lock"
```

发出去，对方解压后 `bash install.sh`。

## 自检

`install.sh` 会自动跑 `references/test_dual_source.py`，全 PASS 才算装好。手动验证：

```bash
python3 references/write_pipeline.py write "读书" preference "爱读科幻小说" references/
python3 references/write_pipeline.py search "读书" references/
python3 references/write_pipeline.py stats references/
```

## 升级

只换 `references/write_pipeline.py` + `SKILL.md` 即可，记忆数据 `references/memory.json` 不受影响（建议先 `backup`）。
