"""Unit tests for structured terminal and Markdown help rendering."""

from __future__ import annotations

from freertos.help_docs import build_help_tree
from freertos.layout import FreeRtosConfig, build_layout
from gdr.help import find_topic, render_markdown, render_terminal


def test_nested_pretty_printer_topic_comes_from_layout_metadata():
    layout = build_layout(FreeRtosConfig(trace_facility=True), (10, 3, 1))
    tree = build_help_tree(layout)

    topic = find_topic(tree, ("pretty-printers", "task"))

    assert topic is not None
    assert "struct tskTaskControlBlock" in topic.description
    assert {field.name for field in topic.fields} >= {"name", "current_priority"}
    output = render_terminal(tree, ("pretty-printers", "task"))
    assert "NAME\n    frt help pretty-printers task" in output
    assert "FIELDS" in output
    assert "pcTaskName" in output


def test_markdown_renderer_preserves_structured_help_sections():
    markdown = render_markdown(build_help_tree())

    assert markdown.startswith("# FreeRTOS help")
    assert "## `tasks`" in markdown
    assert "### Fields" in markdown
    assert "### Configuration" in markdown
    assert "### Limitations" in markdown
    assert "## `pretty-printers`" in markdown


def test_command_completion_suggests_help_topics_without_target_walk(monkeypatch):
    import freertos.commands as commands

    monkeypatch.setattr(
        commands,
        "active",
        lambda: (_ for _ in ()).throw(AssertionError("target walk")),
    )

    assert commands._complete("help ta", "ta") == ["tasks", "task"]
    assert "heap" in commands._complete("help ", None)
