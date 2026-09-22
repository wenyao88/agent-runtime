from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # LLM
    llm_provider: str = "openai_compatible"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.siliconflow.cn/v1"
    llm_model: str = "deepseek-ai/DeepSeek-V3"
    llm_max_tokens: int = 128000

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

    # GitHub
    github_token: str = ""

    # Web search
    web_search_provider: str = "duckduckgo"  # duckduckgo（免 key）| tavily（需 key）
    web_search_api_key: str = ""

    # Tools
    tool_http_timeout_seconds: float = 20.0
    tool_max_chars: int = 4000

    # MCP（可选能力：文件缺失即视为不启用，不影响启动）
    mcp_servers_file: str = "mcp_servers.json"
