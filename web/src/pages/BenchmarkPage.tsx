import { useCallback, useEffect, useState } from "react";
import { Play, RefreshCw } from "lucide-react";
import { fetchBenchmark, fetchBenchmarks, startBenchmarkRun } from "../api/client";
import type {
  BenchmarkMetrics,
  BenchmarkReport,
  BenchmarkRunSummary,
} from "../types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DASH, formatNumber, formatRatio, formatTokens } from "@/lib/format";

type Kind = "pct" | "num" | "int";

const METRIC_ROWS: { label: string; key: keyof BenchmarkMetrics; kind: Kind }[] = [
  { label: "任务数", key: "tasks_total", kind: "int" },
  { label: "出错任务数", key: "tasks_errored", kind: "int" },
  { label: "成功率", key: "success_rate", kind: "pct" },
  { label: "成功率(排除 provider)", key: "success_rate_measured", kind: "pct" },
  { label: "provider 错误", key: "provider_errors", kind: "int" },
  { label: "工具选择准确率", key: "tool_selection_accuracy", kind: "pct" },
  { label: "工具参数准确率", key: "tool_argument_accuracy", kind: "pct" },
  { label: "平均步数", key: "avg_steps", kind: "num" },
  { label: "平均轮次", key: "avg_rounds", kind: "num" },
  { label: "平均 token", key: "avg_total_tokens", kind: "num" },
  { label: "平均耗时(ms)", key: "avg_latency_ms", kind: "num" },
  { label: "压缩比", key: "compression_ratio", kind: "pct" },
  { label: "压缩事件", key: "compaction_events", kind: "int" },
  { label: "摘要 token", key: "summarizer_tokens", kind: "int" },
  { label: "摘要耗时(ms)", key: "summarizer_ms", kind: "int" },
  { label: "错误恢复率", key: "error_recovery_rate", kind: "pct" },
  { label: "裁判均分", key: "avg_judge_score", kind: "num" },
  { label: "已判分条数", key: "judged_tasks", kind: "int" },
];

/** 一律走 `lib/format`：`null`/`undefined` 是"没测"（分母为 0），必须显示 `—` 而不是 0。 */
const FORMAT: Record<Kind, (value: number | null | undefined) => string> = {
  pct: formatRatio,
  num: (value) => formatNumber(value, 1),
  int: formatTokens,
};

function MetricsTable({ metrics }: { metrics: BenchmarkMetrics }) {
  const byStrategy = Object.entries(metrics.compression_by_strategy || {});
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>指标</TableHead>
          <TableHead className="text-right">数值</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {METRIC_ROWS.map(({ label, key, kind }) => (
          <TableRow key={label}>
            <TableCell className="text-slate-500">{label}</TableCell>
            <TableCell className="text-right font-medium text-slate-800">
              {FORMAT[kind](metrics[key] as number | null | undefined)}
            </TableCell>
          </TableRow>
        ))}
        {byStrategy.map(([strategy, ratio]) => (
          <TableRow key={strategy}>
            <TableCell className="pl-4 text-slate-400">· {strategy}</TableCell>
            <TableCell className="text-right text-slate-600">{formatRatio(ratio)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function VerdictTable({ report }: { report: BenchmarkReport }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>任务</TableHead>
          <TableHead>结果</TableHead>
          <TableHead>步数</TableHead>
          <TableHead>轮次</TableHead>
          <TableHead>token</TableHead>
          <TableHead>备注</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {report.verdicts.map((v) => {
          const notes: string[] = [];
          if (v.missing_tools.length) notes.push(`缺工具 ${v.missing_tools.join(",")}`);
          if (v.missing_keywords.length) notes.push(`缺关键词 ${v.missing_keywords.join(",")}`);
          if (v.warning) notes.push(v.warning);
          if (v.error) notes.push(v.error);
          if (v.judge_reason) notes.push(v.judge_reason);
          if (v.skipped) notes.push("↻ 续跑复用");
          return (
            <TableRow key={v.task_id}>
              <TableCell className="font-mono text-xs text-slate-700">{v.task_id}</TableCell>
              <TableCell>
                <Badge variant={v.success ? "secondary" : "destructive"}>
                  {v.success ? "✓ 通过" : "✗ 失败"}
                </Badge>
              </TableCell>
              <TableCell className="text-slate-600">{v.steps}</TableCell>
              <TableCell className="text-slate-600">
                {v.rounds ? `${v.rounds}${v.max_steps ? `/${v.max_steps}` : ""}` : DASH}
              </TableCell>
              <TableCell className="text-slate-600">{formatTokens(v.total_tokens)}</TableCell>
              <TableCell className="max-w-md text-xs whitespace-normal text-slate-500">
                {notes.join("；")}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
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
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-4">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">Benchmark</h1>
          <p className="text-sm text-slate-500">
            历史运行与指标报告。mock provider 是离线夹具，不是真实成绩。
          </p>
        </div>
        <div className="flex items-center gap-3">
          {runs.length ? <Badge variant="outline">{runs.length} 份报告</Badge> : null}
          <Button variant="outline" size="sm" onClick={() => void refresh()}>
            <RefreshCw className="h-4 w-4" />
            刷新
          </Button>
          <Button size="sm" disabled={busy} onClick={() => void startMockRun()}>
            <Play className="h-4 w-4" />
            {busy ? "运行中…" : "跑一次（mock，离线）"}
          </Button>
        </div>
      </div>

      {error && (
        <div className="border-b border-rose-200 bg-rose-50 px-6 py-2 text-sm text-rose-700">
          {error}
        </div>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-[300px_1fr]">
        <div className="flex min-h-0 flex-col border-r border-slate-200 bg-white">
          <div className="flex items-center justify-between px-3 py-2">
            <span className="text-xs font-medium text-slate-500">历史运行</span>
            <span className="text-xs text-slate-400">{runs.length}</span>
          </div>
          <Separator />
          <ScrollArea className="min-h-0 flex-1">
            {runs.length === 0 ? (
              <div className="p-4 text-sm text-slate-500">还没有报告。点右上角跑一次。</div>
            ) : (
              <div className="flex flex-col p-2">
                {runs.map((run) => (
                  <button
                    key={run.run_id}
                    type="button"
                    onClick={() => void open(run.run_id)}
                    className={`mb-1 rounded-md p-2 text-left transition ${
                      selected?.run_id === run.run_id
                        ? "bg-slate-100 ring-1 ring-slate-300"
                        : "hover:bg-slate-50"
                    }`}
                  >
                    <div className="font-mono text-xs text-slate-700">{run.run_id}</div>
                    <div className="mt-1 flex flex-wrap items-center gap-1 text-xs text-slate-500">
                      <Badge variant="outline">{run.config?.group ?? DASH}</Badge>
                      <Badge variant="secondary">{run.config?.provider ?? "?"}</Badge>
                      <span>{formatTokens(run.metrics.tasks_total)} 条</span>
                      <span>成功率 {formatRatio(run.metrics.success_rate)}</span>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </ScrollArea>
        </div>

        <ScrollArea className="min-h-0">
          {!selected ? (
            <div className="p-6 text-sm text-slate-500">选一份报告查看指标与明细。</div>
          ) : (
            <div className="space-y-4 p-6">
              <Card>
                <CardHeader>
                  <CardTitle className="flex flex-wrap items-center gap-2 font-mono text-base">
                    {selected.run_id}
                    <Badge variant="secondary">{selected.config?.provider ?? "?"}</Badge>
                    <Badge variant="outline">{selected.config?.group ?? DASH}</Badge>
                  </CardTitle>
                  <CardDescription className="flex flex-wrap gap-3">
                    <span>模型 {selected.config?.model ?? "?"}</span>
                    <span>分组 {selected.config?.group ?? DASH}</span>
                    <span>{selected.created_at}</span>
                  </CardDescription>
                </CardHeader>
                {(selected.config?.synthetic === true ||
                  selected.config?.provider === "mock") && (
                  <CardContent>
                    <div className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">
                      mock 是离线夹具（合成事件、按任务声明直接调用工具），不是真实成绩
                      {selected.config?.synthetic !== true && "（旧报告没有 synthetic 标记）"}
                    </div>
                  </CardContent>
                )}
              </Card>

              <Tabs defaultValue="overview">
                <TabsList>
                  <TabsTrigger value="overview">概览</TabsTrigger>
                  <TabsTrigger value="verdicts">
                    逐条明细（{selected.verdicts.length}）
                  </TabsTrigger>
                </TabsList>
                <Separator className="my-4" />
                <TabsContent value="overview">
                  <Card>
                    <CardHeader>
                      <CardTitle className="text-base">指标</CardTitle>
                      <CardDescription>
                        没测到的指标显示 {DASH}（分母为 0），不是 0。
                      </CardDescription>
                    </CardHeader>
                    <CardContent>
                      <MetricsTable metrics={selected.metrics} />
                    </CardContent>
                  </Card>
                </TabsContent>
                <TabsContent value="verdicts">
                  <Card>
                    <CardHeader>
                      <CardTitle className="text-base">逐任务明细</CardTitle>
                      <CardDescription>共 {selected.verdicts.length} 条。</CardDescription>
                    </CardHeader>
                    <CardContent>
                      <VerdictTable report={selected} />
                    </CardContent>
                  </Card>
                </TabsContent>
              </Tabs>
            </div>
          )}
        </ScrollArea>
      </div>
    </div>
  );
}
