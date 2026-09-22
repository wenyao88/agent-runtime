# Agent Runtime — 技术研究与研发 Agent 运行框架

自研单 Agent Runtime，覆盖 **ReAct Loop / Function Calling / Tool Registry / MCP / Skills / 三层 Memory / Context Manager / Context Compaction / Agent Trace / Benchmark**，并以两个真实场景验证（GitHub 仓库分析、技术调研报告）。

## 能力链

```
ReAct → Function Calling → Tool Registry → MCP → Skills
→ Memory (Working/Short-term/Long-term) → Context Manager
→ Context Compaction (Squeeze/Truncate/Summarize) → Trace → Benchmark
```

## 架构

| 层 | 目录 | 职责 |
|----|------|------|
| React Web UI | `web/` | Chat / Trace / Inspector 三页面 |
| FastAPI | `src/agent_runtime/api/` | REST + WebSocket，依赖注入 |
| Core | `src/agent_runtime/core/` | 纯接口 + 纯逻辑，零 infra 依赖 |
| Infrastructure | `src/agent_runtime/infrastructure/` | LLM Provider / MCP / DB / Native Tools |
| Benchmark | `src/agent_runtime/benchmark/` | Runner / Evaluator / 消融实验 |

## 快速开始

### 0) 零依赖先看效果（不用 API Key、不用装包）

```bash
python scripts/run_demo_mock.py
```

用脚本化 Mock 模型驱动一次完整 ReAct 闭环：思考 → 调用 `read_file` → 观察结果 → 工具报错被模型自行纠正 → 输出最终答案，并打印步数 / token / trace id。

### 1) 真实模型链路

```bash
cp .env.example .env   # 填入 LLM_API_KEY（硅基流动/DeepSeek/Qwen/GLM 任一 OpenAI 兼容端点）

# 后端：Docker 或本地二选一
docker compose up                     # api + postgres(pgvector) + redis
# 或
pip install -e ".[dev]" && uvicorn agent_runtime.api.app:app --port 8000

curl -X POST http://localhost:8000/api/chat -H "Content-Type: application/json" -d "{\"task\": \"读取 计划.md 并总结\"}"
```

### 2) 前端（实时执行过程）

```bash
cd web && pnpm install && pnpm run dev   # http://localhost:5173
```

Chat 页通过 `ws://localhost:8000/ws/agent/{session_id}` 接收 `skill_matched / step_start / thought / tool_call / tool_result / compaction / final_answer / done` 事件流，折叠卡片实时展示执行过程。

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

工具契约（所有工具一致，含 MCP 工具）：`execute()` **永不抛异常**，失败一律返回
`ToolResult(success=False, text="Error: …")` 作为 observation，由模型自行纠正。

## MCP：消费外部 MCP Server

把 `mcp_servers.example.json` 复制为 `mcp_servers.json` 并启用需要的 server：

```json
[
  { "name": "filesystem", "transport": "stdio", "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "."], "enabled": true }
]
```

启动时（FastAPI lifespan）连接并发现工具，包装成 `MCPToolAdapter` 注入**同一个** registry，
工具名带 server 前缀（`filesystem__read_file`）避免与原生工具撞名。控制台 `GET /api/tools`
可看到全部工具（原生 + MCP）的统一形态。

**MCP 是可选能力**：配置文件缺失/损坏、或某个 server 起不来，只在 `app.state.mcp_errors`
里记录，**绝不阻断启动**。

## Skills：领域 SOP（Markdown 定义）

技能 = 一段**领域操作流程**（SOP）。启动时从 `skills/*.md` 加载，按任务关键词命中后，把正文**追加**进
System Prompt —— Agent 面对"仓库分析"或"技术调研"时按固定套路走，而不是每次从零思考。

内置两个技能：`skills/github_analysis.md`（GitHub 仓库分析）、`skills/tech_research.md`（技术调研）。

### 新增一个技能

在 `skills/` 下建一个 `.md`，front-matter + 正文即可（**不需要改代码**）：

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

支持**有限**的 front-matter 子集：标量、`[a, b]`、`- item` 列表。嵌套映射、锚点、多行字符串
**不支持**——遇到就明确报错并跳过该文件（不猜）。`name` / `description` 必填，未知键忽略（向前兼容）。

### 命中规则与可见性

- **打分**：命中的 trigger **个数**，降序取前 `AGENT_SKILL_TOP_K`（默认 1）个；同分按加载顺序。
  纯关键词子串匹配（大小写无关），**不是**语义匹配。
- **无命中就不注入**：宁可不用技能，也不注入一个不相关的 SOP。
- **注入顺序（安全边界）**：技能文本**只能追加**在 `DEFAULT_SYSTEM_PROMPT` 的硬规则**之后** —— 技能是"内容"，
  防幻觉规则是"底线"，内容不得改写底线（有测试钉死）。
- **怎么知道这次用了哪个技能**：`AgentResult.skills_used`、`skill_matched` 事件、trace session 的
  `config["skills"]`，以及 `GET /api/skills`（列出当前已加载的技能）。

### TaskPlanner（可选，默认关闭）

`AGENT_TASK_PLANNING_ENABLED=true` 时，任务开始前多花一次 LLM 调用产出**计划文本**，同样追加进 System Prompt。

- **天花板（明确标注）**：计划只是"参考文本"，**不保证逐子任务执行** —— ReAct 控制流没有改动，
  模型可能不完全按计划走。升级路径是结构化 `TaskPlan` + 逐子任务循环（会改动 Trace 语义，需先验证收益）。
- **规划器只看得到工具名、看不到参数 schema**（刻意如此：传 schema 会让模型倾向在规划阶段直接发 tool_calls，
  而那一轮的 tool_calls 无处执行）。代价是**计划可能写出一条"工具名对、参数不合法"的步骤** ——
  计划文本会明确标注为"仅供参考，可按实际情况调整"。
- **计划文本有长度上限**（2000 字符，超出会带 `…（计划过长，已截断）` 标记）：它会进入每一步请求的 system prompt，
  不设上限就是每一步都在烧钱。
- **失败即降级**：规划 LLM 报错/返回空 → 不注入任何计划文本，主任务照常跑。
- **技能与坏文件都不会阻断启动**：加载错误只记在 `app.state.skill_errors` 里。

## Memory：三层记忆（默认全关）

| 层 | 存储 | 范围 | 检索方式 |
|---|---|---|---|
| Working | 进程内 | 单次任务 | 子串匹配（已有） |
| Short-term | Redis List + TTL | **单会话**（`session_id`） | 最近 N 条 + 子串过滤 |
| Long-term | PostgreSQL + pgvector | 跨会话 | **余弦距离语义检索** |

召回顺序 `working → short_term → long_term`，够 `MEMORY_RECALL_TOP_K` 条即**短路**（不查更慢的持久层）。

### 开启步骤

```bash
cp .env.example .env
# 打开需要的层（默认全关：没起 DB/Redis 也不影响启动与任务）
#   MEMORY_SHORT_TERM_ENABLED=true
#   MEMORY_LONG_TERM_ENABLED=true      # 还需要 EMBEDDING_API_KEY
#   MEMORY_CONSOLIDATE_ENABLED=true    # 任务结束用 JUDGE_LLM 压成一条长期记忆

docker compose up -d          # api 启动时会自动执行 alembic upgrade head
# 或本地手动：
alembic upgrade head
```

**任一层不可用只记错误、不阻断启动与任务**：装配期错误进 `app.state.memory_errors`（`GET /api/memories` 的
`settings.errors` 也能看到），运行期错误进 `MemoryManager.errors`（有上限，manager 是进程单例）。这是刻意的：
`ReActLoop` 结尾写记忆的那一步**没有 try 保护**，所以"记忆写失败"必须由记忆层自己咽下去。

### 召回如何进入上下文

带**来源与日期**标注、截断带省略号：

```
Relevant Memories:
- [long_term · 2026-09-21] 上次调研结论：pgvector 的运维成本最低…
- [short_term] 本会话前面确认过 Qdrant 的许可协议…
```

这样模型能区分"历史记忆"和"本轮工具结果"（本项目一贯的"证据可追溯"主题）。
长度上限 `MEMORY_INJECT_MAX_CHARS`（默认 500，超出带 `…` 标记）。

### 天花板（不静默）

| 项 | 现状 | 升级路径 |
|---|---|---|
| 向量维度 | 固定 1024，换模型需迁移 + 重算历史向量 | 维度配置化 + 双列灰度迁移 |
| 索引 | ivfflat `lists=100` | 数据量上来后改 hnsw，调 `m`/`ef_construction` |
| 记忆淘汰 | 只有"每会话最多 N 条 + TTL" | 按访问频次/重要性衰减（需额外统计列） |
| `consolidate` | 单次 LLM 摘要，**不保证无损**；失败降级为原文截断 | 多轮摘要 + 保留原文引用链 |
| 短时 `clear()` | 只删**单会话**（`DELETE /api/memories` 清的是默认会话）；long_term 是全表清空 | 全量清空用 `SCAN`；按会话删需加列 |
| 三层写入 | **无事务**，各自 best-effort | 出站队列 / 补偿任务 |

## Demo 1：GitHub 仓库分析

```bash
python scripts/run_demo1_github.py --repo fastapi/fastapi
python scripts/run_demo1_github.py --repo pallets/flask --focus "错误处理与重试"
```

详见 `docs/demo1_github.md`；Demo 2（技术调研）骨架见 `docs/demo2_research.md`。

## 输出可信度（防幻觉）

工具型 Agent 最大的失真来源不是工具挂了，而是**工具挂了模型却照写报告**。本项目在三个层面兜底：

1. **System Prompt 硬规则**：只能基于本次对话的工具结果陈述；工具失败必须如实说明失败原因与拿不到什么；禁止编造文件内容、仓库结构或引用来源。
2. **结果级告警**：若一轮任务里**所有工具调用都失败**，`AgentResult.warning` 与 WebSocket 的 `final_answer` 事件都会带上
   「所有工具调用都失败了（N/N）：最终答案没有建立在真实工具结果之上，请勿直接采信」——把"不可信"变成**用户可见的事实**，而不是指望模型自觉。
3. **低 temperature**：`LLM_TEMPERATURE`（默认 `0.2`）。不少厂商默认 0.6~1.0，小模型在证据缺失时更容易编造。

> 实测记录：Qwen3-8B 在四个 GitHub 工具全部 301 失败后，仍然产出了完整的"分析报告"。第 2、3 层就是针对该现象加的；
> 修复前的测试也证明**失败原因本来就已经进了上下文**——模型是"看到了失败仍然编造"，所以只补证据链没有用。

## 设计文档

- 设计 spec：`docs/superpowers/specs/2026-09-21-agent-runtime-design.md`
- Phase 计划：`docs/superpowers/plans/`

## 测试

测试文件是**双模式**的：装了 pytest 可 `pytest tests/`，没装 pytest 也能直接 `python` 运行
（本仓库的开发沙箱就是后者）。

```bash
# 全部跑一遍（无需 pytest）
Get-ChildItem tests -Recurse -Filter 'test_*.py' | ForEach-Object { python $_.FullName }

# 单个文件
python tests/unit/test_react.py
python tests/unit/test_mcp_client.py
```

覆盖范围：ReAct 循环 / 工具契约与 schema / HTTP 工具基座 / GitHub・搜索・抓取・PDF 四个工具 /
MCP 客户端与 schema 翻译 / 上下文压缩（SQUEEZE・TRUNCATE・策略升级）/ 工具装配 /
Skill 加载・打分路由・内置技能・装配 / TaskPlanner / 三层记忆与召回标注 / Alembic 迁移内容 /
Demo 脚本契约 / **仓库编码约定** / API 冒烟（无 fastapi 时自动 SKIP）。

## 编码约定（踩过三次的坑，现已用测试锁死）

本项目中文注释很多，而"编码"这一族问题在本仓库发作过三次，症状都很隐蔽：

1. 用 GBK 存一个技能 `.md` → **整个技能目录**加载失败，好文件被连带丢掉；
2. 用 Windows PowerShell 5.1 的 `Set-Content`（默认 ANSI）改源文件 → UTF-8 源码变乱码，不可恢复；
3. `alembic.ini` 里写了中文注释 → 中文 Windows 上 `alembic upgrade head` 直接 `UnicodeDecodeError`。

根因不是"中文有罪"，而是**谁读这个文件、用什么编码读**：

| 文件类型 | 读取方 | 约定 |
|---|---|---|
| `.py` | Python（PEP 3120 固定 UTF-8） | **中文安全**，注释/文档字符串照写 |
| `.md` / `.json` / `.yml` / `.toml` | 我们或明确的 UTF-8 读取 | 中文安全 |
| `*.ini` / `*.cfg` / `*.conf` / `*.mako` | **外部工具按 locale 编码读**（如 Alembic 的 configparser） | **必须纯 ASCII**，注释用英文 |

`tests/unit/test_repo_encoding.py` 全仓库守护这条界线（既检查 locale 读取类文件无非 ASCII 字节，
也检查源码/资产是合法 UTF-8），并有一条**反向断言**防止把它过度推广成"代码里不许出现中文"。
另：**不要用 PowerShell 文本管道改仓库源文件**，用编辑工具（`write`/`edit`）。

## Roadmap

- [x] Phase 0 骨架：全接口 + 数据类 + FastAPI 空壳 + 前端空壳
- [x] Phase 1 ReAct 最小闭环：LLM Provider + ReActLoop + FileReaderTool + chat/ws + ChatPage
- [x] Phase 2 Tool 生态 + MCP Client：8 个原生工具 + MCP Client/Adapter + SQUEEZE/TRUNCATE 压缩 + `GET /api/tools`
- [x] Phase 3 Skill + TaskPlanner：Markdown 技能加载 + 关键词打分路由 + 2 个内置技能 + TaskPlanner（默认关）+ `GET /api/skills`
- [x] Phase 4 Memory 持久化：三层记忆（Working / Redis Short-term / PG+pgvector Long-term）+ Embedding + Alembic 迁移 + `GET/DELETE /api/memories`（默认全关）
- [ ] Phase 5 Compaction 完整版 (SUMMARIZE)
- [ ] Phase 6 Benchmark 系统
- [ ] Phase 7 消融实验 + 100~120 条数据集
- [ ] Phase 8 自研 MCP Server + Trace/Inspector 页
- [ ] Phase 9 文档收尾

## Future Work

- Multi-Agent Orchestration via MCP Protocol
