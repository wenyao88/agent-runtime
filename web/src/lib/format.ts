/** 展示层格式化（Trace / Inspector / Benchmark 共用）。
 *
 * 两条口径：
 *   * `None` ≠ `0`：缺失字段（旧报告、可选能力没开）一律显示 `—`，**绝不显示成 0**；
 *   * 数字给人看：token 加千分位，毫秒过 1 秒换单位，比例转百分比。
 */

export const DASH = "—";

/** 缺失/无效 → `—`；有真值 → 原样字符串。 */
export function dash(value: unknown): string {
  if (value === null || value === undefined || value === "") return DASH;
  return String(value);
}

export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return DASH;
  return value.toFixed(digits);
}

export function formatTokens(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return DASH;
  return value.toLocaleString("en-US");
}

export function formatMs(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return DASH;
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

export function formatRatio(value: number | null | undefined, digits = 1): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return DASH;
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return DASH;
  return date.toLocaleString("zh-CN", { hour12: false });
}
