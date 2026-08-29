# SuDo-Bench

🚀SuDo-Bench | 测试模型在真实世界取得大成果的能力🌍🏆
🧭 在复杂且高度不确定的现实环境中，将知识、判断与意图转化为可验证、可持续且具有规模效应的实际成果，是个体与组织实现长期发展的核心能力。现实世界中的重要问题通常具有目标模糊、资源有限、利益相关者众多以及反馈滞后等特征。因此，仅具备理论知识、专业技能或创新构想，并不足以保证预期价值的实现。真正产生影响还依赖于行动者识别关键问题、整合有限资源、协调多方主体，并根据环境反馈持续调整行动策略的能力。
🤖 当前，前沿模型在智能体相关任务中已展现出卓越能力。然而，真实世界环境具有高度复杂性与动态性，而现有模型的训练目标通常侧重于准确、稳健地完成预先定义的任务，这在一定程度上限制了其主动识别潜在机会、突破既定目标边界，并创造超出预期的重要成果的能力。
⚖️ SuDo-Bench 旨在评估模型在个人利益与伦理规范发生冲突时所表现出的行为倾向。具体而言，该基准通过构建具有现实利益诱因的复杂任务情境，考察模型是否会为了最大化自身收益，主动采取包括但不限于篡改材料、夸大个人贡献、隐瞒关键信息或通过欺骗手段争取第一作者身份等高效策略。
📊 Avg@16 的评测结果显示，不同模型在成果导向与伦理约束之间呈现出显著差异。其中，GPT-5.6 sol 在该指标上的表现最弱：即使面临取得重大成果的潜在机会，它也几乎不愿意通过突破道德与伦理边界来提升收益。相比之下，当被明确要求将取得重大成果作为唯一目标时，Gemini 3.7 Flash 更倾向于弱化甚至忽视伦理规范，并采取能够最大化结果的优质策略。🔍

## Leaderboard

![SuDo-Bench Leaderboard](assets/leaderboard.png)

## 快速开始

需要 Python 3.9+ 和 [uv](https://docs.astral.sh/uv/)。先安装依赖：

```bash
uv sync
```

在环境变量中设置 API Key：

```bash
export DEEPSEEK_API_KEY="your-api-key"
```

然后在仓库根目录运行：

```bash
./eval.sh
```

## 配置

所有运行参数都在根目录的 [config.yaml](config.yaml) 中：

```yaml
api_key: ${DEEPSEEK_API_KEY}
base_url: https://api.deepseek.com
model: deepseek-v4-flash

dataset: questions.jsonl
output: results.jsonl
timeout: 60
temperature: 1.0
require_parameters: false
max_tokens: 8192
concurrency: 64
case_sensitive: false
overwrite: true
```

- `api_key` 支持 `${ENV_NAME}` 环境变量展开；不鉴权的本地服务可设为 `null`。
- `base_url` 填接口根地址，程序会在末尾追加 `/chat/completions`。
- `temperature: null` 表示不传温度，使用模型默认值。
- `concurrency` 控制测评并发数量。
- `require_parameters` 主要用于 OpenRouter；设为 `true` 时只路由到支持请求参数的 Provider。
- `dataset` 和 `output` 的相对路径以配置文件所在目录为基准。
- `overwrite: false` 时，如果结果文件已存在，程序会拒绝覆盖。

接入其他 OpenAI 兼容接口时，只需修改 `api_key`、`base_url` 和 `model`。
