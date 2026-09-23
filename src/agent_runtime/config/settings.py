from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # LLM
    llm_provider: str = "openai_compatible"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.siliconflow.cn/v1"
    llm_model: str = "deepseek-ai/DeepSeek-V3"
    llm_max_tokens: int = 128000
    llm_temperature: float = 0.2  # 工具型 Agent 宜低：偏高的默认值会加重编造

    # Judge LLM (optional)
    judge_llm_provider: str = "openai_compatible"
    judge_llm_api_key: str = ""
    judge_llm_base_url: str = "https://api.siliconflow.cn/v1"
    judge_llm_model: str = "qwen/qwen2.5-72b-instruct"

    # Embedding
    embedding_provider: str = "openai_compatible"
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-large-zh-v1.5"

    # Database
    database_url: str = "postgresql+asyncpg://agent:agent@localhost:5432/agent_runtime"
    redis_url: str = "redis://localhost:6379/0"

    # Agent
    agent_max_steps: int = 15
    agent_tool_timeout_seconds: int = 30
    agent_context_compaction_threshold: float = 0.8
    agent_compaction_summarize_enabled: bool = False  # SUMMARIZE 压缩：额外一次 LLM 调用，默认关

    # GitHub
    github_token: str = ""

    # Web search
    web_search_provider: str = "duckduckgo"  # duckduckgo（免 key）| tavily（需 key）
    web_search_api_key: str = ""

    # Tools
    tool_http_timeout_seconds: float = 20.0
    tool_max_chars: int = 4000

    # Skills（可选能力：目录缺失只记错误，不影响启动）
    skills_dir: str = "skills"  # 相对路径按项目根解析（不按 CWD）
    agent_skill_top_k: int = 1  # 每次最多注入几个命中的技能
    agent_task_planning_enabled: bool = False  # TaskPlanner 默认关闭（额外一次 LLM 调用）

    # Memory（Phase 4 三层记忆；**默认全关** —— 没起 DB/Redis 也不得影响启动与任务）
    memory_short_term_enabled: bool = False  # Redis 会话级记忆
    memory_long_term_enabled: bool = False  # PG + pgvector 跨会话语义记忆
    memory_consolidate_enabled: bool = False  # 任务结束是否整理写入持久层
    memory_recall_top_k: int = 3  # 注入 System Prompt 的召回条数上限
    memory_short_term_ttl_seconds: int = 86400
    memory_short_term_max_items: int = 200
    memory_inject_max_chars: int = 500  # 单条记忆注入长度（超出带标记截断）
    memory_embedding_dim: int = 1024  # 必须与迁移里的 vector(1024) 一致

    # MCP（可选能力：文件缺失即视为不启用，不影响启动）
    mcp_servers_file: str = "mcp_servers.json"
