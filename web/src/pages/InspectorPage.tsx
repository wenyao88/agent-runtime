import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { fetchContext, fetchMemories, fetchSkills, fetchTools } from "../api/client";
import type {
  ContextSnapshot,
  MemoriesResponse,
  SkillCatalogEntry,
  SkillsResponse,
  ToolCatalogEntry,
  ToolsResponse,
} from "../types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DASH, formatMs, formatRatio, formatTokens } from "@/lib/format";

/** 四段的固定配色（Tailwind 类名要写全，不能拼字符串，否则会被 purge 掉）。 */
const SECTION_STYLE: Record<string, string> = {
  system: "bg-slate-500",
  memory: "bg-violet-500",
  task: "bg-blue-500",
  messages: "bg-emerald-500",
};

function Errors({ items }: { items: string[] }) {
  if (!items.length) return null;
  return (
    <div className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">
      {items.map((item, index) => (
        <div key={index}>{item}</div>
      ))}
    </div>
  );
}

function Unavailable({ reason, errors }: { reason: string; errors: string[] }) {
  return (
    <div className="space-y-2">
      <div className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-600">
        {reason || "暂时拿不到上下文。"}
      </div>
      <Errors items={errors} />
    </div>
  );
}

function ContextPanel({ snapshot }: { snapshot: ContextSnapshot | null }) {
  if (!snapshot) return <div className="text-sm text-slate-500">加载中…</div>;
  if (!snapshot.available) {
    return <Unavailable reason={snapshot.reason} errors={snapshot.errors ?? []} />;
  }
  const sections = snapshot.sections ?? [];
  const budget = snapshot.budget;
  const used = snapshot.used_tokens ?? 0;
  const threshold = budget?.threshold ?? 0;
  const barMax = Math.max(threshold, used, 1);
  const compaction = snapshot.last_compaction;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <div>
          <div className="text-xs text-slate-500">已用 token</div>
          <div className="text-sm font-medium">{formatTokens(used)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500">可用预算</div>
          <div className="text-sm font-medium">{formatTokens(budget?.available)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500">压缩阈值</div>
          <div className="text-sm font-medium">{formatTokens(budget?.threshold)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500">占用比</div>
          <div className="text-sm font-medium">{formatRatio(snapshot.ratio)}</div>
        </div>
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between text-xs text-slate-500">
          <span>分段占用（灰线 = 压缩阈值）</span>
          <span>{sections.length} 段</span>
        </div>
        <div className="relative h-6 w-full overflow-hidden rounded-md bg-slate-100">
          <div className="flex h-full">
            {sections.map((section) => (
              <div
                key={section.name}
                className={SECTION_STYLE[section.name] ?? "bg-slate-400"}
                style={{ width: `${(section.tokens / barMax) * 100}%` }}
                title={`${section.name}: ${section.tokens}`}
              />
            ))}
          </div>
          {threshold > 0 ? (
            <div
              className="absolute top-0 h-full w-0.5 bg-slate-700"
              style={{ left: `${Math.min(100, (threshold / barMax) * 100)}%` }}
            />
          ) : null}
        </div>
        <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-600">
          {sections.map((section) => (
            <span key={section.name} className="flex items-center gap-1">
              <span
                className={`inline-block h-2 w-2 rounded-full ${
                  SECTION_STYLE[section.name] ?? "bg-slate-400"
                }`}
              />
              {section.name}：{formatTokens(section.tokens)} tokens（{formatTokens(section.chars)} 字符）
            </span>
          ))}
        </div>
      </div>

      <div>
        <div className="mb-1 text-xs text-slate-500">最近一次压缩</div>
        {compaction ? (
          <div className="flex flex-wrap items-center gap-2 text-sm text-slate-700">
            <Badge variant={compaction.noop ? "outline" : "secondary"}>
              {compaction.strategy}
              {compaction.noop ? "（无变化）" : ""}
            </Badge>
            <span>
              {formatTokens(compaction.before)} → {formatTokens(compaction.after)} tokens
            </span>
            <span className="text-slate-500">省 {formatTokens(compaction.saved_tokens)}</span>
            <span className="text-slate-500">丢 {compaction.messages_dropped} 条</span>
            <span className="text-slate-500">压 {compaction.messages_squeezed} 条</span>
            <span className="text-slate-500">摘要 {compaction.summarized} 条</span>
            <span className="text-slate-500">
              摘要成本 {formatTokens(compaction.summarizer_tokens)} tokens /{" "}
              {formatMs(compaction.summarizer_ms)}
            </span>
            {compaction.degraded_from ? (
              <Badge variant="destructive">从 {compaction.degraded_from} 降级</Badge>
            ) : null}
          </div>
        ) : (
          <div className="text-sm text-slate-500">{DASH}（这轮还没有压缩过）</div>
        )}
        {compaction?.degraded_reason ? (
          <div className="mt-1 text-xs text-amber-700">{compaction.degraded_reason}</div>
        ) : null}
      </div>

      <Errors items={snapshot.errors ?? []} />
    </div>
  );
}

function MemoriesPanel({ data }: { data: MemoriesResponse | null }) {
  if (!data) return <div className="text-sm text-slate-500">加载中…</div>;
  const layers = [
    { key: "working", label: "工作记忆", enabled: true, note: "进程内，始终开启" },
    {
      key: "short_term",
      label: "短时记忆（Redis）",
      enabled: data.enabled?.short_term ?? false,
      note: "按会话隔离",
    },
    {
      key: "long_term",
      label: "长期记忆（PG + 向量）",
      enabled: data.enabled?.long_term ?? false,
      note: "跨会话",
    },
  ];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-3">
        {layers.map((layer) => (
          <div key={layer.key} className="rounded-md border border-slate-200 p-3">
            <div className="flex items-center gap-2">
              <Badge variant={layer.enabled ? "secondary" : "outline"}>
                {layer.enabled ? "已启用" : "未启用"}
              </Badge>
              <span className="text-sm font-medium">{layer.label}</span>
            </div>
            <div className="mt-1 text-xs text-slate-500">{layer.note}</div>
          </div>
        ))}
      </div>
      <Errors items={data.settings?.errors ?? []} />
      <Errors items={data.errors ?? []} />
      <div>
        <div className="mb-2 text-xs text-slate-500">最近条目（{data.count} 条）</div>
        {data.memories?.length ? (
          <div className="space-y-2">
            {data.memories.map((entry) => (
              <div key={entry.id} className="rounded-md border border-slate-200 p-2">
                <div className="mb-1 flex items-center gap-2 text-xs text-slate-500">
                  <Badge variant="outline">{entry.source || DASH}</Badge>
                  <span>{entry.role}</span>
                </div>
                <div className="whitespace-pre-wrap break-words text-sm text-slate-800">
                  {entry.content}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-sm text-slate-500">
            没有召回到记忆。默认三层记忆是关的（MEMORY_* 开关），关了就是空 —— 不是坏了。
          </div>
        )}
      </div>
    </div>
  );
}

function ToolRow({ tool }: { tool: ToolCatalogEntry }) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="border-b border-slate-100 py-1">
      <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left hover:bg-slate-50">
        {open ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-slate-400" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-slate-400" />
        )}
        <span className="w-40 shrink-0 font-mono text-xs text-slate-700">{tool.name}</span>
        <span className="min-w-0 flex-1 truncate text-sm text-slate-600">
          {tool.description}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="px-2 pb-2 pl-8">
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md bg-slate-50 p-2 text-xs text-slate-800">
          {JSON.stringify(tool.parameters ?? {}, null, 2)}
        </pre>
      </CollapsibleContent>
    </Collapsible>
  );
}

function ToolsPanel({ data }: { data: ToolsResponse | null }) {
  if (!data) return <div className="text-sm text-slate-500">加载中…</div>;
  return (
    <div>
      <div className="mb-2 text-xs text-slate-500">{data.count} 个工具（原生 + MCP）</div>
      <div>
        {data.tools.map((tool) => (
          <ToolRow key={tool.name} tool={tool} />
        ))}
      </div>
    </div>
  );
}

function SkillRow({ skill }: { skill: SkillCatalogEntry }) {
  return (
    <div className="border-b border-slate-100 py-2">
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs text-slate-700">{skill.name}</span>
        <Badge variant="outline">v{skill.version}</Badge>
        {skill.tags.map((tag) => (
          <Badge key={tag} variant="secondary">
            {tag}
          </Badge>
        ))}
      </div>
      <div className="mt-1 text-sm text-slate-600">{skill.description}</div>
      <div className="mt-1 flex flex-wrap gap-1 text-xs text-slate-500">
        <span>触发词：</span>
        {skill.triggers.length ? (
          skill.triggers.map((trigger) => (
            <span key={trigger} className="rounded bg-slate-100 px-1">
              {trigger}
            </span>
          ))
        ) : (
          <span>{DASH}</span>
        )}
      </div>
      <div className="mt-1 text-xs text-slate-500">
        依赖工具：{skill.required_tools.length ? skill.required_tools.join("、") : DASH}
      </div>
    </div>
  );
}

function SkillsPanel({ data }: { data: SkillsResponse | null }) {
  if (!data) return <div className="text-sm text-slate-500">加载中…</div>;
  return (
    <div>
      <div className="mb-2 text-xs text-slate-500">{data.count} 个技能</div>
      {data.skills.map((skill) => (
        <SkillRow key={skill.name} skill={skill} />
      ))}
    </div>
  );
}

export default function InspectorPage() {
  const [snapshot, setSnapshot] = useState<ContextSnapshot | null>(null);
  const [memories, setMemories] = useState<MemoriesResponse | null>(null);
  const [tools, setTools] = useState<ToolsResponse | null>(null);
  const [skills, setSkills] = useState<SkillsResponse | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);

  async function loadAll() {
    setLoading(true);
    const problems: string[] = [];
    // 四块互相独立：一块挂了不该让另外三块空白（所以逐块 catch，而不是 Promise.all 一把梭）
    const [contextResult, memoryResult, toolResult, skillResult] = await Promise.allSettled([
      fetchContext(),
      fetchMemories(),
      fetchTools(),
      fetchSkills(),
    ]);
    if (contextResult.status === "fulfilled") setSnapshot(contextResult.value);
    else problems.push(`上下文：${contextResult.reason}`);

    if (memoryResult.status === "fulfilled") setMemories(memoryResult.value);
    else problems.push(`记忆：${memoryResult.reason}`);

    if (toolResult.status === "fulfilled") setTools(toolResult.value);
    else problems.push(`工具：${toolResult.reason}`);

    if (skillResult.status === "fulfilled") setSkills(skillResult.value);
    else problems.push(`技能：${skillResult.reason}`);

    setErrors(problems);
    setLoading(false);
  }

  useEffect(() => {
    void loadAll();
    // 只在进入页面时拉一次（本阶段不做实时刷新，spec §7）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-4">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">Inspector</h1>
          <p className="text-sm text-slate-500">
            当前上下文、三层记忆、已注册工具与已加载技能。数据来自后端既有端点，不额外造口径。
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void loadAll()} disabled={loading}>
          <RefreshCw className={loading ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
          刷新
        </Button>
      </div>

      {errors.length ? (
        <div className="border-b border-amber-200 bg-amber-50 px-6 py-2 text-sm text-amber-800">
          {errors.join("；")}
        </div>
      ) : null}

      <ScrollArea className="min-h-0 flex-1">
        <div className="p-6">
          <Tabs defaultValue="context">
            <TabsList>
              <TabsTrigger value="context">上下文</TabsTrigger>
              <TabsTrigger value="memory">记忆</TabsTrigger>
              <TabsTrigger value="tools">工具</TabsTrigger>
              <TabsTrigger value="skills">技能</TabsTrigger>
            </TabsList>
            <Separator className="my-4" />
            <TabsContent value="context">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">当前上下文</CardTitle>
                  <CardDescription>
                    最近一次聊天会话的分段 token 分布。评测 agent 的上下文不进这里。
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <ContextPanel snapshot={snapshot} />
                </CardContent>
              </Card>
            </TabsContent>
            <TabsContent value="memory">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">三层记忆</CardTitle>
                  <CardDescription>未启用的层显示"未启用"，不是错误。</CardDescription>
                </CardHeader>
                <CardContent>
                  <MemoriesPanel data={memories} />
                </CardContent>
              </Card>
            </TabsContent>
            <TabsContent value="tools">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">工具目录</CardTitle>
                  <CardDescription>原生工具与 MCP 工具形态一致，点开看参数 schema。</CardDescription>
                </CardHeader>
                <CardContent>
                  <ToolsPanel data={tools} />
                </CardContent>
              </Card>
            </TabsContent>
            <TabsContent value="skills">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">技能目录</CardTitle>
                  <CardDescription>已加载的 SOP 与触发词。</CardDescription>
                </CardHeader>
                <CardContent>
                  <SkillsPanel data={skills} />
                </CardContent>
              </Card>
            </TabsContent>
          </Tabs>
        </div>
      </ScrollArea>
    </div>
  );
}
