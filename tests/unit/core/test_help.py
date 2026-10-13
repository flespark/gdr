"""Unit tests for structured terminal and Markdown help rendering."""

from __future__ import annotations

from freertos.columns import task_specs
from freertos.heap import HeapGeometry, HeapSnapshot, heap_pairs
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


def test_heap_help_matches_summary_and_problem_fields():
    tree = build_help_tree()
    topic = find_topic(tree, ("heap",))
    assert topic is not None
    documented = {name for field in topic.fields for name in field.name.split("/")}
    for verdict in (
        "ok",
        "mismatch: free block 0x2000 missing",
        "unavailable: unreadable",
    ):
        snap = HeapSnapshot(
            geometry=HeapGeometry(kind=4, algorithm="heap_4"),
            allocs=12,
            frees=4,
            cross_check=verdict,
        )
        pairs = dict(heap_pairs(snap))
        assert pairs.keys() <= documented
        assert pairs["Allocated"] == "12" and pairs["Freed"] == "4"
        assert "CrossCheck" not in pairs
        assert ("Check[HeapCrossCheck]" in pairs) == (verdict != "ok")
    output = render_terminal(tree, ("heap",))
    assert "Allocs/Frees" not in output
    assert "operation counts" in output
    assert "frt check heap" in output


def test_free_rtos_detail_help_uses_single_object_problem_and_entry_fields():
    tree = build_help_tree()
    for kind in ("task", "queue", "semaphore", "mutex", "timer", "eventgroup"):
        topic = find_topic(tree, (kind,))
        assert topic is not None
        names = {field.name for field in topic.fields}
        assert "Check[name]" in names
        assert "Checks" not in names and "OwnerCheck/Checks" not in names
        assert "Key: Value" in topic.description
        assert "merged table" not in topic.description
    queue = find_topic(tree, ("queue",))
    assert queue is not None
    assert "Item[n]" in {field.name for field in queue.fields}
    assert "Item" not in {field.name for field in queue.fields}
    event = find_topic(tree, ("eventgroup",))
    assert event is not None
    assert "Waiter[n]" in {field.name for field in event.fields}


def test_high_water_help_and_panel_column_document_peak_used_bytes():
    topic = find_topic(build_help_tree(), ("tasks",))
    assert topic is not None
    description = next(
        field.description for field in topic.fields if field.name == "HighWater"
    )
    assert (
        "Historical maximum" in description
        and "before scheduler startup" in description
    )
    assert "max(Used," in description
    assert any("HighWater >= Used" in note for note in topic.limitations)
    specs = task_specs(
        base_priority=False,
        stack=True,
        high_water=True,
        runtime=False,
        smp=False,
        affinity=False,
    )
    high = next(spec for spec in specs if spec.key == "HighWater")
    assert "historical maximum stack bytes used" in high.doc


def test_command_completion_suggests_help_topics_without_target_walk(monkeypatch):
    import freertos.commands as commands

    monkeypatch.setattr(
        commands,
        "active",
        lambda: (_ for _ in ()).throw(AssertionError("target walk")),
    )

    assert commands._complete("help ta", "ta") == ["tasks", "task"]
    assert "heap" in commands._complete("help ", None)
