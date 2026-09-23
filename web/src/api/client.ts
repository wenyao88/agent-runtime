import type {
  BenchmarkReport,
  BenchmarkRunList,
  BenchmarkStartResponse,
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
