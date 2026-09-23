# Demo 1：GitHub Repository Analysis Agent

> 本文档说明 Demo 1 验证了什么、怎么跑、以及当前**尚未验证**的部分。

## 1. 这个 Demo 要证明什么

它验证 Phase 2 的能力链在真实仓库上是否闭环：

```
ReAct Loop → Function Calling → Tool Registry → 4 个 GitHub 工具 → 结构化分析报告
```

具体能力点：

| 能力 | 在 Demo 1 中如何体现 |
|---|---|
| ReAct 循环 | 先看仓库信息 → 再看目录 → 再读关键文件 → 最后产出报告 |
| Function Calling | 模型通过 JSON Schema 选择工具并给出参数（`repo` / `path` / `ref`） |
| Tool Registry | 8 个原生工具统一注册，模型只看到 `name` + `description` + `parameters` |
| **工具错误恢复** | 读不存在的文件、目录当成文件读、超限文件 → 都变成可读 observation，模型自行纠正 |
| Context 压缩 | 长任务触发 SQUEEZE / TRUNCATE，Trace 里能看到压缩事件 |
| Trace | 每步记录 thought / tool_call / tool_result / compaction，带 trace_id |

## 2. 前置条件

```bash
cp .env.example .env
# 必填：LLM_API_KEY（硅基流动 / DeepSeek / Qwen / GLM 任一 OpenAI 兼容端点）
# 建议：GITHUB_TOKEN —— 不填也能跑（公开只读接口），但：
#   * 未鉴权时限流严格（60 次/小时），推荐填上；
#   * github_search_code **必须**有 token（该接口未鉴权必然 401，工具会直接返回可读错误）。
pip install -e ".[dev]"
```

## 3. 运行

```bash
python scripts/run_demo1_github.py --repo fastapi/fastapi
python scripts/run_demo1_github.py --repo pallets/flask --focus "错误处理与重试"
# 会话标识：短时记忆按会话隔离（不传则用 default，多会话会互相污染）
python scripts/run_demo1_github.py --repo fastapi/fastapi --session-id smoke-1
```

输出形如：

```
任务：分析 GitHub 仓库 fastapi/fastapi，给出一份结构化报告：...

── Step 1 ──
💭 先获取仓库的基本信息。
🔧 github_get_repo{"repo": "fastapi/fastapi"}
📋 [成功 412ms] repo: fastapi/fastapi
description: FastAPI framework, high performance...
...

✨ 最终报告：
（项目用途 / 技术栈 / 目录结构要点 / 改进建议）
```

## 4. 建议观察的点（面试时可讲）

1. **模型何时选择哪个工具**：它是否会先用 `github_get_repo` 建立全局认识，再 `github_list_dir`，最后 `github_read_file` 精读——这正是 Tool Selection 能力的体现。
2. **错误是如何被吸收的**：故意让它读一个不存在的文件（或用 `--focus` 引导），可以看到失败结果被写回上下文后模型改变策略，而不是整个任务崩掉。
3. **token 与步数**：结尾会打印步数 / token / 耗时 / trace_id，这些正是 Phase 6 Benchmark 要批量统计的量。

## 5. 当前限制（诚实清单）

| 限制 | 说明 |
|---|---|
| 真实 LLM 链路 | ✅ **已在用户本机验证**：四个 GitHub 工具真实返回、301 修复生效、全工具失败时模型如实报告；开发沙箱仍只能验证到"缺依赖 → 可读错误"这一层 |
| `github_search_code` 需要 token | 未配置 token 时工具**不发起请求**直接返回可读错误（该接口未鉴权必然 401） |
| 只支持公开仓库 | 未实现鉴权私有仓库的完整流程 |
| 远程 PDF 不支持 | `pdf_read` 目前只读工作区内的本地文件（远程 PDF 需要二进制响应支持） |
| 无 UI 流式对话的持久化 | Trace 目前是内存态，PostgreSQL 持久化在 Phase 4 |

## 6. 无 API Key 也能看的效果

```bash
python scripts/run_demo_mock.py
```

用脚本化假模型驱动同一条 ReAct 链路（思考 → 调工具 → 观察 → 纠错 → 最终答案），不需要任何 key，适合快速确认代码跑得通。

## 7. 本机实测问题与修复（2026-09-21）

首次在本机跑 Demo 1 时暴露两个问题，均已在代码中修复并被测试钉住：

| 问题 | 现象 | 根因 | 修复 |
|---|---|---|---|
| GitHub 工具全部 301 | 四个 `github_*` 都返回 `HTTP 301` | httpx 默认**不跟随重定向**；GitHub 对 http→https、或改名/迁移过的仓库回 301 | `AsyncClient(follow_redirects=True)`；`test_default_client_follows_redirects` 用桩 httpx 模块锁住该构造契约 |
| 模型编造报告 | 工具全部失败后仍输出完整分析 | System Prompt 里没有任何约束；且结果中**不存在**"本轮不可信"的信号 | Prompt 硬规则 + **全工具失败告警**（`warning` + `final_answer` 事件）+ 低 temperature |

关于第二个问题，有一个值得记住的细节：修复前新写的测试 `test_tool_failure_evidence_reaches_the_model` **一次就通过**了——
说明失败原因本来就进了上下文。模型不是"不知道失败"，而是"知道了仍然编造"。因此只补证据链没用，必须同时
（a）在指令里明确禁止，并（b）把"全工具失败"提升为用户可见的告警。

> 因为这条规则，`test_bad_json_args_recovery` 里旧的 `assert result.warning is None` 已**有意更新**为断言告警存在：
> 该场景唯一的工具调用失败了，答案确实没有任何成功工具结果支撑。

**小模型注意事项**：Qwen3-8B 这类小模型即使有明确指令也更易跑偏。演示时建议用 DeepSeek-V3 / Qwen2.5-72B 级别的模型；
若坚持用 8B，请看 `warning` 字段，并注意报告里的每条结论是否都能对应到 Step 中的工具返回。

## 8. 与记忆层的配合（Phase 4，默认全关）

Demo 1 的任务结束会把 `task/answer` 摘要写入已启用的记忆层。打开开关后可用会话标识隔离：

```bash
# .env 里打开（默认全关：没起 DB/Redis 也不影响运行）
#   MEMORY_SHORT_TERM_ENABLED=true
#   MEMORY_LONG_TERM_ENABLED=true    # 需要 EMBEDDING_API_KEY + alembic upgrade head

python scripts/run_demo1_github.py --repo fastapi/fastapi --session-id smoke-1   # 跑两次
curl "http://127.0.0.1:8000/api/memories?layer=short_term&session_id=smoke-1"    # 应看到 source=short_term
curl "http://127.0.0.1:8000/api/memories?layer=long_term"                        # 不带 query：按时间取最近
curl -X DELETE "http://127.0.0.1:8000/api/memories?session_id=smoke-1"           # 只清该会话
```

**本机已验证**：同会话两次任务 → `short_term count: 2`；`DELETE` 后 `count: 0`；长期记忆写入 + 带 query 的向量检索命中；
不带 query 时按时间倒序返回最近 N 条。**未验证**：`docker compose` 全链路（本机拉不下基础镜像）。
