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

## 设计文档

- 设计 spec：`docs/superpowers/specs/2026-09-21-agent-runtime-design.md`
- Phase 计划：`docs/superpowers/plans/`

## 测试

```bash
python tests/unit/test_llm_mock.py        # 无 pytest 环境可直接 python 运行
python tests/unit/test_file_reader.py
python tests/unit/test_react.py
# 或 pytest（装了 dev 依赖时）
```

## Roadmap

- [x] Phase 0 骨架：全接口 + 数据类 + FastAPI 空壳 + 前端空壳
- [x] Phase 1 ReAct 最小闭环：LLM Provider + ReActLoop + FileReaderTool + chat/ws + ChatPage
- [ ] Phase 2 Tool 生态 + MCP Client
- [ ] Phase 3 Skill + TaskPlanner
- [ ] Phase 4 Memory 持久化 (Redis + pgvector)
- [ ] Phase 5 Compaction 完整版 (SUMMARIZE)
- [ ] Phase 6 Benchmark 系统
- [ ] Phase 7 消融实验 + 100~120 条数据集
- [ ] Phase 8 自研 MCP Server + Trace/Inspector 页
- [ ] Phase 9 文档收尾

## Future Work

- Multi-Agent Orchestration via MCP Protocol
