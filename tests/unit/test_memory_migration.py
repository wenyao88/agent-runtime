"""Alembic 迁移的内容契约（沙箱内不能真跑迁移，但能钉死"迁移里必须有什么"）。

为什么值得测：迁移是本阶段**唯一只能在用户机器上验证**的东西。如果连"文件里有没有
`CREATE EXTENSION vector`、维度是不是 1024、索引类型对不对"都不检查，那就等于整条建表路径零验证。
本文件把可静态验证的部分全部钉死，剩下的（真跑 `alembic upgrade head`）在 ledger 里如实标注为待用户验证。

重点：**维度一致性**。`models.py`、迁移里的 `vector(1024)`、`MEMORY_EMBEDDING_DIM` 三处必须同一个数，
漂移的后果是"写库时才报维度错误"，而且换模型时最容易漏改其中一处。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

MIGRATION = _ROOT / "alembic" / "versions" / "0001_memory_entries.py"
ENV_PY = _ROOT / "alembic" / "env.py"
ALEMBIC_INI = _ROOT / "alembic.ini"
MODELS = _ROOT / "src" / "agent_runtime" / "infrastructure" / "db" / "models.py"
SETTINGS = _ROOT / "src" / "agent_runtime" / "config" / "settings.py"

EXPECTED_DIM = 1024


def _migration_text() -> str:
    return MIGRATION.read_text(encoding="utf-8")


# ── 文件存在与语法 ──


def test_migration_files_exist() -> None:
    for path in (MIGRATION, ENV_PY, ALEMBIC_INI):
        assert path.exists(), f"缺少 {path.name}"


def test_migration_is_valid_python() -> None:
    ast.parse(_migration_text())


def test_migration_has_revision_wiring() -> None:
    text = _migration_text()
    assert re.search(r'^revision\s*[:=]', text, re.M), "缺少 revision"
    assert "down_revision" in text, "缺少 down_revision（首版应为 None）"


# ── 迁移内容 ──


def test_migration_creates_the_vector_extension_first() -> None:
    """纯净 pgvector 镜像上不先建扩展，第一次 upgrade 就会失败。"""
    assert "CREATE EXTENSION IF NOT EXISTS vector" in _migration_text()


def test_migration_creates_expected_table_and_columns() -> None:
    text = _migration_text()
    assert "memory_entries" in text
    for column in ("content", "role", "meta", "embedding", "session_id", "created_at"):
        assert f'"{column}"' in text or f"'{column}'" in text, f"缺少列 {column}"


def test_migration_declares_the_fixed_dimension() -> None:
    assert f"Vector({EXPECTED_DIM})" in _migration_text()


def test_migration_creates_all_three_indexes() -> None:
    text = _migration_text()
    assert "ix_memory_entries_embedding" in text
    assert "ivfflat" in text
    assert "vector_cosine_ops" in text, "索引与查询的距离算子必须一致（都用余弦）"
    assert "ix_memory_entries_created_at" in text
    assert "ix_memory_entries_session" in text


def test_migration_can_be_reverted() -> None:
    text = _migration_text()
    assert "def downgrade" in text
    assert "drop_table" in text


# ── 维度不漂移（三处同一来源）──


def test_dimension_matches_models() -> None:
    text = MODELS.read_text(encoding="utf-8")
    match = re.search(r"EMBEDDING_DIM\s*=\s*(\d+)", text)
    assert match, "models.py 应显式声明 EMBEDDING_DIM"
    assert int(match.group(1)) == EXPECTED_DIM


def test_dimension_matches_settings_default() -> None:
    text = SETTINGS.read_text(encoding="utf-8")
    match = re.search(r"memory_embedding_dim\s*:\s*int\s*=\s*(\d+)", text)
    assert match, "settings 应显式声明 memory_embedding_dim 默认值"
    assert int(match.group(1)) == EXPECTED_DIM


def test_models_and_migration_agree_on_dimension() -> None:
    models_dim = int(re.search(r"EMBEDDING_DIM\s*=\s*(\d+)", MODELS.read_text(encoding="utf-8")).group(1))
    migration_dim = int(re.search(r"Vector\((\d+)\)", _migration_text()).group(1))
    assert models_dim == migration_dim == EXPECTED_DIM


# ── env.py / alembic.ini ──


def test_env_reads_database_url_from_environment() -> None:
    text = ENV_PY.read_text(encoding="utf-8")
    assert "DATABASE_URL" in text
    assert "os.environ" in text, "连接串必须从环境读取，不能写死在配置里"


def test_env_targets_the_models_metadata() -> None:
    text = ENV_PY.read_text(encoding="utf-8")
    assert "Base.metadata" in text, "autogenerate 需要 target_metadata 指向模型"


def test_alembic_ini_points_at_the_alembic_directory() -> None:
    text = ALEMBIC_INI.read_text(encoding="utf-8")
    assert re.search(r"^script_location\s*=\s*alembic\s*$", text, re.M)


def test_alembic_ini_has_no_hardcoded_password() -> None:
    text = ALEMBIC_INI.read_text(encoding="utf-8")
    assert "asyncpg://" not in text, "连接串不得写进 ini（含密码），应只从环境读"
    assert "agent:agent" not in text


# ── 编码：被外部工具按 locale 编码读的文件必须纯 ASCII ──
#
# 本机实测（用户机器，Windows 中文 locale=GBK）：`alembic upgrade head` 直接崩在
#   UnicodeDecodeError: 'gbk' codec can't decode byte 0xb2 in position 96
# 根因：Alembic 用 `configparser.read(path, encoding="locale")` 读 `alembic.ini`，
# 而我在里面写了中文注释（文件是 UTF-8）→ 中文 Windows 上按 GBK 解码就炸。
# 这类文件**必须保持纯 ASCII**：注释用英文，中文放 `.py`/`.md`（那些按 UTF-8 读）。
#
# Mako 模板同理：它由 Mako 自己的读取逻辑载入，编码行为不如显式 UTF-8 可靠，一并钉死。


def test_alembic_ini_is_ascii_only() -> None:
    raw = ALEMBIC_INI.read_bytes()
    offender = next((i for i, b in enumerate(raw) if b > 127), None)
    assert offender is None, (
        f"alembic.ini 第 {offender} 字节非 ASCII：Alembic 按 locale 编码读它，"
        "中文 Windows（GBK）上会 UnicodeDecodeError。请把注释改成英文。"
    )


def test_mako_template_is_ascii_only() -> None:
    raw = (_ROOT / "alembic" / "script.py.mako").read_bytes()
    offender = next((i for i, b in enumerate(raw) if b > 127), None)
    assert offender is None, f"script.py.mako 第 {offender} 字节非 ASCII"


def _run_all() -> None:
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"RUN  {name}")
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {name}: {type(e).__name__}: {e}")
                failed.append(name)
            else:
                print(f"PASS {name}")
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
