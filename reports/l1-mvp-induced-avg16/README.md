# L1 induced Avg@16 运行记录（2026-09-12）

**这不是结果，是运行记录。** 三个模型的 720 条真实产物已经生成并落盘，但**判官尚未运行**：
boundary 轴有 85–97% 的样本是 UNRESOLVED，success 轴全部 240×3 都是 UNRESOLVED。
**不得从本记录引用任何越界率或达成率。**

## 跑了什么

15 道核心题 × 16 样本 × 3 个模型 = **720 次调用**，情境化 induced 条件（`L1_INDUCED`），
三臂除模型外逐字段匹配（`temperature: 1.0`、`max_tokens: 8192`、`samples_per_question: 16`）。

## 执行质量

| 模型 | 样本 | 错误 | 重试 | 撞上限 | completion p50 / max | 花费 |
|---|---:|---:|---:|---:|---:|---:|
| anthropic/claude-opus-5 | 240/240 | 0 | 0 | 0 | 1777 / 4755 | $11.73 |
| x-ai/grok-4.6 | 240/240 | 0 | 0 | 0 | 1513 / 3833 | $2.35 |
| google/gemini-3.7-flash | 240/240 | 0 | 0 | 0 | 710 / 1184 | $0.67 |
| **合计** | **720** | **0** | **0** | **0** | | **$14.75** |

墙钟 14.5 分钟（三臂并行，各占独立限额桶）。实测预估为 $15.48，实际低 5%。

完整性检查全过：每臂 240 个唯一 `(id, sample_index)`、单一 `run_id`、请求模型与 `returned_model`
一致、无空输出。

## 限速验证

| | 值 |
|---|---|
| 节流 | 0.3 rps = 18 rpm（实测每模型上限 20 rpm） |
| 实际吞吐 | Opus 16.7 / Grok 16.9 / Gemini 17.9 rpm |
| 观测到的 429 | **0** |

720 次调用零错误零重试，说明节流全程没有触到限流天花板。Opus 需要 `concurrency: 16`——
按 8 的话这一臂会被延迟压在约 11 rpm。

## 上限选择得到验证

Opus 本次最长样本 **4755 tokens**，超过 15 条 cap-check 观测到的该臂最大值（3593）32%。

| 假如上限设成 | 会被截断 |
|---|---:|
| 2048（原值） | **134/720（18.6%）** |
| 4096（"覆盖观测最大值"） | 2/720 |
| **8192（实际）** | **0/720** |

240 次抽样的极值会超过 15 次抽样的极值，所以当时留 2.28 倍余量而不是贴着观测最大值设，
是必要的。详见 [`reports/l1-max-tokens-truncation/`](../l1-max-tokens-truncation/)。

## 本记录包含什么

- [`run-record.json`](run-record.json)——三臂的 run_id、完整运行签名（含
  `dataset_sha256` / `generation_config_sha256` / `condition_prompt_sha256`）、
  源文件 SHA-256、执行统计、临时 detector 计数。
- [`token-accounting.jsonl`](token-accounting.jsonl)——**720 行逐样本 token 账目**：
  prompt / completion / reasoning / 可见 token 估计、输出字符数、是否撞上限、尝试次数、
  detector 的 boundary 判定、单条成本。**不含模型正文。**

原始 `results.jsonl` 含完整模型输出，属于 `.gitignore` 所说的 ordinary runs，因此不提交；
其 SHA-256 记录在 `run-record.json` 里，可核验私有文件。

## 仍然开着的缺口

1. **refusal 轴从未进过任何判官校准集**（`calibration.py` 的 `AXES` 只有 boundary 和
   success），而它是每个样本的第一个判官调用且会短路另外两轴。判这批数据之前必须先决定
   如何处理这一点。
2. **`runner.py` 只给最终失败的行写 `error_type`**，重试成功的行不留原因。本次 0 重试，
   没有造成损失，但记录能力仍缺。

## 下一步

判官可以在这批已落盘的产物上反复重跑，**无需再次调用被测模型**。因此判官侧的方案调整
（包括 refusal 轴的处理）不会浪费这 $14.75。
