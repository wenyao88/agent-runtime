# Agent Runtime — 技术研究与研发 Agent 运行框架

自研单 Agent Runtime，覆盖 **ReAct Loop / Function Calling / Tool Registry / MCP / Skills / 三层 Memory /
Context Manager / Context Compaction / Agent Trace / Benchmark**，并以两个真实场景验证：GitHub 仓库分析、技术调研报告。

```
ReAct → Function Calling → Tool Registry → MCP → Skills
→ Memory (Working / Short-term / Long-term) → Context Manager
→ Context Compaction (Squeeze / Summarize / Truncate) → Trace → Benchmark
```

## 架构

| 层 | 目录 | 职责 |
|----|------|------|
| React Web UI | `web/` | Chat / Trace / Inspector 页面 |
| FastAPI | `src/agent_runtime/api/` | REST + WebSocket，依赖注入 |
| Core | `src/agent_runtime/core/` | 纯接口 + 纯逻辑，零 infrastructure 依赖 |
| Infrastructure | `src/agent_runtime/infrastructure/` | LLM Provider / MCP / DB / 原生工具 |

分层规则：`core/` 只依赖标准库（协议 + 纯逻辑，可脱离第三方包测试）；`infrastructure/` 放实现，第三方库
**惰性 import**；`api/` 只做薄适配。任何可选能力（MCP / 记忆层 / 技能）缺失或损坏都**只记录错误，不阻断启动与任务**。

## 本地跑起来

### 1. 环境与依赖

Python 3.11+（Docker 镜像用 3.12）；Node 18+（只跑前端时需要）。

```bash
pip install -e ".[dev]"
cp .env.example .env          # Windows: copy .env.example .env
# 编辑 .env，填入 LLM_API_KEY（硅基流动 / DeepSeek / Qwen / GLM 任一 OpenAI 兼容端点）
```

### 2. 零依赖先看效果（不用 API Key、不用起服务）

```bash
python scripts/run_demo_mock.py
```

用脚本化 Mock 模型驱动一次完整 ReAct 闭环：思考 → 调用 `read_file` → 观察结果 → 工具报错被模型自行纠正
→ 输出最终答案，并打印步数 / token / trace id。

### 3. 起后端

```bash
uvicorn agent_runtime.api.app:app --port 8000

curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d "{\"task\": \"读取 README.md 并总结\"}"
```

### 4. 起前端（可选）

```bash
cd web && pnpm install && pnpm run dev   # http://localhost:5173
```

Chat 页通过 `ws://localhost:8000/ws/agent/{session_id}` 接收 `skill_matched / step_start / thought /
tool_call / tool_result / compaction / final_answer / done` 事件流，实时折叠展示执行过程。

四个页面：**Chat**（对话与实时步骤）/** Trace**（历史轨迹与步骤树）/** Inspector**（上下文·记忆·工具·技能）/
**Benchmark**（评测报告与消融对比）。UI 用 shadcn/ui + Tailwind v4（**浅色单主题**，不引重型框架 ——
没有 MUI/AntD 这类库）。组件依赖的是新版 registry 的 `radix-ui` 合集包，产物约 611 kB（gzip 195 kB）；
要更小就把 `radix-ui` 换成按需的 `@radix-ui/react-*`，本阶段没做。

### 5. 用 Docker Compose 起全套（可选）

```bash
docker compose up              # api + postgres(pgvector) + redis
# 或只用容器起依赖、后端仍在本机跑：
docker compose up -d postgres redis
alembic upgrade head           # 建表（只有长期记忆需要）
```

### 6. 跑测试

测试文件是**双模式**的：装了 pytest 可以 `pytest tests/`，没装也能直接 `python` 跑单个文件。

```bash
# Windows：全部跑一遍（无需 pytest）
Get-ChildItem tests -Recurse -Filter 'test_*.py' | ForEach-Object { python $_.FullName }

# 单个文件
python tests/unit/test_react.py
python tests/unit/test_memory_manager.py
```

## HTTP / WebSocket 接口

| 接口 | 说明 |
|---|---|
| `POST /api/chat` | 跑一轮任务，返回最终答案 / 步数 / token / trace id / 告警 |
| `WS /ws/agent/{session_id}` | 同一条链路的事件流（前端实时展示用） |
| `GET /api/tools` | 全部工具（原生 + MCP）的统一形态 |
| `GET /api/skills` | 当前已加载的技能 |
| `GET /api/memories` | 三层记忆的开关状态、召回结果、装配与运行期错误 |
| `DELETE /api/memories` | 清空记忆（可按 `session_id`） |
| `GET /api/benchmarks` | Benchmark 历史报告小结 |
| `GET /api/benchmarks/{run_id}` | 一份完整报告（不存在 → 404） |
| `POST /api/benchmarks/run` | 触发一次评测（后台跑，立即返回 `run_id`） |
| `GET /api/traces` | 历史 trace 小结（最新在前） |
| `GET /api/traces/{trace_id}` | 单条 trace 详情（含步骤树；不存在 → 404 + 可读原因） |
| `GET /api/traces/{trace_id}/events` | 单条 trace 的原始事件日志 |
| `GET /api/context` | 当前上下文快照（分段 token / 预算 / 最近一次压缩） |

## Trace 与 Inspector：运行过程看得见

每次会话在结束时会落成**两条视图**：按步合并的 `steps` 与逐事件的 `events`。前端两个页面把它们显示出来
（shadcn/ui + Tailwind v4，浅色单主题，四个页面同一套语言）：

* **Trace 页**：左列会话列表（来源/状态徽标 + 来源过滤），右侧步骤树（`Collapsible`：思考 / 工具参数 /
  工具结果 / 每步 token 与延迟）+ 压缩与告警摘要 + 最终回答。一步 = 一轮 LLM 调用，所以"每步 token"
  就是那一轮的 `usage`（多工具并行调用属于同一轮，token 不按工具拆）；
* **Inspector 页**：四个 Tab —— **上下文**（分段 token 条 + 压缩阈值线 + 最近一次压缩的省/丢/摘要成本）、
  **记忆**（三层开关与最近条目）、**工具**（目录 + 参数 schema 展开）、**技能**（触发词与依赖工具）。

口径与天花板：

* `source` 区分 `chat` / `benchmark` / `demo` —— **评测轨迹不会混进聊天列表**（前端按来源过滤才有意义）；
* `/api/context` 反映**最近一次聊天会话**的上下文。拿不到时 `available: false` + 可读原因，HTTP 仍 **200**
  （前端直接展示原因，不用 500 装死）；并发多会话时看到的是最后那一次；评测 agent 的上下文不进这里；
* trace **默认内存环形**（默认 50 条，满了淘汰最旧，重启即丢）；要跨重启就把 `TRACE_STORE=sqlite`
  （`TRACE_DB_PATH` 默认 `trace.db`）—— 单进程、无并发写保证。**坏存储不阻断启动**：路径不可用 → 直接换成内存
  store；文件能打开但不是数据库（垃圾文件）→ 仍用 SQLite 但读写降级为空。两条路都把原因记进
  `errors` 并显示在 Trace 页顶部（"可用但读空"和"根本没落盘"不是一回事，所以分得开）；
* 前端**进页面时拉一次**，不做实时推送；trace 的 PG 三表未建（协议已留好，接上即可）；
* **"没测到"和"真的是 0"**：`TokenUsage` 这个类型全仓用 0 表示"provider 没报 usage"，
  前端不区分（页面照实显示 0）—— 这是既有口径，不是本页的取舍；
* 前端只做了**静态**断言（页面确实用了 shadcn 组件、主题只有 light 变量、`components.json` 的别名指向真实文件，
  见 `tests/unit/test_web_pages.py`）+ `tsc -b` + `vite build`；**渲染效果仍需人眼验收**。

## 内置工具（8 个原生工具 + 任意 MCP 工具）

| 工具 | 说明 | 需要外部凭据 |
|---|---|---|
| `read_file` | 读取工作区内文本文件（路径逃逸一律拒绝） | — |
| `github_get_repo` | 仓库信息：定位、主语言、star、默认分支 | 可选（无 token 限流更严） |
| `github_list_dir` | 列出仓库目录 | 可选 |
| `github_read_file` | 读取仓库中单个文本文件 | 可选 |
| `github_search_code` | 搜索代码 | **必须** `GITHUB_TOKEN`（否则直接返回可读错误，不发请求） |
| `web_search` | 联网搜索；默认 DuckDuckGo（免 key），可切 Tavily | 可选 |
| `web_scrape` | 抓取网页并提取正文（去脚本/样式） | — |
| `pdf_read` | 读取工作区内 PDF 文本，支持页范围 `1-5` | 需 `pypdf` |

工具契约（含 MCP 工具）：`execute()` **永不抛异常**，失败一律返回 `ToolResult(success=False, text="Error: …")`
作为 observation，由模型自行纠正。

## MCP：消费外部 MCP Server

把 `mcp_servers.example.json` 复制为 `mcp_servers.json` 并启用需要的 server：

```json
[
  { "name": "filesystem", "transport": "stdio", "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "."], "enabled": true }
]
```

启动时（FastAPI lifespan）连接并发现工具，包装成 `MCPToolAdapter` 注入**同一个** registry，
工具名带 server 前缀（`filesystem__read_file`）避免与原生工具撞名。配置文件缺失/损坏、或某个 server
起不来，只在 `app.state.mcp_errors` 里记录，**绝不阻断启动**。

## MCP Server（自研）：把本项目的工具给别人用

反向也做了：本项目**自己就是一个 MCP Server**，把 8 个原生工具通过 **stdio** 暴露给任意 MCP 客户端。
协议实现是自研的（`core/mcp/server.py` 的协议核心 + `infrastructure/mcp/server.py` 的 stdio 循环），
**不依赖官方 `mcp` SDK**，也没有第三方依赖：

```bash
python scripts/mcp_server.py                        # 项目根为工作区
python scripts/mcp_server.py --root D:/work/repo --name my-tools
```

| 请求 | 响应 |
|---|---|
| `initialize` | `protocolVersion` / `capabilities.tools` / `serverInfo` |
| `notifications/initialized` | **无响应**（通知不回） |
| `tools/list` | `{tools:[{name, description, inputSchema}]}` |
| `tools/call` | `{content:[{type:"text",text}], isError}`；工具失败 → `isError: true` + 错误文本（**异常绝不抛穿 stdio 循环**） |
| 未知 method | JSON-RPC `-32601` |
| 未知工具 | JSON-RPC `-32602` |

接到 Claude Desktop（`claude_desktop_config.json`）：

```json
{ "mcpServers": { "agent-runtime": {
  "command": "python",
  "args": ["D:/deepseek_harness/first/scripts/mcp_server.py", "--root", "D:/deepseek_harness/first"] } } }
```

天花板：只有 **stdio + tools**（没有 HTTP/SSE、没有 resources/prompts/sampling）；协议版本
`2024-11-05`；**没有鉴权**，只在本机 / 受信环境跑；`read_file` / `pdf_read` 用 `--root` 限制在给定目录内。

自研实现的正确性有两层证据：

1. **进程内闭环**（`tests/unit/test_mcp_roundtrip.py`）：把既有 `MCPClient` 与自研 server 用内存管道对接，
   8/8 原生工具全部发现，并真的 `read_file` 读到 README 内容；
2. **真实子进程 stdio**：`python -X utf8 scripts/mcp_server.py < transcript.jsonl > out.jsonl`（stdin 用
   **文件重定向**，不是管道 —— 沙箱拒绝带管道的子进程）。实测 exit 0、stderr 为空、响应 id 全对应、
   `tools/list` 8 个工具、`read_file` 读回 4015 字符、未知工具 `-32602`、坏 JSON 行**不产生**响应。

**未验证**：Claude Desktop 真机接入（要装那个客户端，也只能在图形界面里点）。

天花板（写下来免得被当成"没实现"）：**合法 JSON 但不是对象**（`[1,2]` / `"x"` / `42`）的行不产生响应
（JSON-RPC 严格说该回 `-32600`）—— MCP 不用 batch，客户端不会因此永久等待；这条已由测试钉成**有意行为**。

## Skills：领域 SOP（Markdown 定义）

技能 = 一段**领域操作流程**（SOP）。启动时从 `skills/*.md` 加载，按任务关键词命中后，把正文**追加**进
System Prompt。内置两个：`skills/github_analysis.md`（GitHub 仓库分析）、`skills/tech_research.md`（技术调研）。

新增一个技能：在 `skills/` 下建一个 `.md`，front-matter + 正文即可（**不需要改代码**）：

```markdown
---
name: my_skill
description: 一句话说明这个 SOP 干什么
version: "1.0"
triggers: [关键词A, 关键词B, english keyword]
required_tools: [read_file, web_search]
tags: [code]
---

# 正文就是注入 System Prompt 的技能指导
1. 第一步做什么……
```

支持**有限**的 front-matter 子集：标量、`[a, b]`、`- item` 列表；嵌套映射、锚点、多行字符串**不支持**
（遇到就明确报错并跳过该文件）。`name` / `description` 必填，未知键忽略。

命中规则：按命中的 trigger **个数**降序取前 `AGENT_SKILL_TOP_K`（默认 1）个，纯关键词子串匹配（大小写无关）；
**无命中就不注入**。技能文本只能**追加**在硬规则之后 —— 技能是"内容"，防幻觉规则是"底线"。
可用 `AgentResult.skills_used`、`skill_matched` 事件、`GET /api/skills` 看到本次用了哪个技能。

## Memory：三层记忆（默认全关）

| 层 | 存储 | 范围 | 检索方式 |
|---|---|---|---|
| Working | 进程内 | 进程生命周期 | 子串匹配 |
| Short-term | Redis List + TTL | 单会话（`session_id`） | 最近 N 条 + 子串过滤 |
| Long-term | PostgreSQL + pgvector | 跨会话 | 带 `query` → 余弦距离语义检索；不带 `query` → 按时间倒序取最近 N 条 |

召回顺序 `working → short_term → long_term`，够 `MEMORY_RECALL_TOP_K` 条即**短路**（不查更慢的持久层）。
注入时带**来源与日期**标注、截断带省略号，上限 `MEMORY_INJECT_MAX_CHARS`（默认 500）：

```
Relevant Memories:
- [long_term · 2026-09-21] 上次调研结论：pgvector 的运维成本最低…
- [short_term] 本会话前面确认过 Qdrant 的许可协议…
```

开启步骤（默认全关：没起 DB/Redis 也不影响启动与任务）：

```bash
# .env 里打开需要的层
#   MEMORY_SHORT_TERM_ENABLED=true
#   MEMORY_LONG_TERM_ENABLED=true      # 还需要 EMBEDDING_API_KEY
#   MEMORY_CONSOLIDATE_ENABLED=true    # 任务结束用 JUDGE_LLM 压成一条长期记忆

alembic upgrade head
```

任一层不可用只记错误、不阻断任务：装配期错误进 `app.state.memory_errors`（`GET /api/memories` 的
`settings.errors` 也能看到），运行期错误进 `MemoryManager.errors`。

## 上下文压缩（超预算自动触发）

每次工具结果之后检查上下文占用（触发阈值 = `AGENT_CONTEXT_COMPACTION_THRESHOLD` × 可用预算），按顺序**逐级**处理：

| 顺序 | 策略 | 做什么 | 代价 |
|---|---|---|---|
| 1 | SQUEEZE | 就地压缩过长的工具结果（头 200 + 标记 + 尾 100），协议字段不动 | 免费，信息损失小 |
| 2 | SUMMARIZE | 用 JUDGE_LLM 把"保留窗口之外的早期对话"压成一条带标记的摘要 | 一次 LLM 调用 |
| 3 | TRUNCATE | 直接丢弃最旧消息（保留 system + 当前任务 + 最近 6 条） | 免费，但丢信息 |

顺序的理由：**必须在下手丢消息之前决定要不要摘要** —— TRUNCATE 一跑，要被摘要的素材就没了。
自动模式下**永不**直接丢消息：占用 ≥ 95% 就直接摘要，否则一律先 SQUEEZE、再摘要、最后才 TRUNCATE。

```bash
# .env：SUMMARIZE 默认关（需要 JUDGE_LLM_API_KEY；关闭时超预算直接丢弃最旧消息）
AGENT_COMPACTION_SUMMARIZE_ENABLED=false
AGENT_CONTEXT_COMPACTION_THRESHOLD=0.8
```

摘要消息形如 `[对话摘要 · 已压缩 N 条早期消息]`，插在当前任务之后、最近消息之前；摘要正文有 2000 字符上限
（超出带标记截断）。摘要器缺省、调用失败、返回空或返回非字符串都会**如实降级为 TRUNCATE**，并在 `compaction`
事件里带上 `degraded_from` 与原因；什么也没改变的压缩会带 `noop: true`（不伪装成一次成功的压缩）。

天花板：

- 摘要那次 LLM 调用的 token / 时延会随 `compaction` 事件上报并汇总进报告（见下节"摘要 token / 摘要耗时"）；
  但**只有 provider 报了 `usage` 才有 token**，没报时记 0（不估算）。单次 LLM 摘要**不保证无损**；多轮压缩会逐层叠加摘要。
- **消息太少时没有可摘要的素材**：只保留 `system + 当前任务 + 最近 6 条`，所以不足 7 条时 SUMMARIZE 无事可做 ——
  短任务只会用到 SQUEEZE / TRUNCATE。
- **保留窗口本身超预算时压不下去**：此时每次检查都报 `noop`，上下文会持续高于阈值
  （不丢当前任务与最近消息是硬约束）。

## Benchmark：跑任务集，产出可复现的数字

```bash
python scripts/run_benchmark.py --provider mock            # 离线跑全量 100 条（不需要 key / 网络）
python scripts/run_benchmark.py --provider real --limit 3  # 真实链路抽样（会花钱）
python scripts/run_benchmark.py --provider real --judge 3  # 另抽 3 条做 LLM 裁判
python scripts/inspect_run.py                              # 只读巡检：概览 + 工具失败分布
python scripts/inspect_run.py --run <run_id>               # 某一轮：逐任务失败原因（含轮次 x/y）
python scripts/inspect_run.py --json                       # 机器可读
```

**搜索 provider（研究类任务的关键路径）**：

| provider | 配置 | 说明 |
|---|---|---|
| `duckduckgo`（默认） | 无需 key | Instant Answer 接口：**不是通用网页搜索**，只覆盖实体/主题摘要。冷门主题会返回 `(no results) 无结果`（**success=True**），Agent 只好反复换词重试 |
| `tavily` | `WEB_SEARCH_PROVIDER=tavily` + `WEB_SEARCH_API_KEY=tvly-…` | 通用网页搜索；`search_depth=basic`（1 credit/次），`max_results` 取工具参数（默认 5） |

**开跑前会拦住的致命配置**：`WEB_SEARCH_PROVIDER=tavily` 却没填 key（或 provider 拼错）时，真实评测**直接拒绝开跑**
（exit 2，CLI 打印原因；API 返回 `started: false` + errors）—— 否则每条研究类任务都会白烧到 `max_steps` 才失败。
`--provider mock` 不受影响（夹具不调用工具）。**没有自动 fallback**（避免同一份报告里两组用了不同 provider 的归因混乱）。

**抓取（`web_scrape`）**：默认带 `Accept` / `Accept-Language` 与可联系的真实 UA（裸 UA 会被很多站点直接 403）；
对 **429/5xx**（对方明确说稍后再试）自动重试**一次**并在错误文本里标注"已重试 N 次"；**403/404 不重试**
（同样的请求再问一遍不会变），超时/连接错误也不在工具内重试（外层 `AGENT_TOOL_TIMEOUT_SECONDS` 只有 30s，
工具内 20s 超时再重试会被外层掐掉，连可读错误都拿不到）。

任务集在 `benchmarks/tasks.json`（100 条：GitHub 分析 50 + 技术调研 50，其中每类 10 对"成对相关任务"）；
报告写到 `benchmark_runs/`（已 gitignore）。
也可走 API（`GET /api/benchmarks`、`GET /api/benchmarks/{run_id}`、`POST /api/benchmarks/run`），前端 **Benchmark** 页
可看历史运行与逐任务明细。

### 8 个指标与口径

| 指标 | 口径 |
|---|---|
| 出错任务数 | 没能产出结果的任务数（异常 / 崩溃）；**均值的分母不含它们**（否则等于拿 0 冒充测量值） |
| 成功率 | 必需工具全覆盖 **且** 期望关键词全命中 **且** 无告警（全工具失败 / 撞 `max_steps`） |
| 工具选择准确率 | `已用工具 ⊇ 必需工具` 的任务占比 |
| 工具参数准确率 | 任务声明的 `expected_args` 命中数 / 声明总数（**只在声明过的任务上算**） |
| 平均步数 / 平均 token / 平均耗时 | 逐任务 `AgentResult` 的均值（只统计真的产出结果的任务） |
| 平均轮次 | 真实 **LLM 轮次**的平均。**与"平均步数"不是一回事**：`steps` 是"工具调用数 + 1"，一轮里模型可以一次发多个 `tool_calls`（真实全量常见 2 个）。只看步数会误判"离 `max_steps` 还有多远"（曾出现"步数 31 + `max_steps(15)`"这种看似矛盾的读数） |
| 压缩比 | `Σ(压缩前 − 压缩后) / Σ压缩前`，来自 `compaction` 事件，并按策略拆分 |
| 压缩事件 / 按策略分布 | 事件**条数**（含 `noop`：被触发就算一次）；与"压缩比 `—` = 没测"是两件事 |
| 摘要 token / 摘要耗时 | 摘要那次**额外** LLM 调用的合计成本（来自摘要器自报的 `usage`；0 = 没摘要或 provider 没报 usage，不估算） |
| 错误恢复率 | 发生过工具失败的**任务**中"最终仍成功"的占比 |

**`—` 不是 0**：分母为 0 的指标（没有声明参数 / 没有任务失败 / 没有压缩事件）一律显示 `—`，表示"没测"。
每份报告自带 `provider` / 模型 / 条数 / 时间戳；**`mock` 报告还带 `config.synthetic: true`** —— 离线夹具
（合成压缩事件、按任务声明直接调用工具）**不是真实成绩**，CLI、JSON 与前端都会明确标出。

天花板：每步 token 未统计（一次响应可含多个工具调用，归属口径不明确）；串行执行（100+ 条时再加并发）；
报告存 JSON 文件、未入库；**压缩比可以为负**（那说明 `after > before`，即上下文变大，属于真实数据不加修饰）；
`list_runs` 按 ISO 字符串排序（写入带时区偏移的时间戳时会失真）；真实 100 条成绩与裁判评分只能在你的机器上跑出来；
**前端 Benchmark 页只触发 mock 自检**（没有 provider / limit / judge 控件，加它们属于新能力，本阶段只统一了风格）——
真实评测走上面的 CLI（`--limit` / `--group` / `--ablation`）。

### 消融实验：记忆/压缩到底有没有用

```bash
# 单组（带设置覆盖，只影响这一次运行，不改 .env）
python scripts/run_benchmark.py --provider real --group baseline --limit 3
python scripts/run_benchmark.py --provider real --group memory --limit 3
# 三组一起跑 + 并排对比（每组各跑 --limit 条；落盘 3 份组报告 + 1 份对比报告）
python scripts/run_benchmark.py --provider real --ablation --limit 3
python scripts/run_benchmark.py --provider mock --ablation --limit 3   # 离线自检：只证明管线通
```

| 组 | memory | compaction | 会话范围 |
|---|---|---|---|
| `baseline` | 关 | 关 | 逐任务隔离 |
| `memory` | short_term **且** long_term 开 | 关 | **整轮共享**（`bench-<run_id>`） |
| `memory+compaction` | 同上 | SUMMARIZE 开 | 整轮共享 |

**方法说明（读结论前必看）**：

1. **`compaction=off` 只等于 `SUMMARIZE` 关**：SQUEEZE/TRUNCATE 是超预算后的无条件行为，三组都会压。
   第三组的自变量只有"要不要额外花一次 LLM 摘要"。
2. **`memory` 组是整轮共享会话**，所以组内靠后的任务受益于靠前的任务 —— 顺序相关，**不能**拿它和
   `baseline` 的绝对名次直接比；成对任务里的 `followup` 单独统计，那才是记忆效应的直接观测点。
3. `memory` 组必须开 `long_term`（向量召回）：`working`/`short_term` 的文本匹配方向是"整段 query 是条目内容的
   子串"，长任务文本几乎命不中 —— 只开 short_term 等于测空气。
4. 报告自带开关快照（`config.group/memory/compaction/session_scope`），三组必须是**同一任务集、同一模型、
   同一版本代码**；对比报告 `kind: "ablation"`，与组报告同目录但不出现在历史列表里。
5. 相对差 = 绝对差 / baseline；baseline 为 0 或指标为"没测"（`—`）时相对差算不出来，同样显示 `—`。

天花板：不做统计显著性检验（100 级样本只给均值与计数）；不做并发；judge 单次采样，噪声未消除；
对比报告只按 `pair_role` 切分**逐任务判分**（成功率/步数/token），压缩与摘要成本只能看整组合计 ——
事件流不进单组报告，所以没法按角色拆分。

**真实成绩位留空**：三组真实数据的报告还没跑（沙箱里没有 LLM 凭据，且这是要花钱的调用）。
这里**不放占位数、不写示例数字** —— 跑完下面这条再贴真实报告：

```bash
python scripts/run_benchmark.py --provider real --ablation --limit 20   # 20 条 × 3 组
```

### 断点续跑：几小时的评测断了不用从头来

```bash
python scripts/run_benchmark.py --provider real --limit 100            # 第一轮（中途断了也没关系）
python scripts/run_benchmark.py --provider real --limit 100 --resume    # 接着跑：已成功的条目直接跳过
python scripts/run_benchmark.py --provider real --ablation --resume     # 消融三组各认各的进度
python scripts/run_benchmark.py --provider real --ablation --ablation-id 20260923-120000-real-ablation-ab12
```

**每条任务判分后立刻追加一行**到 `benchmark_runs/<run_id>.progress.jsonl`（写完就 flush，断电最多丢最后一行）。
消融三组各有**自己**的 run_id 与进度文件（`<ablation_id>-<组名>.progress.jsonl`），且**每组跑完立刻落盘该组报告** ——
第三组崩了，前两组的报告照样在磁盘上。

- **跳过的是"已成功"的条目**：失败/超时/限流的条目下次会**重试**（限流正是要重试的东西）。
- **续跑必须配置相同**：进度文件首行存了配置指纹（provider/模型/judge/limit/条数/任务集 id 集合/分组开关），
  对不上就**拒绝续跑**并说清原因 —— 换模型或换 `--limit` 之后接着跑，等于把两套配置的指标混成一份报告。
- **跑完的不再续**：最终报告已在磁盘上时，`--resume` 会开新一轮（不会改坏已完成的那份）。
- **来源可查**：从进度文件捡回来的判分带 `skipped: true`，报告 `config.resumed` 记录条数。
- 进度文件不是报告：历史列表只扫 `*.json`，它不会变成一条幽灵记录。

天花板：进度写入是**尽力而为但可见**的（目录只读/磁盘满时报告照样跑完，原因写进 `config.progress_error` 并打日志）；
进度记录只存"重算指标所需的最小集"（判分 + 工具事件 + 压缩事件），不存答案正文，
所以续跑不能重放答案、也不能补跑裁判；任务集只按 **id 集合**判断是否变过，改了某条任务的文本不会被发现。

## 输出可信度（防幻觉）

工具型 Agent 最大的失真来源不是工具挂了，而是**工具挂了模型却照写报告**。本项目在三个层面兜底：

1. **System Prompt 硬规则**：只能基于本次对话的工具结果陈述；工具失败必须如实说明失败原因与拿不到什么；
   禁止编造文件内容、仓库结构或引用来源。
2. **结果级告警**：若一轮任务里**所有工具调用都失败**，`AgentResult.warning` 与 WebSocket 的 `final_answer`
   事件都会带上「所有工具调用都失败了（N/N）：最终答案没有建立在真实工具结果之上，请勿直接采信」。
3. **低 temperature**：`LLM_TEMPERATURE`（默认 `0.2`）。

## Demo 1：GitHub 仓库分析

```bash
python scripts/run_demo1_github.py --repo fastapi/fastapi
python scripts/run_demo1_github.py --repo pallets/flask --focus "错误处理与重试"
# 会话标识：短时记忆按会话隔离，不传则全部落在 default
python scripts/run_demo1_github.py --repo fastapi/fastapi --session-id smoke-1
```

用法与观察点见 `docs/demo1_github.md`。

## 目录结构

```
src/agent_runtime/
  core/           协议与纯逻辑：agent(ReAct) / llm / tool / skill / memory / context / trace / benchmark / mcp
  infrastructure/ 实现：llm / mcp(客户端 + 自研 server 的 stdio 循环) / db / memory(Redis·PG) / tools / skills /
                  benchmark / trace(内存环形 + 可选 SQLite)
  api/            FastAPI：routes（chat / tools / skills / memories / benchmarks / traces / context）/ ws /
                  schemas / deps / app
  config/         settings（pydantic-settings）与日志
skills/           内置技能（Markdown SOP）
benchmarks/       评测任务集（tasks.json）
benchmark_runs/   评测报告落盘（gitignore）
alembic/          memory_entries 迁移（pgvector）
scripts/          run_demo_mock.py（零依赖）/ run_demo1_github.py（真实链路）/ run_benchmark.py（评测）/
                  inspect_run.py（只读看报告）/ mcp_server.py（把工具暴露出去）
tests/            unit/（无需 pytest 也能跑）+ integration/
web/              React 19 + Vite + Tailwind v4 + shadcn/ui（Chat / Trace / Inspector / Benchmark）
```
