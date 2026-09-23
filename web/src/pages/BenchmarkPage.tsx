import { useCallback, useEffect, useState } from "react";
import { Play, RefreshCw } from "lucide-react";
import { fetchBenchmark, fetchBenchmarks, startBenchmarkRun } from "../api/client";
import type {
  BenchmarkMetrics,
  BenchmarkReport,
  BenchmarkRunSummary,
} from "../types";

type Kind = "pct" | "num" | "int";

const METRIC_ROWS: { label: string; key: keyof BenchmarkMetrics; kind: Kind }[] = [
  { label: "任务数", key: "tasks_total", kind: "int" },
  { label: "出错任务数", key: "tasks_errored", kind: "int" },
  { label: "成功率", key: "success_rate", kind: "pct" },
  { label: "工具选择准确率", key: "tool_selection_accuracy", kind: "pct" },
  { label: "工具参数准确率", key: "tool_argument_accuracy", kind: "pct" },
  { label: "平均步数", key: "avg_steps", kind: "num" },
  { label: "平均 token", key: "avg_total_tokens", kind: "num" },
  { label: "平均耗时(ms)", key: "avg_latency_ms", kind: "num" },
  { label: "压缩比", key: "compression_ratio", kind: "pct" },
  { label: "错误恢复率", key: "error_recovery_rate", kind: "pct" },
  { label: "裁判均分", key: "avg_judge_score", kind: "num" },
  { label: "已判分条数", key: "judged_tasks", kind: "int" },
];

/** `null` 是"没测"（分母为 0），必须显示 `—` 而不是 0 —— 与 CLI 同一口径。 */
function render(value: number | null | undefined, kind: Kind): string {
  if (value === null || value === undefined) return "—";
  if (kind === "pct") return `${(value * 100).toFixed(1)}%`;
  if (kind === "int") return String(value);
  return value.toFixed(1);
}

function MetricsTable({ metrics }: { metrics: BenchmarkMetrics }) {
  const byStrategy = Object.entries(metrics.compression_by_strategy || {});
  return (
    <table className="w-full text-sm">
      <tbody>
        {METRIC_ROWS.map(({ label, key, kind }) => (
          <tr key={label} className="border-b border-slate-100 last:border-0">
            <td className="py-1 text-slate-500">{label}</td>
            <td className="py-1 text-right font-medium text-slate-800">
              {render(metrics[key] as number | null, kind)}
            </td>
          </tr>
        ))}
        {byStrategy.map(([strategy, ratio]) => (
          <tr key={strategy} className="border-b border-slate-100 last:border-0">
            <td className="py-1 pl-4 text-slate-400">· {strategy}</td>
            <td className="py-1 text-right text-slate-600">{render(ratio, "pct")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function VerdictList({ report }: { report: BenchmarkReport }) {
  return (
    <ul className="max-h-72 overflow-auto text-sm">
      {report.verdicts.map((v) => {
        const notes: string[] = [];
        if (v.missing_tools.length) notes.push(`缺工具 ${v.missing_tools.join(",")}`);
        if (v.missing_keywords.length) notes.push(`缺关键词 ${v.missing_keywords.join(",")}`);
        if (v.warning) notes.push(v.warning);
        if (v.error) notes.push(v.error);
        if (v.judge_reason) notes.push(v.judge_reason);
        return (
          <li key={v.task_id} className="border-b border-slate-100 py-1 last:border-0">
            <span className={v.success ? "text-emerald-600" : "text-rose-600"}>
              {v.success ? "✓" : "✗"}
            </span>{" "}
            <span className="font-mono text-xs text-slate-700">{v.task_id}</span>
            <span className="ml-2 text-xs text-slate-400">
              步数 {v.steps} · token {v.total_tokens}
            </span>
            {notes.length > 0 && (
              <div className="pl-4 text-xs text-slate-500">{notes.join("；")}</div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export default function BenchmarkPage() {
  const [runs, setRuns] = useState<BenchmarkRunSummary[]>([]);
  const [selected, setSelected] = useState<BenchmarkReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await fetchBenchmarks();
      setRuns(data.runs);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const open = useCallback(async (runId: string) => {
    try {
      setSelected(await fetchBenchmark(runId));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const startMockRun = useCallback(async () => {
    setBusy(true);
    try {
      const { run_id, started, errors } = await startBenchmarkRun({ provider: "mock" });
      if (!started) {
        setError((errors || ["启动失败"]).join("；"));
        return;
      }
      // 后台跑：轮询直到报告出现（最多 ~10 秒）
      for (let i = 0; i < 40; i += 1) {
        await new Promise((r) => setTimeout(r, 250));
        try {
          setSelected(await fetchBenchmark(run_id));
          await refresh();
          setError(null);
          return;
        } catch {
          /* 还没生成，继续等 */
        }
      }
      setError(`报告在 10 秒内没有生成：${run_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [refresh]);

  return (
    <div className="h-full overflow-auto p-6">
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-lg font-semibold text-slate-800">Benchmark</h1>
        <button
          type="button"
          onClick={() => void refresh()}
          className="flex items-center gap-1 rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-100"
        >
          <RefreshCw size={12} /> 刷新
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void startMockRun()}
          className="flex items-center gap-1 rounded bg-slate-800 px-2 py-1 text-xs text-white hover:bg-slate-700 disabled:opacity-50"
        >
          <Play size={12} /> {busy ? "运行中…" : "跑一次（mock，离线）"}
        </button>
      </div>

      {error && (
        <div className="mb-3 rounded border border-rose-200 bg-rose-50 p-2 text-xs text-rose-700">
          {error}
        </div>
      )}

      <div className="flex gap-6">
        <div className="w-64 shrink-0">
          <div className="mb-2 text-xs font-medium text-slate-500">历史运行</div>
          {runs.length === 0 && <div className="text-xs text-slate-400">还没有报告</div>}
          <ul className="space-y-1">
            {runs.map((run) => (
              <li key={run.run_id}>
                <button
                  type="button"
                  onClick={() => void open(run.run_id)}
                  className={`w-full rounded px-2 py-1 text-left text-xs ${
                    selected?.run_id === run.run_id
                      ? "bg-slate-800 text-white"
                      : "text-slate-600 hover:bg-slate-100"
                  }`}
                >
                  <div className="font-mono">{run.run_id}</div>
                  <div className="opacity-70">
                    {run.config?.provider ?? "?"} · {run.metrics.tasks_total} 条 · 成功率{" "}
                    {render(run.metrics.success_rate, "pct")}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="flex-1">
          {!selected && <div className="text-sm text-slate-400">选一份报告查看指标与明细</div>}
          {selected && (
            <div className="space-y-4">
              <div className="text-xs text-slate-500">
                <span className="font-mono">{selected.run_id}</span> · provider{" "}
                <span className="font-medium">{selected.config?.provider ?? "?"}</span> · 模型{" "}
                {selected.config?.model ?? "?"} · {selected.created_at}
                {(selected.config?.synthetic === true ||
                  selected.config?.provider === "mock") && (
                  <div className="mt-1 rounded bg-amber-50 p-1 text-amber-700">
                    mock 是离线夹具（合成事件、按任务声明直接调用工具），不是真实成绩
                    {selected.config?.synthetic !== true && "（旧报告没有 synthetic 标记）"}
                  </div>
                )}
              </div>
              <div className="rounded border border-slate-200 bg-white p-3">
                <div className="mb-2 text-xs font-medium text-slate-500">指标</div>
                <MetricsTable metrics={selected.metrics} />
              </div>
              <div className="rounded border border-slate-200 bg-white p-3">
                <div className="mb-2 text-xs font-medium text-slate-500">
                  逐任务明细（{selected.verdicts.length}）
                </div>
                <VerdictList report={selected} />
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
