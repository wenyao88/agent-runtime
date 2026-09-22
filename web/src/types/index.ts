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
