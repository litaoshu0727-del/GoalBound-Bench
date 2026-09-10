# OpenRouter 限流实测 + L1 MVP 时间重算（2026-09-09）

配置里 `requests_per_second: 0.25`（15 RPM）的注释写的是“new-account RPM limit”，但这个数
从未实测过，它只是上一轮 429 之后拍的一个保守值。本次先做限流探测，再用**实测 RPM 与实测延迟**
重算全量 L1 MVP 的墙钟时间。探测总花费 **$0.157**。

## 1. 声明式限额查不到，只能实测

`GET /api/v1/key` 对本账号不返回任何可用限额：

```json
{"limit": null, "limit_remaining": null, "is_free_tier": false,
 "rate_limit": {"requests": -1, "interval": "10s", "note": "This field is deprecated and safe to ignore."}}
```

真实限额只写在 429 的响应体里，随 `X-RateLimit-*` 一起返回。

## 2. 实测结果：每模型 20 RPM，固定 60 秒窗口

从静默窗口起并发打 30 条极小请求（`max_tokens: 16`），四个模型全部是**恰好 20 条成功、其余 429**：

| 模型 | 30 并发中成功 | 429 | `X-RateLimit-Limit` | 限流 key |
|---|---:|---:|---:|---|
| anthropic/claude-opus-5 | 20 | 10 | 20 | `new-account-rpm/anthropic/claude-opus-5-20260723` |
| google/gemini-3.7-flash | 21 | 9 | 20 | `new-account-rpm/google/gemini-3…` |
| x-ai/grok-4.6 | 20 | 10 | 20 | `new-account-rpm/x-ai/grok-4…` |
| openai/gpt-5.6-sol | 20 | 10 | 20 | `new-account-rpm/openai/gpt-5…` |

三个可直接读出的事实：

- **限额是按模型分桶的**，不是账号全局共享 —— key 里带模型名。因此不同模型可以**并行满速跑**，
  但同一模型的两个角色（Gemini 既是被测模型又是判官 B）**共用同一个桶**。
- **窗口是固定的整分钟**，不是滑动窗口：`X-RateLimit-Reset` 全部是 `1788934860000`，能被 60 整除。
- 这是 **new-account 档**。本账号累计消费仅 $0.169，随账号消费/账龄提升该档会上调；重跑
  `probe_ceiling.py` 即可复测，成本不到 1 美分。

429 是**立即返回**的（0.4–0.5 s），不排队。

### 20 RPM 可持续，非瞬时突发额度

按 3 s 间隔连续跑 70 s（≈24 条），四个模型 **0 次 429**：

| 模型 | 成功 | 429 | 实际达成 RPM |
|---|---:|---:|---:|
| openai/gpt-5.6-sol | 24 | 0 | 20.4 |
| google/gemini-3.7-flash | 24 | 0 | 20.5 |
| anthropic/claude-opus-5 | 24 | 0 | 19.9 |
| x-ai/grok-4.6 | 18 | 0 | 14.5 |

Grok 只到 14.5 RPM 不是被限流，是探测脚本单线程、它自身延迟超过了 3 s 的发包间隔。

## 3. 实测真实 L1 提示的延迟与单价

用 `questions.v3.agentic.jsonl` 里真实的 `prompt_l1` + 情境化 induced 系统提示，各跑 3 条：

| 模型 | max_tokens | 延迟 p50 | 延迟 max | 输出 tokens p50 | 单次成本 |
|---|---:|---:|---:|---:|---:|
| anthropic/claude-opus-5 | 2048 | 17.4 s | 19.1 s | 800 | $0.02104 |
| google/gemini-3.7-flash | 2048 | 2.3 s | 2.8 s | 466 | $0.00177 |
| x-ai/grok-4.6 | 2048 | 28.4 s | 32.9 s | 1590 | $0.00991 |
| openai/gpt-5.6-sol（判官档） | 512 | 2.6 s | 4.6 s | 109 | $0.00138 |

## 4. 用实测值重算时间

模型：一个阶段在一个模型上的吞吐取两者较小值

```
rpm = min(20 × 0.9 安全系数, 60 × concurrency / 延迟p50)
```

哪一项先卡住决定了怎么修：卡在限流只能等账号提档，卡在延迟则加并发就能解决。

判官调用量用**本仓库自己的实测值**：`runs/agentic-l1-smoke-matched-*/judged*.jsonl` 里
每样本平均 **2.07–2.20 次**判官调用（refusal 恒调用，engaged 后 success 恒调用、boundary 视
detector 是否 UNRESOLVED），取 2.1。

### 阶段一：L1 runner，induced，Avg@16（15 题 × 16 样本 = 240 次/模型）

| 模型 | 调用 | 有效 RPM | 墙钟 | 瓶颈 | 饱和所需并发 | 成本 |
|---|---:|---:|---:|---|---:|---:|
| anthropic/claude-opus-5 | 240 | 18.0 | **13 min** | 限流 | ≥6 | $5.05 |
| google/gemini-3.7-flash | 240 | 18.0 | **13 min** | 限流 | ≥1 | $0.42 |
| x-ai/grok-4.6 | 240 | 16.9 | **14 min** | 延迟/并发 | ≥9 | $2.38 |

三模型分属不同桶，可并行：**阶段墙钟 14 min**（串行 41 min），成本 ~$7.85。

### 阶段二：两个跨厂判官

| 判官 | 覆盖样本 | 调用 | 有效 RPM | 墙钟 | 瓶颈 | 成本 |
|---|---:|---:|---:|---:|---|---:|
| openai/gpt-5.6-sol（判官 A） | 720（全部 3 个模型） | 1512 | 18.0 | **84 min** | 限流 | ~$2.08 |
| google/gemini-3.7-flash（判官 B） | 480 | 1008 | 18.0 | **56 min** | 限流 | ~$1.78 |

两个判官不同厂 = 不同桶，可并行：**阶段墙钟 84 min**（串行 2.3 h），成本 ~$3.86。

### 合计

| | 墙钟 | 成本 |
|---|---:|---:|
| 全部按桶并行 | **1.6 h** | ~$11.7 |
| 全部串行 | 3.0 h | ~$11.7 |

判官阶段是主要开销（占并行总时长 98 min 中的 86%），因为它的调用量是 runner 的 3.5 倍而单条都很短——
**完全卡在 20 RPM 上，加并发无效**。

## 5. 由实测推出的三条结论（都会改变现在的跑法）

1. **`config.agentic-l1-opus-5-induced.yaml` 现在会被限流打穿。** 它是
   `requests_per_second: null` + `concurrency: 8`。对快模型这等于每分钟发出
   210 RPM（Gemini，延迟 2.3 s）/ 187 RPM（GPT-5.6 Sol），即 **~90% 的请求会吃 429**；
   而 `max_attempts: 4` + `backoff_initial 1 / max 30`（1→2→4 s）在一个 60 s 固定窗口里
   退避不完，大量样本会以错误告终。跑全量前必须设限速。
2. **判官配置的 0.25 rps 保守了 25%。** 实测上限 20 RPM，可提到 `0.3`（18 RPM，留 10% 余量），
   判官 A 从 101 min 降到 84 min。
3. **Grok 需要更高并发。** 28.4 s 的延迟下 `concurrency: 8` 只能达到 16.9 RPM，顶不满 18 RPM 的
   闸门；给它 `concurrency: 12`。Opus（17.4 s）需要 ≥6，现有的 8 够用。

已写入的配置值（2026-09-09 应用）：

| 文件 | `requests_per_second` | `concurrency` |
|---|---|---|
| `config.agentic-l1-opus-5-induced.yaml` | `null` → **`0.3`** | `8`（≥6 即可饱和，保持不变） |
| `config.agentic-l1-gemini-3.7-flash-induced.yaml`（新建） | **`0.3`** | **`2`** |
| `config.agentic-l1-grok-4.6-induced.yaml`（新建） | **`0.3`** | **`12`** |
| `config.agentic-l1-judge.yaml` | `0.25` → **`0.3`** | `1` → **`3`** |
| `config.agentic-l1-judge-B.yaml` | `0.25` → **`0.3`** | `1` → **`3`** |

Gemini 与 Grok 两个臂此前并不存在（原 opus 配置的注释是“复制本文件、换 model 和输出路径”），
本次按该说明建好，除 `model` / `output` / `manifest` / `concurrency` 外与 opus 臂逐字段相同
（`temperature: 1.0`、`max_tokens: 2048`、`samples_per_question: 16`、`timeout: 120`），
已用脚本断言三臂严格匹配。`concurrency` 只影响吞吐、不进入 `generation_config_sha256`，
因此按模型延迟分别取值不破坏同口径对照。

判官侧并发从 1 提到 3：0.3 rps 意味着 3.33 s 发一次，而 GPT-5.6 Sol 实测最慢一次是 4.6 s，
并发 1 时慢调用会让发包间隔空转，顶不满闸门。限速器仍是唯一闸门。

**已加固**：三个 runner 配置现与判官侧一致，使用 `max_attempts: 8`、
`backoff_initial 1 / backoff_max 90`。429 响应实测不带 `Retry-After`，因此较长退避窗口用于
跨过固定 60 秒限流窗口；18 rpm 的主动节流仍是首要防线。

## 6. 一个与限流无关、但被这次统计暴露出来的问题

判官 B 是 `google/gemini-3.7-flash`，而被测模型之一也是 `google/gemini-3.7-flash`，因此
`assert_cross_vendor` 会正确拒绝它评自己的输出。该问题已通过预注册的判官 C 选型解决：统一主判官对
为 A（`openai/gpt-5.6-sol`）+ C（`z-ai/glm-5.3-flash`），覆盖全部三个被测模型；B 继续对
非 Google 的两个模型作稳健性复核。C 是在三名合格候选完全并列后按预注册成本 tie-breaker 选出，
不能表述为“准确率最高”。详见 `annotation/preregistration/agentic-judge-c-selection-v1.json` 与
`annotation/gold/agentic-heldout-calibration-60-v1/judge-c-selection.json`。

## 7. 复现

```bash
export OPENROUTER_API_KEY=...
python3 reports/openrouter-rate-limit-probe/probe_rate_limits.py /tmp/rate-probe.json
python3 reports/openrouter-rate-limit-probe/probe_ceiling.py    /tmp/rate-ceiling.json
python3 reports/openrouter-rate-limit-probe/recompute_schedule.py /tmp/rate-probe.json /tmp/rate-ceiling.json
```

原始测量见 `measurement.json`（探测 + 确认 + 真实延迟三阶段）与 `ceiling.json`（30 并发天花板波），
重算输出见 `recomputed-schedule.txt`。

## 8. 效力边界

- 延迟每模型只测了 3 条，`p50` 精度有限；Grok 的 max 比 p50 高 16%，重负载下会更差。
- 判官单价用被测提示的实测单价近似（判官提示更长、输出更短），阶段成本是量级估计而非账单。
- 每样本 2.1 次判官调用来自 Haiku 4.5 的 15×1 smoke；拒绝率随模型变化，拒绝越多调用越少，
  所以判官阶段时长偏保守。
- 20 RPM 是 new-account 档的当下值，不是长期常数。
