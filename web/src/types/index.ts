export type AgentStreamEvent = {
  event_type:
    | "skill_matched"
    | "step_start"
    | "thought"
    | "tool_call"
    | "tool_result"
    | "compaction"
    | "final_answer"
    | "done"
    | "error";
  data: Record<string, any>;
};

export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  events: AgentStreamEvent[];
  warning?: string | null;
  streaming?: boolean;
  error?: string | null;
};

// ── Benchmark（Phase 6）──
// 指标里的 `null` 表示"分母为 0、没测"，UI 必须显示 `—` 而不是 0。

export type BenchmarkMetrics = {
  tasks_total: number;
  tasks_errored: number;
  success_rate: number | null;
  success_rate_measured: number | null;
  provider_errors: number;
  tool_selection_accuracy: number | null;
  tool_argument_accuracy: number | null;
  avg_steps: number | null;
  /** 真实 LLM 轮次（`avg_steps` 是"工具调用数 + 1"的平均，两者不是一回事）。 */
  avg_rounds?: number | null;
  avg_total_tokens: number | null;
  avg_latency_ms: number | null;
  compression_ratio: number | null;
  compression_by_strategy: Record<string, number | null>;
  // 事件数是计数（0 是真值）；摘要成本是"额外花的钱"，0 = 没摘要或 provider 没报 usage。
  // 这四个字段是 Phase 7 加的：**更早落盘的报告里没有它们**，读出来是 undefined → 显示 `—`。
  compaction_events?: number;
  compaction_events_by_strategy?: Record<string, number>;
  summarizer_tokens?: number;
  summarizer_ms?: number;
  error_recovery_rate: number | null;
  judged_tasks: number;
  avg_judge_score: number | null;
};

export type TaskVerdict = {
  task_id: string;
  success: boolean;
  required_tools_ok: boolean;
  keywords_ok: boolean;
  missing_tools: string[];
  missing_keywords: string[];
  extra_tool_calls: number;
  arg_hits: number;
  arg_expected: number;
  warning: string | null;
  skills_used: string[];
  steps: number;
  min_steps: number;
  /** 真实 LLM 轮次与轮次上限（旧报告没有这两个字段 → 显示 `—`）。 */
  rounds?: number;
  max_steps?: number;
  total_tokens: number;
  latency_ms: number;
  error: string;
  judge_scores: Record<string, number> | null;
  judge_reason: string;
  /** 判分是从进度文件捡回来的（上一轮已成功跑过），不是本轮刚跑。 */
  skipped?: boolean;
};

export type BenchmarkRunSummary = {
  run_id: string;
  created_at: string;
  config: Record<string, any>;
  metrics: BenchmarkMetrics;
};

export type BenchmarkReport = BenchmarkRunSummary & { verdicts: TaskVerdict[] };

export type BenchmarkRunList = { count: number; runs: BenchmarkRunSummary[] };

export type BenchmarkStartResponse = {
  run_id: string;
  started: boolean;
  errors?: string[];
};

// ── Trace / Context / 目录（Phase 8）──
// 与 `GET /api/traces`、`GET /api/context`、`/api/tools`、`/api/skills`、`/api/memories` 一一对应。
// 缺失字段（旧数据、可选能力）一律是可选的 → UI 显示 `—`，不要假装是 0。

export type TraceSource = "chat" | "benchmark" | "demo";

export type TraceSummary = {
  trace_id: string;
  task: string;
  source: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  /** 步数（合并后的步骤数）。 */
  steps: number;
  total_tokens: number;
  total_latency_ms: number;
  final_answer: string | null;
};

export type TraceStoreInfo = { store: string; errors: string[] };

export type TracesResponse = {
  count: number;
  traces: TraceSummary[];
  /** 本次查询的错误；装配期错误在 `settings.errors` 里，两者故意分开。 */
  errors: string[];
  settings: TraceStoreInfo;
};

export type TokenUsageView = {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
};

export type ToolCallView = { tool_name?: string; args?: Record<string, any> };

/** 一步里可能有多个工具调用，所以这两个字段是"一个对象或一串对象"。 */
export type TraceStepView = {
  step_number: number;
  thought: string | null;
  tool_call: ToolCallView | ToolCallView[] | null;
  tool_result:
    | { tool_name?: string; success?: boolean; chars?: number; latency_ms?: number; text?: string }
    | Array<{ tool_name?: string; success?: boolean; chars?: number; latency_ms?: number; text?: string }>
    | null;
  token_usage: TokenUsageView;
  latency_ms: number;
};

export type TraceEventView = {
  event_type: string;
  step_number: number;
  timestamp: string;
  data: Record<string, any>;
};

export type TraceDetail = {
  trace_id: string;
  task: string;
  config: Record<string, any>;
  steps: TraceStepView[];
  final_answer: string | null;
  total_tokens: TokenUsageView;
  total_latency_ms: number;
  status: string;
  started_at: string;
  finished_at: string | null;
  source: string;
  events: TraceEventView[];
};

/** `available:false` 时 `trace` 是 `null`，`reason` 是人读得懂的原因（HTTP 404 详情就是它）。 */
export type TraceDetailResponse = {
  available: boolean;
  trace: TraceDetail | null;
  reason: string;
};

export type TraceEventsResponse = { count: number; events: TraceEventView[]; reason: string };

export type ContextSection = { name: string; tokens: number; chars: number };

export type CompactionView = {
  strategy: string;
  before: number;
  after: number;
  saved_tokens: number;
  messages_dropped: number;
  messages_squeezed: number;
  summarized: number;
  noop: boolean;
  degraded_from: string | null;
  degraded_reason: string;
  summarizer_tokens: number;
  summarizer_ms: number;
};

/** 拿不到上下文时只有 `available/reason/errors`；拿到了才有其余字段。 */
export type ContextSnapshot = {
  available: boolean;
  reason: string;
  errors: string[];
  budget?: {
    model_max_tokens: number;
    reserved_output: number;
    available: number;
    threshold: number;
  };
  used_tokens?: number;
  ratio?: number;
  sections?: ContextSection[];
  messages?: number;
  /** `null` = 这轮**没压过**（不是"压了但没变化"——那是 `noop: true`）。 */
  last_compaction?: CompactionView | null;
};

export type ToolCatalogEntry = {
  name: string;
  description: string;
  parameters: Record<string, any>;
};

export type ToolsResponse = { count: number; tools: ToolCatalogEntry[] };

export type SkillCatalogEntry = {
  name: string;
  description: string;
  version: string;
  triggers: string[];
  required_tools: string[];
  tags: string[];
};

export type SkillsResponse = { count: number; skills: SkillCatalogEntry[] };

export type MemoryEntryView = {
  content: string;
  role: string;
  source: string;
  created_at: string | null;
  metadata: Record<string, any>;
  id: string;
};

export type MemoriesResponse = {
  settings: {
    short_term: { enabled: boolean };
    long_term: { enabled: boolean };
    errors: string[];
  };
  enabled: { short_term: boolean; long_term: boolean };
  count: number;
  memories: MemoryEntryView[];
  errors: string[];
};
