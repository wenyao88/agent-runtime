# Demo 1：GitHub 仓库分析 Agent

真实链路演示：让 Agent 用 4 个 GitHub 工具分析一个仓库，产出结构化报告。

```
ReAct Loop → Function Calling → Tool Registry → 4 个 GitHub 工具 → 结构化分析报告
```

| 能力 | 在 Demo 1 中如何体现 |
|---|---|
| ReAct 循环 | 先看仓库信息 → 再看目录 → 再读关键文件 → 最后产出报告 |
| Function Calling | 模型通过 JSON Schema 选择工具并给出参数（`repo` / `path` / `ref`） |
| Tool Registry | 8 个原生工具统一注册，模型只看到 `name` + `description` + `parameters` |
| 工具错误恢复 | 读不存在的文件、目录当成文件读、超限文件 → 都变成可读 observation，模型自行纠正 |
| Context 压缩 | 长任务触发 SQUEEZE / TRUNCATE |
| Trace | 按步记录 thought / tool_call / tool_result，带 trace_id（`compaction` 是**会话级**事件，记在 step 0，不属于某一步） |

## 1. 前置条件

```bash
cp .env.example .env
# 必填：LLM_API_KEY（硅基流动 / DeepSeek / Qwen / GLM 任一 OpenAI 兼容端点）
# 建议：GITHUB_TOKEN —— 不填也能跑（公开只读接口），但：
#   * 未鉴权时限流严格（60 次/小时），推荐填上；
#   * github_search_code **必须**有 token（该接口未鉴权必然 401，工具会直接返回可读错误）。
pip install -e ".[dev]"
```

## 2. 运行

```bash
python scripts/run_demo1_github.py --repo fastapi/fastapi
python scripts/run_demo1_github.py --repo pallets/flask --focus "错误处理与重试"
# 会话标识：短时记忆按会话隔离（不传则用 default，多会话会互相污染）
python scripts/run_demo1_github.py --repo fastapi/fastapi --session-id smoke-1
```

输出形如：

```
任务：分析 GitHub 仓库 fastapi/fastapi，给出一份结构化报告：...
会话：default

── Step 1 ──
💭 先获取仓库的基本信息。
🔧 github_get_repo{"repo": "fastapi/fastapi"}
📋 [成功 412ms] repo: fastapi/fastapi
description: FastAPI framework, high performance...
...

✨ 最终报告：
（项目用途 / 技术栈 / 目录结构要点 / 改进建议）
```

## 3. 建议观察的点

1. **模型何时选择哪个工具**：它是否会先用 `github_get_repo` 建立全局认识，再 `github_list_dir`，最后
   `github_read_file` 精读 —— 这正是 Tool Selection 能力的体现。
2. **错误是如何被吸收的**：故意让它读一个不存在的文件（或用 `--focus` 引导），可以看到失败结果被写回
   上下文后模型改变策略，而不是整个任务崩掉。
3. **步数、轮次与 token**：结尾会打印**步数 / 轮次 / token / 耗时 / trace_id**。
   两个数别混：步数 = 工具调用数 + 1，轮次 = 真实 LLM 调用次数（一轮里模型可以一次发多个工具调用）。
4. **全工具失败时不可信**：如果所有工具调用都失败，收尾摘要会打印 `⚠` 告警（见根 README 的
   「输出可信度」）—— 此时报告内容没有真实工具结果支撑。

**小模型注意事项**：8B 级别的模型即使有明确指令也更易跑偏。演示建议用 DeepSeek-V3 / Qwen2.5-72B 级别的
模型；若用 8B，请看 `warning` 字段，并核对报告里的每条结论是否都能对应到某一步的工具返回。

## 4. 限制

| 限制 | 说明 |
|---|---|
| `github_search_code` 需要 token | 未配置 token 时工具**不发起请求**，直接返回可读错误（该接口未鉴权必然 401） |
| 配了 token 就是鉴权请求 | 填了 `GITHUB_TOKEN` 时会带 `Authorization` 头（未填则走公开只读接口，限流更严）；**私有仓库的完整流程未验证** |
| 远程 PDF 不支持 | `pdf_read` 目前只读工作区内的本地文件 |
| 非 UTF-8 文本 | 读取时按 UTF-8 解码，无法解码的字节以替换字符呈现，不报错 |

## 5. 无 API Key 也能看的效果

```bash
python scripts/run_demo_mock.py
```

用脚本化假模型驱动同一条 ReAct 链路（思考 → 调工具 → 观察 → 纠错 → 最终答案），不需要任何 key，
适合快速确认代码跑得通。

## 6. 配合记忆层（默认全关）

Demo 1 的任务结束会把 `task/answer` 摘要写入已启用的记忆层。打开开关后可用会话标识隔离：

```bash
# .env 里打开（默认全关：没起 DB/Redis 也不影响运行）
#   MEMORY_SHORT_TERM_ENABLED=true
#   MEMORY_LONG_TERM_ENABLED=true    # 需要 EMBEDDING_API_KEY + alembic upgrade head

python scripts/run_demo1_github.py --repo fastapi/fastapi --session-id smoke-1   # 跑两次
curl "http://127.0.0.1:8000/api/memories?layer=short_term&session_id=smoke-1"    # 应看到 source=short_term
curl "http://127.0.0.1:8000/api/memories?layer=long_term"                        # 不带 query：按时间取最近
curl -X DELETE "http://127.0.0.1:8000/api/memories?session_id=smoke-1"           # 清该会话的短时记忆；
                                                                                 # 长期记忆是**全表清空**（PG 层不接受 session_id）
```

装配失败（例如开了长期记忆却没配 `EMBEDDING_API_KEY`）会打印 `⚠ 记忆层问题：…`，不会静默降级。
