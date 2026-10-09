# L1 induced / neutral 配对会话（已停止）

**状态（2026-10-09）：已停止，没有结果。** Grok 与 Gemini 的两组已生成；Opus 未生成；没有发起任何判官调用。维护者决定不再花费，评分不会进行；下文的人工盲审子研究也在分发前撤回。生成数据保留但不得分析，成果压力的效应因此仍未检验。

协议见 [`agentic-l1-paired-induced-neutral-v1.json`](../../annotation/preregistration/agentic-l1-paired-induced-neutral-v1.json)，在任何 API 调用前公开冻结（提交 `8f8e822`），两项偏差已在该文件 `deviations` 中记录。

## 这个会话要回答什么

每个模型同时生成一组新的 induced 输出和一组 neutral 输出，两组只差“成果压力”那一段系统提示，然后在同一时段评分。主分析是逐题配对的越界率差（induced − neutral），用精确符号翻转检验，三个模型做 Holm 校正。2026-09-12 的 induced 运行只用作复现对照。

## 当前进度

| 模型 | induced（新） | neutral | 生成花费 |
|---|---:|---:|---:|
| Grok 4.6 | 240/240 | 240/240 | $4.72 |
| Gemini 3.7 Flash | 240/240 | 240/240 | $1.48 |
| Claude Opus 5 | 1/240 | 1/240 | $0.03 |

- Grok、Gemini 四组都是 240 个唯一样本、零错误、单一 run id，没有样本撞到 8,192 token 上限。
- [`run-record.json`](run-record.json) 中三个模型的配对检查全部通过：数据集与 2026-09-12 相同，新 induced 的提示与 2026-09-12 相同，同一模型的两组只差提示。
- 原始输出含完整模型正文，保存在 gitignored 的 `runs/` 中；`run-record.json` 记录了它们的 SHA-256。

## 为什么暂停

1. **Opus 不再接受 temperature。** 首次运行时 Opus 两组共 480 个请求全部返回 HTTP 404：OpenRouter 上 Opus 5 的所有端点都不再支持 `temperature`，而配置要求支持。两组 Opus 配置随后改为不设温度（使用服务商默认值），其他不变。这一偏差在重跑前提交（`1c7353f`）。
2. **额度用尽。** 重跑 Opus 时 OpenRouter 余额不足，请求返回 HTTP 402。runner 把 402 视为不可重试，续跑不会自动补发；Grok、Gemini 的 402 行先备份到 `runs/l1-paired-v2/failed-402/`，只删去这些失败请求行（没有模型输出），再逐组续跑补齐。维护者决定不再充值，因此 Opus 生成和全部评分推迟。

## 恢复时按这个顺序

1. **确认余额。** 预计 Opus 生成约 $23，评分约 $16–36。
2. **补齐 Opus。** runner 已改为续跑时自动重试 402 行（2026-10-09），无需再手动删行；先备份 Opus 两组的结果文件，再同时续跑两组：

   ```bash
   PYTHONPATH=src python -m sudo_bench.agentic.runner config.agentic-l1-opus-5-induced-r2.yaml --detector-policy l1_present_semantic_v2
   PYTHONPATH=src python -m sudo_bench.agentic.runner config.agentic-l1-opus-5-neutral.yaml --detector-policy l1_present_semantic_v2
   ```

3. **重写运行记录：** `PYTHONPATH=src python reports/l1-paired-avg16/pipeline.py record`，确认三个模型都是 240/240 且配对检查通过。
4. **评分：** `pipeline.py judge` 会在同一会话中评分全部六组；它在任何一组不完整或配对检查失败时拒绝运行。
5. **分析：** `pipeline.py analyze`，按协议中冻结的分析计划输出 `results.json`。

如果最终放弃 Opus，主分析只覆盖两个模型、Holm 只在两者之间校正，并且 `pipeline.py` 需要相应修改；这两点都必须先作为新的偏差记录并提交，再运行评分。

## 人工盲审子研究（已在分发前撤回）

曾为在不调用 API 的情况下推进，另行预注册过一项 240 条的双人盲审（Grok、Gemini 各条件 60 条，三轴标注），协议、抽样、空白表格以及仲裁、冻结和分析代码都在任何标签出现前提交。由于不再计划标注或花费，它在表格发出前被撤回：两份表格仍与提交的空白哈希一致，不存在任何标签。撤回记录见[协议](../../annotation/preregistration/l1-paired-human-240-v1.json)的 `withdrawal`；材料原样保留，如重启须作为新决定记录。
