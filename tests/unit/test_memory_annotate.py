"""记忆召回注入的纯逻辑契约：来源标注、去重、排序、渲染。

为什么这些逻辑值得单独测：注入 System Prompt 的质量只取决于三件事 ——
**顺序对不对、来源说不说得清、截断有没有标记**。三者都是纯数据变换，
所以能在沙箱内钉死，不必依赖真实 Redis/PG。

设计约定（spec §4.2）：
  * 排序：层优先级 working < short_term < long_term；同层按 created_at **倒序**；
    created_at 为 None 排**层末**；键相同保持输入顺序（稳定）。
  * `format_line` 输出 `- [来源 · YYYY-MM-DD] 内容`；超长截断**必须带省略号**。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_runtime.core.memory.annotate import (
    MAX_DEFAULT_CHARS,
    SOURCE_LONG,
    SOURCE_SHORT,
    SOURCE_WORKING,
    dedupe,
    format_line,
    sort_entries,
    with_source,
)
from agent_runtime.core.memory.base import MemoryEntry, MemoryQuery


def _entry(content: str, source: str = "", when: datetime | None = None) -> MemoryEntry:
    return MemoryEntry(content=content, source=source, created_at=when)


# ── 数据类扩展 ──


def test_memory_entry_has_source_and_created_at_defaults() -> None:
    e = MemoryEntry(content="x")
    assert e.source == ""
    assert e.created_at is None


def test_memory_query_has_session_id_default() -> None:
    assert MemoryQuery(text="x").session_id is None


# ── with_source ──


def test_with_source_tags_every_entry() -> None:
    tagged = with_source([_entry("a"), _entry("b")], SOURCE_LONG)
    assert [e.source for e in tagged] == [SOURCE_LONG, SOURCE_LONG]


def test_with_source_is_authoritative_over_layer_tag() -> None:
    """调用方最清楚自己查的是哪一层；层自带的 source 不应覆盖它。"""
    tagged = with_source([_entry("a", source=SOURCE_LONG)], SOURCE_SHORT)
    assert tagged[0].source == SOURCE_SHORT


def test_with_source_does_not_mutate_input() -> None:
    original = _entry("a")
    with_source([original], SOURCE_LONG)
    assert original.source == "", "不得就地修改入参对象（跨层会串味）"


def test_with_source_handles_empty_list() -> None:
    assert with_source([], SOURCE_LONG) == []


# ── dedupe ──


def test_dedupe_keeps_first_occurrence() -> None:
    entries = [_entry("same", source=SOURCE_WORKING), _entry("same", source=SOURCE_LONG)]
    out = dedupe(entries)
    assert len(out) == 1
    assert out[0].source == SOURCE_WORKING, "保留首次出现（层优先级更高者在前）"


def test_dedupe_ignores_surrounding_whitespace() -> None:
    out = dedupe([_entry("hello world"), _entry("  hello world  ")])
    assert len(out) == 1


def test_dedupe_keeps_distinct_contents() -> None:
    out = dedupe([_entry("a"), _entry("b")])
    assert [e.content for e in out] == ["a", "b"]


# ── sort_entries ──


def test_sort_orders_by_layer_priority() -> None:
    entries = [
        _entry("long", source=SOURCE_LONG),
        _entry("short", source=SOURCE_SHORT),
        _entry("working", source=SOURCE_WORKING),
    ]
    assert [e.content for e in sort_entries(entries)] == ["working", "short", "long"]


def test_sort_by_created_at_desc_within_layer() -> None:
    entries = [
        _entry("old", source=SOURCE_LONG, when=datetime(2026, 1, 1)),
        _entry("new", source=SOURCE_LONG, when=datetime(2026, 9, 21)),
        _entry("mid", source=SOURCE_LONG, when=datetime(2026, 5, 5)),
    ]
    assert [e.content for e in sort_entries(entries)] == ["new", "mid", "old"]


def test_sort_puts_none_created_at_last_within_layer() -> None:
    entries = [
        _entry("no-date", source=SOURCE_WORKING),
        _entry("dated", source=SOURCE_WORKING, when=datetime(2026, 1, 1)),
    ]
    assert [e.content for e in sort_entries(entries)] == ["dated", "no-date"]


def test_sort_is_stable_for_equal_keys() -> None:
    entries = [_entry("first"), _entry("second"), _entry("third")]
    assert [e.content for e in sort_entries(entries)] == ["first", "second", "third"]


def test_sort_unknown_source_goes_last() -> None:
    entries = [_entry("mystery", source="whatever"), _entry("working", source=SOURCE_WORKING)]
    assert [e.content for e in sort_entries(entries)] == ["working", "mystery"]


# ── format_line ──


def test_format_line_includes_source_and_date() -> None:
    line = format_line(_entry("内容", source=SOURCE_LONG, when=datetime(2026, 9, 21, 13, 5)))
    assert line == "- [long_term · 2026-09-21] 内容"


def test_format_line_without_date_shows_source_only() -> None:
    assert format_line(_entry("内容", source=SOURCE_WORKING)) == "- [working] 内容"


def test_format_line_without_source_has_no_brackets() -> None:
    assert format_line(_entry("内容")) == "- 内容"


def test_format_line_truncates_with_marker() -> None:
    line = format_line(_entry("x" * 100, source=SOURCE_LONG), max_chars=10)
    assert line.endswith("…"), "截断必须带标记（Demo 1 吃过无标记截断的亏）"
    assert "x" * 10 in line
    assert "x" * 11 not in line


def test_format_line_short_text_untouched() -> None:
    assert format_line(_entry("短"), max_chars=MAX_DEFAULT_CHARS) == "- 短"


def test_format_line_zero_max_chars_means_no_truncation() -> None:
    text = "y" * 50
    assert format_line(_entry(text), max_chars=0) == f"- {text}"


def test_format_line_collapses_newlines() -> None:
    """记忆内容必须压成一行：多行内容会破坏 System Prompt 里的列表结构。"""
    assert format_line(_entry("第一行\n第二行")) == "- 第一行 第二行"


def _run_all() -> None:
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        print(f"RUN  {t.__name__}")
        t()
        print(f"PASS {t.__name__}")
    print("ALL PASS")


if __name__ == "__main__":
    _run_all()
