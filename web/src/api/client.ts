import type {
  BenchmarkReport,
  BenchmarkRunList,
  BenchmarkStartResponse,
  ContextSnapshot,
  MemoriesResponse,
  SkillsResponse,
  ToolsResponse,
  TraceDetailResponse,
  TracesResponse,
} from "../types";

const BASE_URL = "http://localhost:8000";

/** 把后端的 `detail` 当成错误信息（审查 M1）。
 *
 * 后端把"为什么失败"写在响应体的 `detail` 里（例如 trace 404 给的是
 * "没有这条 trace：xxx（默认内存存储重启即丢…）"）。只抛 `404 Not Found` 等于把原因丢掉，
 * 页面上就只剩一句没用的状态码。不是 JSON（网关/代理错误页）时退回状态行。
 */
async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body?.detail === "string" && body.detail) return body.detail;
  } catch {
    // 响应体不是 JSON：用状态行兜底
  }
  return `${res.status} ${res.statusText}`;
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`);
  if (!res.ok) throw new Error(await readError(res));
  return res.json() as Promise<T>;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json() as Promise<T>;
}

export const WS_BASE_URL = "ws://localhost:8000";

// ── Benchmark（Phase 6）──

export const fetchBenchmarks = () => apiGet<BenchmarkRunList>("/api/benchmarks");

export const fetchBenchmark = (runId: string) =>
  apiGet<BenchmarkReport>(`/api/benchmarks/${encodeURIComponent(runId)}`);

export const startBenchmarkRun = (body: {
  provider: "mock" | "real";
  limit?: number | null;
  judge?: number;
}) => apiPost<BenchmarkStartResponse>("/api/benchmarks/run", body);

// ── Trace / Context / 目录（Phase 8）──

export const fetchTraces = (limit = 50) =>
  apiGet<TracesResponse>(`/api/traces?limit=${limit}`);

export const fetchTrace = (traceId: string) =>
  apiGet<TraceDetailResponse>(`/api/traces/${encodeURIComponent(traceId)}`);

/** `available:false` 也是 200（后端**不装死**），原因在 body.reason 里。 */
export const fetchContext = () => apiGet<ContextSnapshot>("/api/context");

export const fetchTools = () => apiGet<ToolsResponse>("/api/tools");

export const fetchSkills = () => apiGet<SkillsResponse>("/api/skills");

export const fetchMemories = (query = "", topK = 20) =>
  apiGet<MemoriesResponse>(
    `/api/memories?query=${encodeURIComponent(query)}&top_k=${topK}&layer=all`,
  );
