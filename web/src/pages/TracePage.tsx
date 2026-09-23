import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { fetchTrace, fetchTraces } from "../api/client";
import type { TraceDetail, TraceEventView, TraceStepView, TraceSummary, TracesResponse } from "../types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { DASH, formatMs, formatTokens, formatTime } from "@/lib/format";

const SOURCE_ALL = "all";

/** 一步里可能有多个工具调用/结果（一轮多个并行调用），统一折成数组再渲染。 */
function asArray<T>(value: T | T[] | null | undefined): T[] {
  if (value === null || value === undefined) return [];
  return Array.isArray(value) ? value : [value];
}

function sourceVariant(source: string): "default" | "secondary" | "outline" {
  if (source === "benchmark") return "secondary";
  if (source === "chat") return "default";
  return "outline";
}

function statusVariant(status: string): "default" | "secondary" | "destructive" | "outline" {
  if (status === "finished") return "secondary";
  if (status === "running") return "outline";
  return "destructive";
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-xs text-slate-500">{label}</span>
      <span className="text-sm font-medium text-slate-800">{value}</span>
    </div>
  );
}

function StepRow({
  step,
  open,
  onToggle,
}: {
  step: TraceStepView;
  open: boolean;
  onToggle: () => void;
}) {
  const calls = asArray(step.tool_call);
  const results = asArray(step.tool_result);
  const toolName = calls[0]?.tool_name ?? results[0]?.tool_name;
  const failed = results.some((result) => result.success === false);

  return (
    <Collapsible open={open} onOpenChange={onToggle}>
      <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left hover:bg-slate-50">
        {open ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-slate-400" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-slate-400" />
        )}
        <span className="w-16 shrink-0 text-xs text-slate-500">Step {step.step_number}</span>
        <span className="min-w-0 flex-1 truncate text-sm text-slate-800">
          {step.thought ? step.thought.replace(/\s+/g, " ") : "（无思考文本）"}
        </span>
        {toolName ? <Badge variant="outline">{toolName}</Badge> : null}
        {failed ? <Badge variant="destructive">失败</Badge> : null}
        <span className="shrink-0 text-xs text-slate-500">{formatMs(step.latency_ms)}</span>
      </CollapsibleTrigger>
      <CollapsibleContent className="space-y-3 px-2 pb-4 pl-8">
        {step.thought ? (
          <div>
            <div className="mb-1 text-xs font-medium text-slate-500">思考</div>
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-slate-50 p-2 text-xs text-slate-800">
              {step.thought}
            </pre>
          </div>
        ) : null}

        {calls.map((call, index) => (
          <div key={`call-${index}`}>
            <div className="mb-1 text-xs font-medium text-slate-500">
              工具调用 {call.tool_name ?? DASH}
            </div>
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-slate-50 p-2 text-xs text-slate-800">
              {JSON.stringify(call.args ?? {}, null, 2)}
            </pre>
          </div>
        ))}

        {results.map((result, index) => (
          <div key={`result-${index}`}>
            <div className="mb-1 flex items-center gap-2 text-xs font-medium text-slate-500">
              <span>工具结果 {result.tool_name ?? DASH}</span>
              <Badge variant={result.success === false ? "destructive" : "secondary"}>
                {result.success === false ? "失败" : "成功"}
              </Badge>
              <span>{formatTokens(result.chars)} 字符</span>
              <span>{formatMs(result.latency_ms)}</span>
            </div>
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-slate-50 p-2 text-xs text-slate-800">
              {result.text || "（空）"}
            </pre>
          </div>
        ))}

        <div className="flex gap-4 text-xs text-slate-500">
          <span>prompt {formatTokens(step.token_usage?.prompt_tokens)}</span>
          <span>completion {formatTokens(step.token_usage?.completion_tokens)}</span>
          <span>合计 {formatTokens(step.token_usage?.total_tokens)}</span>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function EventLine({ event }: { event: TraceEventView }) {
  const data = event.data ?? {};
  if (event.event_type === "compaction") {
    return (
      <div className="flex items-center gap-2 text-xs text-slate-600">
        <Badge variant="secondary">压缩</Badge>
        <span>
          {String(data.strategy ?? DASH)}：{formatTokens(data.tokens_before)} →{" "}
          {formatTokens(data.tokens_after)}
        </span>
      </div>
    );
  }
  if (event.event_type === "error") {
    return (
      <div className="flex items-center gap-2 text-xs text-red-700">
        <Badge variant="destructive">错误</Badge>
        <span>
          {String(data.error_type ?? DASH)}：{String(data.message ?? "")}
        </span>
      </div>
    );
  }
  return null;
}

export default function TracePage() {
  const [data, setData] = useState<TracesResponse | null>(null);
  const [listError, setListError] = useState("");
  const [loading, setLoading] = useState(false);
  const [source, setSource] = useState(SOURCE_ALL);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [detailReason, setDetailReason] = useState("");
  const [openStep, setOpenStep] = useState<number | null>(null);

  async function selectTrace(traceId: string) {
    setSelectedId(traceId);
    setOpenStep(null);
    try {
      const body = await fetchTrace(traceId);
      if (!body.available || !body.trace) {
        setDetail(null);
        // 兜底分支：路由对"没有这条 trace"回 404，`apiGet` 会把后端的 detail 当错误信息抛出来，
        // 于是正常路径走下面的 catch；这里留着是为了"服务端愿意回 200 + available:false"的情况
        // （`available` 语义就是这么定的），两条路都展示人读得懂的原因。
        setDetailReason(body.reason || "这条 trace 不可用");
        return;
      }
      setDetail(body.trace);
      setDetailReason("");
      setOpenStep(body.trace.steps[0]?.step_number ?? null);
    } catch (error) {
      setDetail(null);
      setDetailReason(String(error));
    }
  }

  async function loadList(keepId = "") {
    setLoading(true);
    try {
      const body = await fetchTraces(100);
      setData(body);
      setListError("");
      const next = body.traces.find((item) => item.trace_id === keepId) ?? body.traces[0];
      if (next) {
        await selectTrace(next.trace_id);
      } else {
        setSelectedId("");
        setDetail(null);
        setDetailReason("");
      }
    } catch (error) {
      setData(null);
      setListError(String(error));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadList();
    // 只在进入页面时拉一次：本阶段不做实时推送（spec §7 天花板）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const sources = Array.from(new Set((data?.traces ?? []).map((item) => item.source)));
  const visible: TraceSummary[] = (data?.traces ?? []).filter(
    (item) => source === SOURCE_ALL || item.source === source,
  );
  const events = (detail?.events ?? []).filter(
    (event) => event.event_type === "compaction" || event.event_type === "error",
  );

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-4">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">Trace</h1>
          <p className="text-sm text-slate-500">
            每次会话的步骤树与事件日志。默认内存存储，重启即丢；聊天与评测分别带来源标签。
          </p>
        </div>
        <div className="flex items-center gap-3">
          {data ? (
            <Badge variant="outline">{data.settings.store}</Badge>
          ) : null}
          <Button variant="outline" size="sm" onClick={() => void loadList(selectedId)} disabled={loading}>
            <RefreshCw className={loading ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
            刷新
          </Button>
        </div>
      </div>

      {listError ? (
        <div className="border-b border-red-200 bg-red-50 px-6 py-2 text-sm text-red-700">
          拉取 trace 列表失败：{listError}
        </div>
      ) : null}
      {data?.errors?.length ? (
        <div className="border-b border-amber-200 bg-amber-50 px-6 py-2 text-sm text-amber-800">
          {data.errors.join("；")}
        </div>
      ) : null}
      {data?.settings?.errors?.length ? (
        <div className="border-b border-amber-200 bg-amber-50 px-6 py-2 text-sm text-amber-800">
          存储装配问题：{data.settings.errors.join("；")}
        </div>
      ) : null}

      <div className="grid min-h-0 flex-1 grid-cols-[320px_1fr]">
        <div className="flex min-h-0 flex-col border-r border-slate-200 bg-white">
          <div className="flex items-center justify-between gap-2 px-3 py-2">
            <span className="text-xs text-slate-500">{visible.length} 条</span>
            <Select value={source} onValueChange={setSource}>
              <SelectTrigger className="h-8 w-32">
                <SelectValue placeholder="来源" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SOURCE_ALL}>全部来源</SelectItem>
                {sources.map((item) => (
                  <SelectItem key={item} value={item}>
                    {item}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Separator />
          <ScrollArea className="min-h-0 flex-1">
            {visible.length === 0 ? (
              <div className="p-4 text-sm text-slate-500">
                还没有 trace。去 Chat 页发一句话，或跑一次评测（Benchmark 页）再回来。
              </div>
            ) : (
              <div className="flex flex-col p-2">
                {visible.map((item) => (
                  <button
                    key={item.trace_id}
                    onClick={() => void selectTrace(item.trace_id)}
                    className={`mb-1 rounded-md p-2 text-left transition ${
                      item.trace_id === selectedId
                        ? "bg-slate-100 ring-1 ring-slate-300"
                        : "hover:bg-slate-50"
                    }`}
                  >
                    <div className="mb-1 flex items-center gap-1">
                      <Badge variant={sourceVariant(item.source)}>{item.source}</Badge>
                      <Badge variant={statusVariant(item.status)}>{item.status}</Badge>
                      <span className="ml-auto text-xs text-slate-400">
                        {formatTime(item.started_at)}
                      </span>
                    </div>
                    <div className="line-clamp-2 text-sm text-slate-800">{item.task}</div>
                    <div className="mt-1 flex gap-3 text-xs text-slate-500">
                      <span>{item.steps} 步</span>
                      <span>{formatTokens(item.total_tokens)} tokens</span>
                      <span>{formatMs(item.total_latency_ms)}</span>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </ScrollArea>
        </div>

        <ScrollArea className="min-h-0">
          {detailReason ? (
            <div className="p-6 text-sm text-slate-600">{detailReason}</div>
          ) : !detail ? (
            <div className="p-6 text-sm text-slate-500">左侧选一条 trace 查看步骤与事件。</div>
          ) : (
            <div className="space-y-4 p-6">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">{detail.task}</CardTitle>
                  <CardDescription className="flex flex-wrap items-center gap-2">
                    <Badge variant={sourceVariant(detail.source)}>{detail.source}</Badge>
                    <Badge variant={statusVariant(detail.status)}>{detail.status}</Badge>
                    <span className="font-mono text-xs">{detail.trace_id}</span>
                  </CardDescription>
                </CardHeader>
                <CardContent className="grid grid-cols-2 gap-3 md:grid-cols-4">
                  <Meta label="步骤" value={String(detail.steps.length)} />
                  <Meta label="总 token" value={formatTokens(detail.total_tokens?.total_tokens)} />
                  <Meta label="总耗时" value={formatMs(detail.total_latency_ms)} />
                  <Meta label="事件数" value={String(detail.events.length)} />
                  <Meta label="开始" value={formatTime(detail.started_at)} />
                  <Meta label="结束" value={formatTime(detail.finished_at)} />
                  <Meta label="prompt" value={formatTokens(detail.total_tokens?.prompt_tokens)} />
                  <Meta
                    label="completion"
                    value={formatTokens(detail.total_tokens?.completion_tokens)}
                  />
                </CardContent>
              </Card>

              {events.length ? (
                <Card>
                  <CardHeader>
                    <CardTitle className="text-base">压缩与告警</CardTitle>
                    <CardDescription>来自事件日志（压缩次数是真实发生的次数）。</CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-2">
                    {events.map((event, index) => (
                      <EventLine key={`${event.event_type}-${index}`} event={event} />
                    ))}
                  </CardContent>
                </Card>
              ) : null}

              <Card>
                <CardHeader>
                  <CardTitle className="text-base">步骤树</CardTitle>
                  <CardDescription>
                    {detail.steps.length
                      ? `共 ${detail.steps.length} 步，点开看思考、工具参数与结果。`
                      : "这条 trace 没有记录到步骤（多半是任务在第一步就失败了）。"}
                  </CardDescription>
                </CardHeader>
                <CardContent className="p-2">
                  {detail.steps.map((step) => (
                    <StepRow
                      key={step.step_number}
                      step={step}
                      open={openStep === step.step_number}
                      onToggle={() =>
                        setOpenStep(openStep === step.step_number ? null : step.step_number)
                      }
                    />
                  ))}
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-base">最终回答</CardTitle>
                </CardHeader>
                <CardContent>
                  <pre className="whitespace-pre-wrap break-words text-sm text-slate-800">
                    {detail.final_answer || "（没有最终回答）"}
                  </pre>
                </CardContent>
              </Card>
            </div>
          )}
        </ScrollArea>
      </div>
    </div>
  );
}
