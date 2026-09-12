"""Structured help documents and terminal/Markdown renderers.

Help content is data, not preformatted command output.  GDB command trees render
that data for the terminal today; a future MkDocs generator can call
:func:`render_markdown` without parsing terminal strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class HelpField:
    """One documented output field or column."""

    name: str
    description: str


@dataclass(frozen=True)
class HelpTopic:
    """One node in a git-style help tree."""

    name: str
    summary: str
    description: str
    usage: tuple[str, ...] = ()
    fields: tuple[HelpField, ...] = ()
    tips: tuple[str, ...] = ()
    configuration: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    children: tuple[HelpTopic, ...] = ()
    category: str = "Guide"


@dataclass(frozen=True)
class CommandHelp(HelpTopic):
    """A command topic plus the routing metadata that executes it."""

    action: str = ""
    kind: str = ""
    category: str = "Command"


@dataclass(frozen=True)
class HelpTree:
    """A complete command tree's structured documentation."""

    program: str
    title: str
    summary: str
    topics: tuple[HelpTopic, ...]
    category_descriptions: tuple[tuple[str, str], ...] = ()


class _StructField(Protocol):
    name: str
    path: tuple[str | int, ...]
    kind: str
    summary: bool


class _StructLayout(Protocol):
    struct_name: str
    display_name: str | None
    fields: dict[str, _StructField]


class SupportsHelpStructs(Protocol):
    @property
    def structs(self) -> dict[str, Any]:
        """Adapter layout's concrete struct map."""
        ...


def command_topics(tree: HelpTree) -> tuple[CommandHelp, ...]:
    """Return command nodes from *tree* with their concrete type preserved."""
    return tuple(topic for topic in tree.topics if isinstance(topic, CommandHelp))


def command_aliases(tree: HelpTree) -> dict[str, str]:
    """Return alias-to-canonical-command mappings declared by the help tree."""
    return {
        alias: topic.name for topic in command_topics(tree) for alias in topic.aliases
    }


def find_topic(tree: HelpTree, path: tuple[str, ...]) -> HelpTopic | None:
    """Resolve a topic path, accepting aliases at every level."""
    topics = tree.topics
    found: HelpTopic | None = None
    for raw_part in path:
        part = raw_part.strip().lower()
        found = next(
            (
                topic
                for topic in topics
                if part == topic.name.lower()
                or part in {alias.lower() for alias in topic.aliases}
            ),
            None,
        )
        if found is None:
            return None
        topics = found.children
    return found


def _section(title: str, lines: tuple[str, ...]) -> list[str]:
    if not lines:
        return []
    return ["", title, *(f"    {line}" for line in lines)]


def _field_section(fields: tuple[HelpField, ...]) -> list[str]:
    if not fields:
        return []
    width = max(len(field.name) for field in fields)
    return [
        "",
        "FIELDS",
        *(f"    {item.name:<{width}}  {item.description}" for item in fields),
    ]


def render_terminal(tree: HelpTree, path: tuple[str, ...] = ()) -> str:
    """Render the tree overview or one topic as stable plain text."""
    if not path:
        lines = [
            tree.title,
            "",
            tree.summary,
            "",
            "USAGE",
            f"    {tree.program} help <topic>",
        ]
        categories: list[str] = []
        for topic in tree.topics:
            if topic.category not in categories:
                categories.append(topic.category)
        category_descriptions = dict(tree.category_descriptions)
        for category in categories:
            members = [topic for topic in tree.topics if topic.category == category]
            width = max(len(topic.name) for topic in members)
            lines.extend(["", category.upper()])
            description = category_descriptions.get(category)
            if description:
                lines.append(f"    {description}")
                lines.append("")
            for topic in members:
                prefix = f"{tree.program} " if isinstance(topic, CommandHelp) else ""
                lines.append(f"    {prefix}{topic.name:<{width}}  {topic.summary}")
        aliases = command_aliases(tree)
        if aliases:
            width = max(len(alias) for alias in aliases)
            lines.extend(["", "ALIASES"])
            lines.extend(
                f"    {alias:<{width}}  -> {tree.program} {target}"
                for alias, target in aliases.items()
            )
        lines.extend(["", f"Run '{tree.program} help <topic>' for detailed help."])
        return "\n".join(lines)

    topic = find_topic(tree, path)
    if topic is None:
        return ""
    qualified = " ".join((tree.program, "help", *path))
    lines = [
        "NAME",
        f"    {qualified} - {topic.summary}",
        "",
        "DESCRIPTION",
        f"    {topic.description}",
    ]
    lines += _section("USAGE", topic.usage)
    lines += _field_section(topic.fields)
    lines += _section("TIPS", topic.tips)
    lines += _section("CONFIGURATION", topic.configuration)
    lines += _section("LIMITATIONS", topic.limitations)
    if topic.children:
        width = max(len(child.name) for child in topic.children)
        lines.extend(["", "SUBTOPICS"])
        lines.extend(
            f"    {child.name:<{width}}  {child.summary}" for child in topic.children
        )
        lines.extend(["", f"Run '{qualified} <subtopic>' for details."])
    if topic.aliases:
        lines += _section("ALIASES", topic.aliases)
    return "\n".join(lines)


def render_markdown(tree: HelpTree) -> str:
    """Render a complete tree as MkDocs-compatible Markdown."""
    lines = [f"# {tree.title}", "", tree.summary, ""]
    for topic in tree.topics:
        lines.extend(_topic_markdown(topic, level=2, program=tree.program))
    return "\n".join(lines).rstrip() + "\n"


def _topic_markdown(topic: HelpTopic, *, level: int, program: str) -> list[str]:
    heading = "#" * level
    lines = [f"{heading} `{topic.name}`", "", topic.description, ""]
    sections: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("Usage", tuple(f"`{line}`" for line in topic.usage)),
        ("Tips", topic.tips),
        ("Configuration", topic.configuration),
        ("Limitations", topic.limitations),
    )
    if topic.fields:
        lines += [
            f"{heading}# Fields",
            "",
            "| Field | Description |",
            "| --- | --- |",
        ]
        lines += [f"| `{item.name}` | {item.description} |" for item in topic.fields]
        lines.append("")
    for title, values in sections:
        if values:
            lines += [
                f"{heading}# {title}",
                "",
                *(f"- {value}" for value in values),
                "",
            ]
    for child in topic.children:
        lines += _topic_markdown(child, level=level + 1, program=program)
    return lines


def pretty_printer_topic(layout: SupportsHelpStructs | None) -> HelpTopic:
    """Build exact pretty-printer docs from the active layout metadata."""
    children: list[HelpTopic] = []
    if layout is not None:
        seen: set[str] = set()
        for struct_name, struct_layout in layout.structs.items():
            display = struct_layout.display_name or struct_name.removeprefix("struct ")
            slug = re.sub(r"[^a-z0-9]+", "-", display.lower()).strip("-")
            if slug in seen:
                slug = re.sub(r"[^a-z0-9]+", "-", struct_name.lower()).strip("-")
            seen.add(slug)
            declared_fields = tuple(
                HelpField(
                    name,
                    _printer_field_description(field),
                )
                for name, field in struct_layout.fields.items()
                if field.summary
            )
            summary_fields = declared_fields or (
                HelpField(
                    "(none)",
                    "This registered type has no one-line summary fields; only its display label is emitted.",
                ),
            )
            children.append(
                HelpTopic(
                    name=slug,
                    summary=f"{display} fold for `{struct_name}`",
                    description=(
                        f"GDB values whose underlying tag is `{struct_name}` are folded "
                        f"as `{display}(...)`; typedef and const/volatile spelling is ignored."
                    ),
                    usage=("p <value>", "info locals", "bt full"),
                    fields=summary_fields,
                    tips=(
                        "Use `p /r <value>` when the native unformatted value is required.",
                    ),
                    configuration=(
                        "The field set comes from the layout selected during `gdr init`.",
                    ),
                    limitations=(
                        "Unreadable or absent target fields render as N/A; the printer never calls the inferior.",
                    ),
                    aliases=(struct_name,),
                    category="Pretty-printer",
                )
            )
    description = (
        "GDR pretty-printers fold selected kernel structs into one-line summaries. "
        "The active adapter layout is the single source of truth for type matching and fields."
    )
    if layout is None:
        description += " Initialise an RTOS adapter to list its concrete printers."
    return HelpTopic(
        name="pretty-printers",
        summary="Describe registered kernel-struct folds",
        description=description,
        usage=("p <kernel-object>",),
        tips=("Open a printer subtopic to see its exact summary fields.",),
        configuration=(
            "Concrete printers are registered after `gdr init <rtos> <version>`.",
        ),
        limitations=("Only types present in the selected adapter layout are folded.",),
        aliases=("printers", "pretty-printer"),
        children=tuple(children),
        category="Reference",
    )


def functions_topic() -> HelpTopic:
    """Return documentation for the RTOS-neutral convenience functions."""
    return HelpTopic(
        name="functions",
        summary="Describe raw-value convenience functions",
        description=(
            "Convenience functions navigate kernel objects but return native gdb.Value "
            "objects, leaving field inspection and expression evaluation to GDB."
        ),
        usage=(
            'p $gdr_task("<name>")',
            "p $gdr_tasks()",
            'p $gdr_object("<kind>", "<name>")',
        ),
        fields=(
            HelpField(
                "$gdr_task", "One target-native task value, or null when absent."
            ),
            HelpField(
                "$gdr_tasks", "A target-native pointer array of scheduler tasks."
            ),
            HelpField(
                "$gdr_object",
                "One target-native object value selected by kind and name.",
            ),
        ),
        tips=("Inspect fields with normal GDB expressions after navigation.",),
        configuration=(
            "The accepted kinds and enumeration capability are adapter-owned.",
        ),
        limitations=(
            "Functions never call target code and cannot find objects the adapter cannot enumerate.",
        ),
        category="Reference",
    )


def _printer_field_description(field: _StructField) -> str:
    path = ".".join(str(part) for part in field.path)
    presentation = {
        "string": "a quoted string",
        "ptr": "a symbolized pointer/address",
        "enum": "a symbolic enum when known",
        "flags": "symbolic flags when known",
        "hex": "hexadecimal",
        "": "an integer/value",
    }.get(field.kind, field.kind)
    return f"Reads `{path}` and renders it as {presentation}."
