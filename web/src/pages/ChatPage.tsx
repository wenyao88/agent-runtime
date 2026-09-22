import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Brain,
  ChevronDown,
  ChevronRight,
  FileText,
  Sparkles,
  Wrench,
  Zap,
} from "lucide-react";
import { WS_BASE_URL } from "../api/client";
import type { AgentStreamEvent, ChatMessage } from "../types";

const EXAMPLES = ["读取 计划.md 并总结要点", "分析 README.md 的结构"];

const MARKDOWN_CLASS = [
  "text-sm text-slate-800",
  "[&_p]:my-2",
  "[&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5",
  "[&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5",
  "[&_li]:my-0.5",
  "[&_h1]:mt-3 [&_h1]:text-lg [&_h1]:font-semibold",
  "[&_h2]:mt-3 [&_h2]:font-semibold",
  "[&_h3]:mt-2 [&_h3]:font-semibold",
  "[&_a]:text-blue-600 [&_a]:underline",
  "[&_code]:rounded [&_code]:bg-slate-100 [&_code]:px-1 [&_code]:py-0.5",
  "[&_pre]:my-2 [&_pre]:overflow-auto [&_pre]:rounded-lg [&_pre]:bg-slate-900 [&_pre]:p-3 [&_pre]:text-slate-100",
  "[&_pre_code]:bg-transparent [&_pre_code]:p-0",
  "[&_blockquote]:border-l-4 [&_blockquote]:border-slate-300 [&_blockquote]:pl-3 [&_blockquote]:text-slate-600",
  "[&_table]:my-2 [&_table]:w-full [&_table]:border-collapse",
  "[&_th]:border [&_th]:border-slate-200 [&_th]:bg-slate-50 [&_th]:px-2 [&_th]:py-1 [&_th]:text-left",
  "[&_td]:border [&_td]:border-slate-200 [&_td]:px-2 [&_td]:py-1",
].join(" ");

function truncate(text: string, max = 160): string {
  const oneLine = text.replace(/\s+/g, " ").trim();
  return oneLine.length > max ? `${oneLine.slice(0, max)}…` : oneLine;
}

function stepCount(events: AgentStreamEvent[]): number {
  const steps = events
    .map((e) => e.data?.step)
    .filter((s): s is number => typeof s === "number");
  return new Set(steps).size;
}

function toolCallCount(events: AgentStreamEvent[]): number {
  return events.filter((e) => e.event_type === "tool_call").length;
}

function Row({
  icon,
  label,
  mono,
  tone,
}: {
  icon: ReactNode;
  label: string;
  mono?: boolean;
  tone?: "ok" | "err";
}) {
  const color = tone === "err" ? "text-red-600" : tone === "ok" ? "text-slate-700" : "text-slate-600";
  return (
    <li className="flex items-start gap-2 px-3 py-2 text-xs">
      <span className="mt-0.5 shrink-0">{icon}</span>
      <span className={`min-w-0 flex-1 break-words ${mono ? "font-mono" : ""} ${color}`}>
        {label}
      </span>
    </li>
  );
}

function EventRow({ ev }: { ev: AgentStreamEvent }) {
  const d = ev.data ?? {};
  switch (ev.event_type) {
    case "step_start":
      return <Row icon={<Zap className="h-3.5 w-3.5 text-slate-400" />} label={`Step ${d.step}`} />;
    case "thought":
      return (
        <Row
          icon={<Brain className="h-3.5 w-3.5 text-violet-500" />}
          label={truncate(String(d.content || "(无思考文本)"), 300)}
        />
      );
    case "tool_call":
      return (
        <Row
          icon={<Wrench className="h-3.5 w-3.5 text-blue-500" />}
          label={`${d.tool}(${truncate(String(d.args ?? ""), 200)})`}
          mono
        />
      );
    case "tool_result":
      return (
        <Row
          icon={
            <FileText
              className={`h-3.5 w-3.5 ${d.success ? "text-emerald-500" : "text-red-500"}`}
            />
          }
          label={`${d.success ? "成功" : "失败"} · ${d.latency_ms ?? 0}ms · ${truncate(String(d.result ?? ""))}`}
          tone={d.success ? "ok" : "err"}
        />
      );
    case "compaction":
      return (
        <Row
          icon={<Zap className="h-3.5 w-3.5 text-amber-500" />}
          label={`上下文压缩 ${d.before} → ${d.after} tokens（${d.strategy}）`}
        />
      );
    case "final_answer":
      return (
        <Row icon={<Sparkles className="h-3.5 w-3.5 text-emerald-500" />} label="生成最终答案" />
      );
    case "error":
      return (
        <Row
          icon={<FileText className="h-3.5 w-3.5 text-red-500" />}
          label={String(d.message ?? "执行出错")}
          tone="err"
        />
      );
    default:
      return null;
  }
}

function ExecutionCard({ message }: { message: ChatMessage }) {
  const [open, setOpen] = useState(true);
  const { events, streaming } = message;
  if (events.length === 0 && !streaming) return null;

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-slate-600 hover:bg-slate-50"
      >
        {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        <span className="font-medium text-slate-700">执行过程</span>
        <span className="text-slate-400">
          {stepCount(events)} 步 · {toolCallCount(events)} 次工具调用
        </span>
        {streaming && (
          <span className="ml-auto flex items-center gap-1 text-blue-600">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />
            运行中
          </span>
        )}
      </button>
      {open && (
        <ul className="divide-y divide-slate-100 border-t border-slate-100">
          {events.map((ev, i) => (
            <EventRow key={i} ev={ev} />
          ))}
        </ul>
      )}
    </div>
  );
}

export default function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => () => socketRef.current?.close(), []);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const patchLastAssistant = (fn: (m: ChatMessage) => ChatMessage) =>
    setMessages((prev) => {
      const next = [...prev];
      const i = next.length - 1;
      if (i >= 0 && next[i].role === "assistant") next[i] = fn(next[i]);
      return next;
    });

  function send() {
    const task = input.trim();
    if (!task || streaming) return;
    setInput("");
    setStreaming(true);
    setMessages((prev) => [
      ...prev,
      { role: "user", content: task, events: [] },
      {
        role: "assistant",
        content: "",
        events: [],
        streaming: true,
        warning: null,
        error: null,
      },
    ]);

    const ws = new WebSocket(`${WS_BASE_URL}/ws/agent/${crypto.randomUUID()}`);
    socketRef.current = ws;

    ws.onopen = () => ws.send(JSON.stringify({ type: "task", task }));

    ws.onmessage = (event) => {
      let frame: AgentStreamEvent;
      try {
        frame = JSON.parse(String(event.data)) as AgentStreamEvent;
      } catch {
        return;
      }

      if (frame.event_type === "final_answer") {
        const content = String(frame.data?.content ?? "");
        patchLastAssistant((m) => ({ ...m, content, events: [...m.events, frame] }));
        return;
      }
      if (frame.event_type === "done") {
        const warning = (frame.data?.warning ?? null) as string | null;
        patchLastAssistant((m) => ({ ...m, streaming: false, warning }));
        setStreaming(false);
        ws.close();
        return;
      }
      patchLastAssistant((m) => ({ ...m, events: [...m.events, frame] }));
    };

    ws.onerror = () => {
      patchLastAssistant((m) => ({
        ...m,
        streaming: false,
        error: "WebSocket 连接失败：请确认后端已在 http://localhost:8000 运行。",
      }));
      setStreaming(false);
    };

    ws.onclose = () => {
      patchLastAssistant((m) =>
        m.streaming ? { ...m, streaming: false, error: m.error ?? "连接已关闭" } : m
      );
      setStreaming(false);
    };
  }

  return (
    <div className="flex h-full flex-col">
      <div ref={listRef} className="flex-1 space-y-4 overflow-auto p-6">
        {messages.length === 0 && (
          <div className="mx-auto mt-16 max-w-xl text-center text-slate-500">
            <Sparkles className="mx-auto mb-3 h-8 w-8 text-slate-400" />
            <p className="mb-4 text-sm">
              输入一个任务，Agent 会思考、调用工具，并实时展示每一步执行过程。
            </p>
            <div className="flex flex-wrap justify-center gap-2">
              {EXAMPLES.map((ex) => (
                <button
                  key={ex}
                  type="button"
                  onClick={() => setInput(ex)}
                  className="rounded-full border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600 hover:border-slate-400"
                >
                  {ex}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="flex justify-end">
              <div className="max-w-[75%] whitespace-pre-wrap rounded-2xl bg-slate-900 px-4 py-2 text-sm text-white">
                {m.content}
              </div>
            </div>
          ) : (
            <div key={i} className="flex justify-start">
              <div className="w-full max-w-[85%] space-y-2">
                <ExecutionCard message={m} />

                {m.error && (
                  <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                    {m.error}
                  </div>
                )}
                {m.warning && (
                  <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
                    ⚠ {m.warning}
                  </div>
                )}

                {m.content && (
                  <div className={`rounded-2xl border border-slate-200 bg-white px-4 py-3 ${MARKDOWN_CLASS}`}>
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
                  </div>
                )}

                {m.streaming && !m.content && (
                  <div className="flex items-center gap-2 text-xs text-slate-500">
                    <span className="h-2 w-2 animate-pulse rounded-full bg-blue-500" />
                    思考中…
                  </div>
                )}
              </div>
            </div>
          )
        )}
      </div>

      <div className="border-t border-slate-200 bg-white p-4">
        <div className="mx-auto flex max-w-3xl items-end gap-2">
          <textarea
            value={input}
            rows={2}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder="描述你的任务…（Enter 发送，Shift+Enter 换行）"
            className="flex-1 resize-none rounded-xl border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
          />
          <button
            type="button"
            onClick={send}
            disabled={streaming || !input.trim()}
            className="rounded-xl bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {streaming ? "执行中…" : "发送"}
          </button>
        </div>
      </div>
    </div>
  );
}
