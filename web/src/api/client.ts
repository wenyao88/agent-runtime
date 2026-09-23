import type {
  BenchmarkReport,
  BenchmarkRunList,
  BenchmarkStartResponse,
  ContextSnapshot,
  MemoriesResponse,
  SkillsResponse,
  ToolsResponse,
  TraceDetailResponse,
  TraceEventsResponse,
  TracesResponse,
} from "../types";

const BASE_URL = "http://localhost:8000";

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
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

export const fetchTraceEvents = (traceId: string) =>
  apiGet<TraceEventsResponse>(`/api/traces/${encodeURIComponent(traceId)}/events`);

/** `available:false` 也是 200（后端**不装死**），原因在 body.reason 里。 */
export const fetchContext = () => apiGet<ContextSnapshot>("/api/context");

export const fetchTools = () => apiGet<ToolsResponse>("/api/tools");

export const fetchSkills = () => apiGet<SkillsResponse>("/api/skills");

export const fetchMemories = (query = "", topK = 20) =>
  apiGet<MemoriesResponse>(
    `/api/memories?query=${encodeURIComponent(query)}&top_k=${topK}&layer=all`,
  );
