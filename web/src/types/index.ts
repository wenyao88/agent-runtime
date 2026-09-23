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
