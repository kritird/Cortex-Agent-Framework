"""Tests for TaskGraphCompiler."""
import pytest
from cortex.config.schema import TaskTypeConfig
from cortex.exceptions import CortexCycleError, CortexMissingDependencyError
from cortex.modules.task_graph_compiler import DecomposedTask, TaskGraphCompiler


def make_task_type(name, depends_on=None, mandatory=True):
    return TaskTypeConfig(name=name, description=f"Task {name}", depends_on=depends_on or [])


def test_compile_simple():
    compiler = TaskGraphCompiler()
    tasks = [make_task_type("a"), make_task_type("b"), make_task_type("c", depends_on=["a", "b"])]
    graph = compiler.compile(tasks)
    assert "a" in graph.task_types
    assert "c" in graph.adjacency
    assert graph.topo_order.index("a") < graph.topo_order.index("c")


def test_cycle_detection():
    compiler = TaskGraphCompiler()
    tasks = [
        make_task_type("a", depends_on=["b"]),
        make_task_type("b", depends_on=["a"]),
    ]
    with pytest.raises(CortexCycleError):
        compiler.compile(tasks)


def test_missing_dependency():
    compiler = TaskGraphCompiler()
    tasks = [make_task_type("a", depends_on=["nonexistent"])]
    with pytest.raises(CortexMissingDependencyError):
        compiler.compile(tasks)


def test_instantiate_and_ready_tasks():
    compiler = TaskGraphCompiler()
    tasks = [make_task_type("a"), make_task_type("b"), make_task_type("c", depends_on=["a"])]
    compiled = compiler.compile(tasks)
    decomposed = [
        DecomposedTask(task_name="a", instruction="Do a"),
        DecomposedTask(task_name="b", instruction="Do b"),
        DecomposedTask(task_name="c", instruction="Do c", depends_on=["a"]),
    ]
    runtime = compiler.instantiate(compiled, "sess_test01", decomposed)
    ready = compiler.get_ready_tasks(runtime)
    ready_names = {t.task_name for t in ready}
    assert "a" in ready_names
    assert "b" in ready_names
    assert "c" not in ready_names


def test_mark_complete_unblocks():
    compiler = TaskGraphCompiler()
    tasks = [make_task_type("a"), make_task_type("b", depends_on=["a"])]
    compiled = compiler.compile(tasks)
    decomposed = [
        DecomposedTask(task_name="a", instruction="Do a"),
        DecomposedTask(task_name="b", instruction="Do b", depends_on=["a"]),
    ]
    runtime = compiler.instantiate(compiled, "sess_test02", decomposed)
    task_a = next(t for t in runtime.tasks.values() if t.task_name == "a")
    compiler.mark_complete(runtime, task_a.task_id)
    ready = compiler.get_ready_tasks(runtime)
    assert any(t.task_name == "b" for t in ready)


# ─── add_tasks_batch ──────────────────────────────────────────────────────

def _runtime_with_types(compiler, session_id, types, decomposed):
    compiled = compiler.compile(types)
    return compiler.instantiate(compiled, session_id, decomposed)


def test_add_tasks_batch_simple_dep_on_complete():
    """New task depending on an already-complete task becomes immediately ready."""
    compiler = TaskGraphCompiler()
    types = [make_task_type("a"), make_task_type("verify")]
    runtime = _runtime_with_types(
        compiler, "sess_add01", types,
        [DecomposedTask(task_name="a", instruction="Do a")],
    )
    task_a = next(t for t in runtime.tasks.values() if t.task_name == "a")
    compiler.mark_complete(runtime, task_a.task_id)

    effective = {tt.name: tt for tt in types}
    committed = compiler.add_tasks_batch(
        runtime,
        [{
            "task_name": "verify_a",
            "task_type": "verify",
            "instruction": "Verify a's output",
            "depends_on": ["a"],
        }],
        effective,
    )
    assert len(committed) == 1
    assert committed[0].task_name == "verify_a"
    assert committed[0].is_adhoc is True
    ready_names = {t.task_name for t in compiler.get_ready_tasks(runtime)}
    assert "verify_a" in ready_names


def test_add_tasks_batch_forward_reference_within_batch():
    """A batch entry can depend on another entry added later in the same batch."""
    compiler = TaskGraphCompiler()
    types = [make_task_type("a"), make_task_type("step")]
    runtime = _runtime_with_types(
        compiler, "sess_add02", types,
        [DecomposedTask(task_name="a", instruction="Do a")],
    )
    effective = {tt.name: tt for tt in types}

    # 'second' is listed first but depends on 'first' which is listed after.
    committed = compiler.add_tasks_batch(
        runtime,
        [
            {
                "task_name": "second",
                "task_type": "step",
                "instruction": "runs second",
                "depends_on": ["first"],
            },
            {
                "task_name": "first",
                "task_type": "step",
                "instruction": "runs first",
                "depends_on": ["a"],
            },
        ],
        effective,
    )
    committed_names = {t.task_name for t in committed}
    assert committed_names == {"first", "second"}
    second = next(t for t in committed if t.task_name == "second")
    first = next(t for t in committed if t.task_name == "first")
    assert first.depends_on_ids[0] in second.depends_on_ids or True  # wire check
    # second must depend on first (by id)
    assert second.depends_on_ids == [first.task_id]


def test_add_tasks_batch_rejects_cycle_in_batch():
    """Mutually-dependent batch entries both get dropped (neither resolves)."""
    compiler = TaskGraphCompiler()
    types = [make_task_type("a"), make_task_type("step")]
    runtime = _runtime_with_types(
        compiler, "sess_add03", types,
        [DecomposedTask(task_name="a", instruction="Do a")],
    )
    effective = {tt.name: tt for tt in types}

    committed = compiler.add_tasks_batch(
        runtime,
        [
            {"task_name": "x", "task_type": "step", "instruction": "x", "depends_on": ["y"]},
            {"task_name": "y", "task_type": "step", "instruction": "y", "depends_on": ["x"]},
        ],
        effective,
    )
    assert committed == []
    assert "x" not in runtime.name_to_id
    assert "y" not in runtime.name_to_id


def test_add_tasks_batch_rejects_unknown_task_type():
    compiler = TaskGraphCompiler()
    types = [make_task_type("a")]
    runtime = _runtime_with_types(
        compiler, "sess_add04", types,
        [DecomposedTask(task_name="a", instruction="Do a")],
    )
    effective = {tt.name: tt for tt in types}
    committed = compiler.add_tasks_batch(
        runtime,
        [{
            "task_name": "boom",
            "task_type": "nonexistent",
            "instruction": "should fail",
            "depends_on": [],
        }],
        effective,
    )
    assert committed == []
    assert "boom" not in runtime.name_to_id


def test_add_tasks_batch_rejects_duplicate_name():
    compiler = TaskGraphCompiler()
    types = [make_task_type("a"), make_task_type("step")]
    runtime = _runtime_with_types(
        compiler, "sess_add05", types,
        [DecomposedTask(task_name="a", instruction="Do a")],
    )
    effective = {tt.name: tt for tt in types}
    committed = compiler.add_tasks_batch(
        runtime,
        [{
            "task_name": "a",  # collides with existing
            "task_type": "step",
            "instruction": "dup",
            "depends_on": [],
        }],
        effective,
    )
    assert committed == []


def test_add_tasks_batch_rejects_dep_on_failed_task():
    """Depending on a failed/timeout task would hang forever — reject."""
    compiler = TaskGraphCompiler()
    types = [make_task_type("a"), make_task_type("step")]
    runtime = _runtime_with_types(
        compiler, "sess_add06", types,
        [DecomposedTask(task_name="a", instruction="Do a")],
    )
    task_a = next(t for t in runtime.tasks.values() if t.task_name == "a")
    compiler.mark_failed(runtime, task_a.task_id)

    effective = {tt.name: tt for tt in types}
    committed = compiler.add_tasks_batch(
        runtime,
        [{
            "task_name": "follow",
            "task_type": "step",
            "instruction": "depends on failed a",
            "depends_on": ["a"],
        }],
        effective,
    )
    assert committed == []


def test_add_tasks_batch_rejects_dep_on_nonexistent():
    compiler = TaskGraphCompiler()
    types = [make_task_type("a"), make_task_type("step")]
    runtime = _runtime_with_types(
        compiler, "sess_add07", types,
        [DecomposedTask(task_name="a", instruction="Do a")],
    )
    effective = {tt.name: tt for tt in types}
    committed = compiler.add_tasks_batch(
        runtime,
        [{
            "task_name": "orphan",
            "task_type": "step",
            "instruction": "bad dep",
            "depends_on": ["ghost"],
        }],
        effective,
    )
    assert committed == []


def test_add_tasks_batch_appends_task_id_indexing():
    """Task IDs continue the existing indexing scheme."""
    compiler = TaskGraphCompiler()
    types = [make_task_type("a"), make_task_type("b"), make_task_type("step")]
    runtime = _runtime_with_types(
        compiler, "sess_add08", types,
        [
            DecomposedTask(task_name="a", instruction="a"),
            DecomposedTask(task_name="b", instruction="b"),
        ],
    )
    assert len(runtime.tasks) == 2
    effective = {tt.name: tt for tt in types}
    committed = compiler.add_tasks_batch(
        runtime,
        [{
            "task_name": "new",
            "task_type": "step",
            "instruction": "new task",
            "depends_on": [],
        }],
        effective,
    )
    assert len(committed) == 1
    assert committed[0].task_id.startswith("sess_add08/002_")
    assert "new" in runtime.name_to_id
    assert len(runtime.tasks) == 3
