# 架构与设计取舍

这份文档回答三个问题：**系统怎么分层**、**一次请求到底经过了什么**、**每个关键选择为什么是这样（以及被否掉的方案是什么）**。
快速上手看 [README](../README.md)；具体接口、配置项与使用限制也都在 README。

## 1. 分层与依赖方向

```
scripts/           入口：run_demo_mock / run_demo1_github / run_benchmark / inspect_run / mcp_server
   │  （唯一允许 import api 的地方）
api/               FastAPI 适配层：routes / ws / schemas / deps / app
   │              只做参数解析、状态码与序列化；业务逻辑一律下沉
infrastructure/    实现层：llm / tools / mcp / skills / memory(Redis·PG) / context / benchmark / trace
   │              可能依赖第三方（一律**惰性 import**），不 import api
core/              协议与纯逻辑：agent(ReAct) / llm / tool / skill / memory / context / trace / benchmark / mcp
                  零第三方依赖、不 import 上面两层 —— 所以它能在没有网络、没有数据库的环境里被完整测试
```

规则由测试守着，不靠自觉：`tests/unit/test_core_layering.py` 递归扫 `core/**` 与 `infrastructure/**` 的
import 语句，用**白名单**（`sys.stdlib_module_names`）判定，任何越界或新的第三方依赖都会让它红。
目前唯一的例外是 `core/context/manager.py` 里那个带 `try/except` 的可选 `tiktoken`（装了就用真分词器，
没装退化为字符估算）—— 它是显式列入白名单的，再冒出来第二个就会被抓住。

为什么要这么切：`core` 是"能在沙箱里被完整验证"的那一层，`infrastructure` 是"换个实现不影响上层"的那一层，
`api` 是"最薄"的那一层。**业务逻辑不写在路由里** —— 路由依赖 fastapi，一旦逻辑写进去，在没有 fastapi 的
环境里就完全无法验证（`infrastructure/*/service.py` 就是这么来的）。

## 2. 六条不变量

| # | 不变量 | 为什么 | 谁在守 |
|---|---|---|---|
| 1 | `core/**` 只用标准库，且不 import 上层 | 纯逻辑必须能在任何环境里被测试 | `test_core_layering.py` |
| 2 | **`None` ≠ `0`**：没测到的指标是 `null`，前端显示 `—` | "0 次压缩"和"没测压缩"是两件事，混淆会让报告说谎 | 指标层 `None` + 前端 `web/src/lib/format.ts` |
| 3 | 可选能力**绝不阻断启动**：坏配置只记错 + 降级 | 一个坏 trace 文件不该让整个服务起不来 | `api/app.py` 的 lifespan、各 `catalog.py`、`SqliteTraceStore` |
| 4 | 每个能力**只有一处装配** | 两处装配会慢慢漂移（历史上 memory、context 都栽过） | `infrastructure/*/catalog.py`、`scripts/mcp_server.py` |
| 5 | 工具**永不抛异常**：失败是可读的 `ToolResult(success=False)` | observation 交给模型自行纠正，而不是炸掉整轮任务 | 各工具实现 + `ReActLoop` 兜底 |
| 6 | 前端不引重型 UI 框架、不新增构建机制 | 展示层不该变成第二个项目 | `pnpm` + `tsc -b` + `vite build` |

第 3 条的落地方式：装配函数一律返回 `(对象, 错误列表)` 而不是抛；`lifespan` 把每一块都包在
`try/except` 里并把原因写进 `app.state.*_errors`；Trace 页与 Inspector 页把这些原因**显示出来** ——
"降级了但没人知道"比"挂了"更难查。

## 3. 一次请求的完整链路

```
POST /api/chat（或 WS /ws/agent/{session_id}）
  └─ api/deps.get_agent()                     装配：LLM provider / 工具 registry / 技能 router /
     │                                        记忆 / 上下文 / tracer（store 是进程单例）
     └─ ReActLoop.run(task, session_id)
        ① tracer.start_session(task)          带上 source（chat | benchmark）与 skills 快照
        ② 技能命中：SkillRouter.match()        纯关键词子串，命中数降序取 top_k
        ③ 可选 TaskPlanner                    默认关（多花一次 LLM 调用，只产出"参考计划"文本）
        ④ 可选记忆召回：MemoryManager.recall()  三层级联、带 session_id
        ⑤ ContextManager.build()              组装 system（含技能与记忆注入）+ 当前任务
        ⑥ 循环（每轮 = 一次 LLM 调用，`rounds` 记的就是它）
             llm.chat(messages, tools)         工具 schema 走 `tools` 参数；
                                               记忆**不是工具**，它以文本注入 system prompt
             thought → 记录（含这一轮的 token usage）
             无 tool_calls → 收尾
             有 tool_calls → 逐个执行 → 结果作为 observation 回灌
             超预算 → ContextManager.compact() 就地压缩（见 §4.3）
        ⑦ 兜底：撞 max_steps → 再问一次要"强制最终答案" + 告警
        ⑧ 全部工具都失败 → 结果带 warning（前端显著提示"答案没建立在真实工具结果上"）
        ⑨ 可选：把任务摘要写入记忆
        ⑩ tracer.end_session(result)          落进 TraceStore（失败只记原因，不影响任务结果）
```

两条对外事实：`POST /api/chat` 是一次性响应，`WS /ws/agent/{id}` 是同一条链路的**事件流**
（`skill_matched / step_start / thought / tool_call / tool_result / compaction / final_answer / done`），
前端 Chat 页靠后者做实时展示。

**两处同名但口径不同的 `steps`**（容易误读，特意写下来）：

| 名字 | 口径 | 出现在哪 |
|---|---|---|
| `AgentResult.steps` / `TaskVerdict.steps` | **工具调用数 + 1**（每个工具调用一条 `AgentStep`，最后一条是最终回答） | `/api/chat`、评测报告 |
| `TraceSession.steps` | **LLM 轮次**（一轮一条 `TraceStep`，一轮里的多个工具调用合并进同一条） | Trace 页的步骤树 |

真正的"轮次"是 `AgentResult.rounds` / `TaskVerdict.rounds`（评测报告里的"平均轮次"）。
只看 `steps` 会误判"离 max_steps 还有多远"——一轮里模型可以一次发多个 `tool_calls`。

## 4. 关键设计取舍

### 4.1 Trace：为什么是"两份视图 + 可选存储"

- **两份视图**：`events` 是逐事件的原始日志（WS 实时流、排查问题用），`steps` 是按轮合并的结构
  （UI 的步骤树用）。两者是同一批事实的两种形状，合并逻辑放在 `Tracer` 里而不是前端 —— 前端只负责画。
- **`TraceStore` 协议 + 内存环形默认 + 可选 SQLite**：默认零配置、零落盘（重启即丢），
  要跨重启就 `TRACE_STORE=sqlite`。协议只有四个方法（`save/get/list/events`），
  所以接 PG 只需要再加一个实现，上层一行不改。
- **被否掉的方案**：① 只放内存不留协议 —— Trace 页在重启后什么都看不到，且以后接 PG 要改调用方；
  ② 只支持 PG —— 本地跑一次 Demo 就得先起数据库，与"零依赖也能看效果"冲突；
  ③ 把 events 存两套（一张表 + 一份 JSON）—— 两份真相必然漂移，现在整份 `session.to_dict()`
  进 `session_json`，可查询列只是索引。
- **store 是进程单例、`ContextManager` 每次新建**：store 要跨会话累积历史，所以必须共享；
  上下文只属于一次会话，每次新建最省心（也不会串任务）。`api/deps.get_last_context_manager()`
  因此只记得"最近一次聊天会话"，这是它的**天花板**，写进了 README。

### 4.2 上下文计量：`used_tokens` 用真账本，`sections` 只是拆解

`snapshot()` 的 `used_tokens` 与 `ratio` 取自 `token_count()`（也就是 `should_compact()` 用的那个数），
`sections` 是分段估算。**两者不保证逐段相加等于总数** —— 分词器不可加，实测差过 1 个 token。
早期版本把逐段之和当总数，结果是 Inspector 显示的数字与"到底会不会触发压缩"对不上；现在以真账本为准。

### 4.3 压缩：阶梯与"`compaction=off` 只关摘要"

自动模式下（`should_compact()`：占用 > 可用预算 × 0.8）：

1. 起始档由 `decide_strategy(ratio)` 给出：`≥0.95` SUMMARIZE、`0.90~0.95` TRUNCATE、`<0.90` SQUEEZE；
2. 但 0.90~0.95 这一档**优先试摘要** —— 否则"摘要器接上了却永远不试"（等同于死代码）；
3. SQUEEZE 没压到阈值以下 → 有摘要器就 SUMMARIZE → 仍超 → TRUNCATE；
4. 摘要器缺失或摘要调用失败 → 降级为 TRUNCATE，并如实记 `degraded_from` + `degraded_reason`。

由此得到消融实验的口径：**`compaction=off` 只等于关闭 SUMMARIZE**，SQUEEZE/TRUNCATE 是超预算后的
无条件行为，三组都会做。第三组的自变量只有"要不要额外花一次 LLM 摘要调用"。

### 4.4 评测：为什么用"成对任务"而不是绝对名次

`+Memory` 组必须在**整轮共享会话**（`session_scope=run`）才测得出记忆效应，代价是组内靠后的任务受益于
靠前的任务 —— 顺序相关，**绝对名次不可比**。所以数据集是成对任务（`pair_id` + `pair_role`：
每对 first → followup，followup 文本包含 first 的关键词），结论只读 `followup` 行的对比。
另外 `+Memory` 组必须同时打开 `long_term`：`working`/`short_term` 的匹配方向是"整段 query 是条目内容的
子串"，长任务文本几乎命不中，只开 short_term 等于测空气。

### 4.5 打分口径：`steps` 之外为什么要加 `rounds`

真实全量里出现过"步数 31，而 `max_steps=15`"这种看似矛盾的读数 —— 因为 `steps` 是工具调用数 + 1，
一轮可以发多个调用。加了 `rounds`（真实 LLM 轮次）之后，"离上限还有多远"才读得出来。
两个数都保留：`steps` 是旧口径（报告可比性），`rounds` 是新增的事实。

### 4.6 MCP 两个方向

- **消费方**：`mcp_servers.json` 配 stdio server，启动时发现工具 → 包装成 `MCPToolAdapter` 注入
  **同一个** registry，名字带前缀（`filesystem__read_file`）避免撞名；坏配置只进 `app.state.mcp_errors`。
- **提供方（自研 server）**：把本项目的原生工具通过 stdio 暴露给别的客户端，协议核心在
  `core/mcp/server.py`（无 IO，纯 `handle`/`handle_line`），stdio 循环在 `infrastructure/mcp/server.py`。
  **不依赖官方 `mcp` SDK**；只做 `initialize / tools/list / tools/call` 与通知，协议版本 `2024-11-05`。
- **被否掉的方案**：HTTP/SSE 传输、鉴权、resources/prompts/sampling —— 目标是"给本机客户端用工具"，
  这些都不解决当前问题（YAGNI）。使用限制：**没有鉴权**，只在本机 / 受信环境跑，`--root` 限制可读目录。

### 4.7 搜索：为什么不做自动 fallback

`duckduckgo`（免 key）的 Instant Answer **不是通用搜索**：冷门主题会返回 `(no results)` 却标成功。
曾经真实全量因此每条研究类任务白烧到 `max_steps`。处置是两条：

1. 不做自动 fallback（否则"这条结论来自哪个搜索后端"就说不清，归因混乱）；
2. 真实评测**开跑前**校验配置：`WEB_SEARCH_PROVIDER=tavily` 没 key 直接拒绝启动，
   而不是让每条任务各失败一次。

### 4.8 前端：为什么只有静态断言

四个页面（Chat / Trace / Inspector / Benchmark）都用同一套 shadcn/ui + Tailwind v4 浅色主题。
`web/` 里没有测试框架，构建通过**证明不了**任何产品约定，所以 spec 里写的检查被落成了
`tests/unit/test_web_pages.py`：每个页面确实 import 了 `@/components/ui/*`、主题里没有 `.dark`、
`components.json` 的别名指向真实文件、缺失值统一走 `lib/format` 的 `—`。
**渲染效果仍需人眼验收** —— 这是本仓库明确不假装能自动化的部分。

## 5. 对外数据契约

对外形状一旦定下就是契约（前端、报告、脚本都按它写）：

```python
AgentResult(task, final_answer, steps[], total_tokens, total_latency_ms,
            trace_id, warning, skills_used[], rounds, max_steps)

BenchmarkReport(run_id, config{}, verdicts[], metrics{}, created_at)
  └─ BenchmarkMetrics: tasks_total / tasks_errored / success_rate / success_rate_measured /
       provider_errors / tool_selection_accuracy / tool_argument_accuracy / avg_steps / avg_rounds /
       avg_total_tokens / avg_latency_ms / compression_ratio / compression_by_strategy /
       compaction_events / compaction_events_by_strategy / summarizer_tokens / summarizer_ms /
       error_recovery_rate / judged_tasks / avg_judge_score        # 未测到的都是 None
  └─ TaskVerdict(..., rounds, max_steps, skipped)                  # skipped=从进度文件复用

TraceSession(trace_id, task, source, status, steps[], events[], final_answer,
             total_tokens, total_latency_ms, started_at, finished_at)   # 可 JSON 往返

MCP 帧：initialize / notifications/initialized（无响应）/ tools/list / tools/call
        → content:[{type:"text",text}] + isError；未知 method -32601，未知工具 -32602
```

## 6. 明确不做

MCP 的 HTTP/SSE 与鉴权、resources/prompts/sampling；trace 的 PG 三表（协议已留好）；实时 trace 推送与
前端图表库；深色模式与多主题；评测的统计显著性检验与并发；把逐条 token 归属拆到"每个工具调用"级别
（一轮的 usage 记在轮上）；自动搜索 fallback。

## 7. 怎么继续读

| 想知道 | 看 |
|---|---|
| 怎么跑起来、有哪些接口与配置 | [README](../README.md) |
| 端到端演示与观察点 | [demo1_github.md](demo1_github.md) |
| 每个能力的细节口径 | `README.md` 的对应章节 + `docs/` 与本文件 |
| 代码怎么分层、为什么不那样设计 | 就是本文档 |
