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

Chat 页通过 `ws://localhost:8000/ws/agent/{session_id}` 接收 `step_start / thought / tool_call / tool_result / compaction / final_answer / done` 事件流，折叠卡片实时展示执行过程。

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

## Demo 1：GitHub 仓库分析

```bash
python scripts/run_demo1_github.py --repo fastapi/fastapi
python scripts/run_demo1_github.py --repo pallets/flask --focus "错误处理与重试"
```

详见 `docs/demo1_github.md`。

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
Demo 脚本契约 / API 冒烟（无 fastapi 时自动 SKIP）。

## Roadmap

- [x] Phase 0 骨架：全接口 + 数据类 + FastAPI 空壳 + 前端空壳
- [x] Phase 1 ReAct 最小闭环：LLM Provider + ReActLoop + FileReaderTool + chat/ws + ChatPage
- [x] Phase 2 Tool 生态 + MCP Client：8 个原生工具 + MCP Client/Adapter + SQUEEZE/TRUNCATE 压缩 + `GET /api/tools`
- [ ] Phase 3 Skill + TaskPlanner
- [ ] Phase 4 Memory 持久化 (Redis + pgvector)
- [ ] Phase 5 Compaction 完整版 (SUMMARIZE)
- [ ] Phase 6 Benchmark 系统
- [ ] Phase 7 消融实验 + 100~120 条数据集
- [ ] Phase 8 自研 MCP Server + Trace/Inspector 页
- [ ] Phase 9 文档收尾

## Future Work

- Multi-Agent Orchestration via MCP Protocol
