"""Skill 加载：把 `skills/*.md` 变成可用的 `BaseSkill`。

**为什么不用 PyYAML**：本沙箱没有 pip、装不上第三方包 —— 依赖它等于交付一段**无法验证**的代码。
因此只解析受支持的 front-matter 子集：标量、`[a, b]`、`- item` 列表；
不支持的形状（嵌套映射、锚点、多行、无法识别的缩进）**明确报错并跳过**，绝不猜测。

ponytail 天花板：复杂 YAML 不支持。升级路径 = 安装 pyyaml 后只替换 `parse_front_matter`，
`load_file` / `load_directory` 的契约与错误语义保持不变。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseSkill, SkillManifest

DELIMITER = "---"
SUPPORTED_KEYS = ("name", "description", "version", "triggers", "required_tools", "tags")
LIST_KEYS = ("triggers", "required_tools", "tags")


class SkillFormatError(Exception):
    """skill 文件格式不受支持 —— 明确失败，不做猜测。"""


class MarkdownSkill(BaseSkill):
    """Markdown 定义的技能：manifest 来自 front-matter，正文即 System Prompt 扩展。"""

    def __init__(self, manifest: SkillManifest, body: str, source: str = "") -> None:
        self.manifest = manifest
        self.body = body.strip()
        self.source = source

    def build_prompt_extension(self, task: str) -> str:
        return self.body


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _to_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


class SkillLoader:
    @staticmethod
    def parse_front_matter(text: str) -> tuple[dict, str]:
        """返回 (front_matter, body)；不支持的形状抛 `SkillFormatError`。"""
        normalized = (text or "").replace("\r\n", "\n")
        if not normalized.startswith(DELIMITER + "\n") and normalized.strip() != DELIMITER:
            raise SkillFormatError("缺少 front-matter：文件必须以 '---' 开头")

        rest = normalized[len(DELIMITER) :].lstrip("\n")
        if rest.startswith(DELIMITER):
            # 空 front-matter 块（"---\n---\n正文"）：结束分隔符就在 offset 0，
            # 下面的 find("\n---") 匹配不到，旧实现会误报「未闭合」——分隔符明明在。
            block, body = "", rest[len(DELIMITER) :].lstrip("\n")
        else:
            end = rest.find("\n" + DELIMITER)
            if end == -1:
                raise SkillFormatError("front-matter 未闭合：缺少结束的 '---'")
            block = rest[:end]
            body = rest[end + len("\n" + DELIMITER) :].lstrip("\n")

        data: dict = {}
        current_key: str | None = None
        skipping = False

        for raw_line in block.split("\n"):
            line = raw_line.rstrip()
            if not line.strip() or line.lstrip().startswith("#"):
                continue

            if line.startswith((" ", "\t")):
                stripped = line.strip()
                if skipping:
                    continue
                if current_key is None:
                    raise SkillFormatError(f"缩进内容缺少所属键：{raw_line!r}")
                if not stripped.startswith("- "):
                    raise SkillFormatError(
                        f"不支持的缩进结构：{raw_line!r}（仅支持 '- item' 列表项）"
                    )
                if current_key not in LIST_KEYS:
                    raise SkillFormatError(
                        f"{current_key}: 该键只接受单个值，不接受列表"
                    )
                item = _strip_quotes(stripped[2:])
                if item[:1] in ("&", "*"):
                    raise SkillFormatError(f"{current_key}: 不支持 YAML 锚点/别名 → {item!r}")
                existing = data.get(current_key)
                if isinstance(existing, list):
                    existing.append(item)
                elif existing in ("", None):
                    data[current_key] = [item]
                else:
                    raise SkillFormatError(f"{current_key}: 不能同时是标量与列表")
                continue

            if ":" not in line:
                raise SkillFormatError(f"无法解析的行：{raw_line!r}")
            key, _, value = line.partition(":")
            key = key.strip()

            if key not in SUPPORTED_KEYS:
                # 未知键连同其缩进块一起忽略：向前兼容（未来新增字段不会让老代码报错）
                current_key, skipping = None, True
                continue

            skipping = False
            current_key = key
            inline = value.strip()

            if inline[:1] in ("&", "*"):
                # YAML 锚点/别名：不支持。不拦就会静默变成字符串 "&a [x, y]" —— 一个永远命中不了的 trigger。
                raise SkillFormatError(
                    f"{key}: 不支持 YAML 锚点/别名 → {inline!r}；请直接写值"
                )
            if inline.startswith("{"):
                raise SkillFormatError(f"{key}: 不支持嵌套映射结构 → {inline!r}")
            if inline.startswith("["):
                if key not in LIST_KEYS:
                    # `name: [a, b]` 不拦会变成字符串 "['a', 'b']"：一个永远叫不对的名字。
                    raise SkillFormatError(
                        f"{key}: 该键只接受单个值，不接受列表 → {inline!r}"
                    )
                if not inline.endswith("]"):
                    raise SkillFormatError(f"{key}: 列表缺少 ']' → {inline!r}")
                inner = inline[1:-1].strip()
                data[key] = (
                    [_strip_quotes(part) for part in inner.split(",") if part.strip()]
                    if inner
                    else []
                )
                continue
            if not inline:
                if key in LIST_KEYS:
                    data[key] = []  # 允许后续 '- item' 追加
                    continue
                raise SkillFormatError(f"{key}: 缺少值")
            data[key] = _strip_quotes(inline)

        return data, body

    @classmethod
    def load_file(cls, path: str) -> BaseSkill:
        file_path = Path(path)
        try:
            # utf-8-sig：Windows 编辑器（记事本 / PowerShell 5.1）默认写 UTF-8 BOM。
            # 用纯 "utf-8" 读会留下 BOM，导致 startswith("---") 失败并报出「文件必须以 '---' 开头」
            # 这种把用户指到反方向的诊断 —— 文件明明以 '---' 开头。
            text = file_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            # UnicodeDecodeError 必须一起接住：否则一个二进制文件被误命名成 .md，
            # 就会让 load_directory 直接抛出去，把**同目录下所有好技能一起丢掉**。
            raise SkillFormatError(f"读取失败：{e}") from None

        data, body = cls.parse_front_matter(text)
        name = str(data.get("name") or "").strip()
        if not name:
            raise SkillFormatError(f"{file_path.name}: 缺少必填字段 name")
        description = str(data.get("description") or "").strip()
        if not description:
            # 不再悄悄补成「技能 {name}」：计划 §1.1 写的是 name / description 双必填，
            # 而 description 会出现在 GET /api/skills 里，是给人看的。
            raise SkillFormatError(f"{file_path.name}: 缺少必填字段 description")
        if not body.strip():
            raise SkillFormatError(f"{file_path.name}: 正文为空（技能没有可注入的指导内容）")

        manifest = SkillManifest(
            name=name,
            description=description,
            version=str(data.get("version") or "1.0"),
            triggers=_to_list(data.get("triggers")),
            required_tools=_to_list(data.get("required_tools")),
            tags=_to_list(data.get("tags")),
        )
        return MarkdownSkill(manifest=manifest, body=body, source=str(file_path))

    @classmethod
    def load_directory(cls, dir_path: str) -> tuple[list[BaseSkill], list[str]]:
        """加载目录下所有 `*.md`；返回 (skills, errors)。

        坏文件只记错误、不影响好文件，也**不阻断启动**（与 MCP 加载一致）。
        """
        directory = Path(dir_path)
        if not directory.is_dir():
            return [], [f"skills 目录不存在：{dir_path}"]

        skills: list[BaseSkill] = []
        errors: list[str] = []
        seen: set[str] = set()

        for path in sorted(directory.glob("*.md")):
            try:
                skill = cls.load_file(str(path))
            except SkillFormatError as e:
                errors.append(f"{path.name}: {e}")
                continue
            if skill.manifest.name in seen:
                errors.append(f"{path.name}: 技能名重复（name={skill.manifest.name}）")
                continue
            seen.add(skill.manifest.name)
            skills.append(skill)

        return skills, errors
