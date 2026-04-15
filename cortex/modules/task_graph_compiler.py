"""TaskGraphCompiler — compiles task_types into a DAG and instantiates runtime graphs."""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from cortex.config.schema import TaskTypeConfig
from cortex.exceptions import CortexCycleError, CortexConfigError, CortexMissingDependencyError
from cortex.modules.result_envelope_store import TaskEnvelope

logger = logging.getLogger(__name__)


@dataclass
class DecomposedTask:
    """A task envelope as parsed from the LLM's decomposition stream."""
    task_name: str
    instruction: str
    depends_on: List[str] = field(default_factory=list)
    input_refs: List[str] = field(default_factory=list)
    context_hints: Dict[str, str] = field(default_factory=dict)
    mandatory: Optional[bool] = None


@dataclass
class CompiledTaskGraph:
    """Result of static compilation of task_types from cortex.yaml."""
    # {task_name: TaskTypeConfig}
    task_types: Dict[str, TaskTypeConfig]
    # {task_name: [dependency_names]}
    adjacency: Dict[str, List[str]]
    # topological order
    topo_order: List[str]
    # task names that have no dependents (leaf nodes)
    leaf_nodes: Set[str]


@dataclass
class RuntimeTask:
    """A concrete task instance in a session's runtime graph."""
    task_id: str
    task_name: str
    instruction: str
    depends_on: List[str]  # task_names (not IDs)
    depends_on_ids: List[str]  # resolved task_ids
    input_refs: List[str]
    status: str = "pending"  # pending | running | complete | failed | timeout
    mandatory: bool = True
    context_hints: Dict[str, str] = field(default_factory=dict)
    config: Optional[TaskTypeConfig] = None
    is_adhoc: bool = False  # True if not defined in cortex.yaml — created at runtime
    # Wave-level validation retry state. attempt_count is the number of times
    # this task has been re-dispatched by the wave validation gate (not the
    # inner exception-retry loop in GenericMCPAgent). Capped at 3.
    attempt_count: int = 0
    validation_feedback: Optional[str] = None  # set by gate, consumed by sub-agent on retry
    # HITL budget per sub-agent attempt. Reset by the wave gate on each retry.
    # Hard-capped at 3 asks per attempt to prevent runaway clarification loops.
    hitl_ask_count: int = 0


@dataclass
class RuntimeTaskGraph:
    """Live task graph for one session execution."""
    session_id: str
    tasks: Dict[str, RuntimeTask]  # {task_id: RuntimeTask}
    name_to_id: Dict[str, str]  # {task_name: task_id}
    ready_tasks: List[RuntimeTask]
    pending_tasks: List[RuntimeTask]
    is_partial: bool = False  # True if a mandatory task failed


class TaskGraphCompiler:
    """
    Compiles cortex.yaml task_types into a DAG at startup.
    Instantiates a concrete task graph per session at runtime.
    """

    def compile(self, task_types: List[TaskTypeConfig]) -> CompiledTaskGraph:
        """
        1. Build adjacency list from depends_on fields
        2. Detect cycles using DFS
        3. Detect missing depends_on references
        4. Compute topological sort
        5. Return CompiledTaskGraph
        """
        type_map: Dict[str, TaskTypeConfig] = {t.name: t for t in task_types}
        adjacency: Dict[str, List[str]] = {t.name: list(t.depends_on) for t in task_types}

        # Check for missing references
        for task in task_types:
            for dep in task.depends_on:
                if dep not in type_map:
                    raise CortexMissingDependencyError(
                        task_name=task.name,
                        missing_dep=dep,
                        yaml_path=f"task_types[{task.name}].depends_on",
                    )

        # Cycle detection via DFS
        visited: Set[str] = set()
        path: List[str] = []
        path_set: Set[str] = set()

        def dfs(node: str) -> None:
            if node in path_set:
                cycle_start = path.index(node)
                raise CortexCycleError(path[cycle_start:] + [node])
            if node in visited:
                return
            path.append(node)
            path_set.add(node)
            for dep in adjacency.get(node, []):
                dfs(dep)
            path.pop()
            path_set.remove(node)
            visited.add(node)

        for name in type_map:
            dfs(name)

        # Topological sort (Kahn's algorithm)
        in_degree: Dict[str, int] = {name: 0 for name in type_map}
        dependents: Dict[str, List[str]] = {name: [] for name in type_map}
        for name, deps in adjacency.items():
            for dep in deps:
                in_degree[name] += 1
                dependents[dep].append(name)

        queue = [name for name, deg in in_degree.items() if deg == 0]
        topo_order: List[str] = []
        while queue:
            node = queue.pop(0)
            topo_order.append(node)
            for dependent in dependents.get(node, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        # Find leaf nodes (no dependents)
        leaf_nodes = {name for name in type_map if not dependents.get(name)}

        logger.info(
            "Compiled task graph: %d types, %d leaf nodes",
            len(type_map), len(leaf_nodes)
        )

        return CompiledTaskGraph(
            task_types=type_map,
            adjacency=adjacency,
            topo_order=topo_order,
            leaf_nodes=leaf_nodes,
        )

    def instantiate(
        self,
        compiled: CompiledTaskGraph,
        session_id: str,
        decomposed_tasks: List[DecomposedTask],
        user_task_types: Optional[List[TaskTypeConfig]] = None,
        max_tasks: int = 20,
        sandbox_enabled: bool = False,
    ) -> RuntimeTaskGraph:
        """
        Create a RuntimeTaskGraph from decomposed LLM output.
        """
        # Merge user task type overrides (same name wins)
        effective_types = dict(compiled.task_types)
        if user_task_types:
            for ut in user_task_types:
                effective_types[ut.name] = ut

        # Track which tasks are ad-hoc (not defined in cortex.yaml)
        adhoc_task_names: Set[str] = set()

        # Resolve unknown tasks as ad-hoc rather than dropping them
        valid_tasks: List[DecomposedTask] = []
        for dt in decomposed_tasks:
            if dt.task_name not in effective_types:
                # Create an ad-hoc TaskTypeConfig for this unknown task
                capability = "code_exec" if sandbox_enabled else "llm_synthesis"
                adhoc_config = TaskTypeConfig(
                    name=dt.task_name,
                    description=f"Ad-hoc task created at runtime: {dt.task_name}",
                    output_format="text",
                    mandatory=False,
                    complexity="adaptive",
                    capability_hint=capability,
                )
                effective_types[dt.task_name] = adhoc_config
                adhoc_task_names.add(dt.task_name)
                logger.info(
                    "Decomposed task '%s' not in task_types — created ad-hoc config (capability=%s)",
                    dt.task_name, capability,
                )
            valid_tasks.append(dt)

        # Cap task count — drop optional tasks first if over limit
        if len(valid_tasks) > max_tasks:
            mandatory = [
                t for t in valid_tasks
                if (t.mandatory is True) or (t.mandatory is None and effective_types[t.task_name].mandatory)
            ]
            optional = [t for t in valid_tasks if t not in mandatory]
            valid_tasks = mandatory + optional
            valid_tasks = valid_tasks[:max_tasks]
            logger.warning("Task count capped at %d", max_tasks)

        # Assign task IDs
        tasks: Dict[str, RuntimeTask] = {}
        name_to_id: Dict[str, str] = {}
        for i, dt in enumerate(valid_tasks):
            task_id = f"{session_id}/{i:03d}_{dt.task_name}"
            config = effective_types[dt.task_name]
            name_to_id[dt.task_name] = task_id
            tasks[task_id] = RuntimeTask(
                task_id=task_id,
                task_name=dt.task_name,
                instruction=dt.instruction,
                depends_on=dt.depends_on or list(config.depends_on),
                depends_on_ids=[],  # resolved below
                input_refs=dt.input_refs,
                mandatory=dt.mandatory if dt.mandatory is not None else config.mandatory,
                context_hints=dt.context_hints,
                config=config,
                is_adhoc=dt.task_name in adhoc_task_names,
            )

        # Resolve depends_on to task_ids
        for task in tasks.values():
            resolved_ids = []
            for dep_name in task.depends_on:
                dep_id = name_to_id.get(dep_name)
                if dep_id:
                    resolved_ids.append(dep_id)
                else:
                    logger.warning(
                        "Task '%s' depends on '%s' which was not instantiated",
                        task.task_name, dep_name
                    )
            task.depends_on_ids = resolved_ids

        # Identify ready tasks (no unresolved deps)
        ready_tasks = self.get_ready_tasks_from(tasks)
        pending_tasks = [t for t in tasks.values() if t not in ready_tasks]

        return RuntimeTaskGraph(
            session_id=session_id,
            tasks=tasks,
            name_to_id=name_to_id,
            ready_tasks=ready_tasks,
            pending_tasks=pending_tasks,
        )

    def get_ready_tasks_from(self, tasks: Dict[str, RuntimeTask]) -> List[RuntimeTask]:
        """Return tasks whose all depends_on_ids are in 'complete' state."""
        completed_ids = {tid for tid, t in tasks.items() if t.status == "complete"}
        ready = []
        for task in tasks.values():
            if task.status != "pending":
                continue
            if all(dep_id in completed_ids for dep_id in task.depends_on_ids):
                ready.append(task)
        return ready

    def get_ready_tasks(self, graph: RuntimeTaskGraph) -> List[RuntimeTask]:
        """Return tasks whose depends_on are all in completed state."""
        return self.get_ready_tasks_from(graph.tasks)

    def mark_complete(self, graph: RuntimeTaskGraph, task_id: str) -> None:
        """Mark task complete, update graph state."""
        if task_id in graph.tasks:
            graph.tasks[task_id].status = "complete"
            # Update ready/pending lists
            graph.ready_tasks = [t for t in graph.ready_tasks if t.task_id != task_id]
            graph.pending_tasks = [t for t in graph.pending_tasks if t.task_id != task_id]
            # Check for newly unblocked tasks
            newly_ready = self.get_ready_tasks(graph)
            for t in newly_ready:
                if t not in graph.ready_tasks:
                    graph.ready_tasks.append(t)
            logger.debug("Task complete: %s", task_id)

    def add_tasks_batch(
        self,
        graph: RuntimeTaskGraph,
        adds: List[dict],
        effective_types: Dict[str, TaskTypeConfig],
    ) -> List[RuntimeTask]:
        """Append new tasks to a live RuntimeTaskGraph mid-session.

        Used by PrimaryAgent.replan() to grow the DAG in response to completed
        work. Each entry in ``adds`` is a dict with keys:
          - task_name:  unique name for the new task
          - task_type:  must exist in ``effective_types``
          - instruction: natural-language instruction for the sub-agent
          - depends_on: list of task_names (may reference existing tasks OR
                        other entries in this same batch — forward refs OK)
          - mandatory:  optional bool override; defaults to task_type's flag

        Validation and commit strategy:
          1. Reject entries whose task_type is unknown, whose task_name
             collides with an existing task (in the graph or earlier in this
             batch), or whose deps reference a terminally-failed task
             (failed/timeout) — the new task would hang forever.
          2. Commit in topological rounds: in each round, commit every
             remaining entry whose deps are all satisfied (already in
             graph.name_to_id, which includes entries committed earlier in
             this batch). Repeat until no progress.
          3. Entries still remaining after the loop are dropped and logged —
             this naturally rejects cycles (A depends on B, B depends on A
             both sit unresolved forever) and references to non-existent
             tasks.

        Returns the list of successfully committed RuntimeTasks. Never raises
        — replan must never block a session.
        """
        if not adds:
            return []

        failed_dep_names = {
            t.task_name for t in graph.tasks.values()
            if t.status in ("failed", "timeout")
        }
        seen_names: Set[str] = set(graph.name_to_id.keys())
        candidates: List[dict] = []
        for add in adds:
            try:
                name = str(add.get("task_name", "")).strip()
                task_type = str(add.get("task_type", "")).strip()
                instruction = str(add.get("instruction", "")).strip()
                deps = add.get("depends_on") or []
                if not isinstance(deps, list):
                    deps = []
            except Exception as e:
                logger.warning("add_tasks_batch: malformed add entry (%s) — skipping", e)
                continue

            if not name or not task_type or not instruction:
                logger.warning(
                    "add_tasks_batch: missing name/task_type/instruction — skipping: %r", add,
                )
                continue
            if task_type not in effective_types:
                logger.warning(
                    "add_tasks_batch: unknown task_type '%s' for new task '%s' — skipping",
                    task_type, name,
                )
                continue
            if name in seen_names:
                logger.warning(
                    "add_tasks_batch: task_name '%s' already exists — skipping duplicate",
                    name,
                )
                continue
            if any(d in failed_dep_names for d in deps):
                bad = [d for d in deps if d in failed_dep_names]
                logger.warning(
                    "add_tasks_batch: task '%s' depends on failed/timeout task(s) %s — "
                    "would hang forever, skipping",
                    name, bad,
                )
                continue

            seen_names.add(name)
            candidates.append({
                "task_name": name,
                "task_type": task_type,
                "instruction": instruction,
                "depends_on": [str(d).strip() for d in deps if str(d).strip()],
                "mandatory": add.get("mandatory"),
            })

        committed: List[RuntimeTask] = []
        remaining = list(candidates)
        while remaining:
            progress = False
            still: List[dict] = []
            for add in remaining:
                deps = add["depends_on"]
                if all(d in graph.name_to_id for d in deps):
                    config = effective_types[add["task_type"]]
                    idx = len(graph.tasks)
                    task_id = f"{graph.session_id}/{idx:03d}_{add['task_name']}"
                    mandatory = add["mandatory"]
                    if mandatory is None:
                        mandatory = config.mandatory
                    depends_on_ids = [graph.name_to_id[d] for d in deps]
                    task = RuntimeTask(
                        task_id=task_id,
                        task_name=add["task_name"],
                        instruction=add["instruction"],
                        depends_on=list(deps),
                        depends_on_ids=depends_on_ids,
                        input_refs=[],
                        mandatory=bool(mandatory),
                        config=config,
                        is_adhoc=True,
                    )
                    graph.tasks[task_id] = task
                    graph.name_to_id[add["task_name"]] = task_id
                    graph.pending_tasks.append(task)
                    committed.append(task)
                    progress = True
                    logger.info(
                        "Replan add: '%s' (type=%s, deps=%s) → %s",
                        add["task_name"], add["task_type"], deps, task_id,
                    )
                else:
                    still.append(add)
            remaining = still
            if not progress:
                break

        for add in remaining:
            unresolved = [d for d in add["depends_on"] if d not in graph.name_to_id]
            logger.warning(
                "add_tasks_batch: dropping '%s' — unresolved deps %s "
                "(nonexistent task or cycle in batch)",
                add["task_name"], unresolved,
            )

        if committed:
            newly_ready = self.get_ready_tasks(graph)
            for t in newly_ready:
                if t not in graph.ready_tasks:
                    graph.ready_tasks.append(t)

        return committed

    def mark_failed(self, graph: RuntimeTaskGraph, task_id: str) -> None:
        """Mark task failed. If mandatory, flag graph as partial."""
        if task_id in graph.tasks:
            task = graph.tasks[task_id]
            task.status = "failed"
            graph.ready_tasks = [t for t in graph.ready_tasks if t.task_id != task_id]
            graph.pending_tasks = [t for t in graph.pending_tasks if t.task_id != task_id]
            if task.mandatory:
                graph.is_partial = True
                logger.warning("Mandatory task failed: %s — graph marked partial", task_id)
            else:
                logger.warning("Optional task failed: %s", task_id)

    def snapshot_graph(self, graph: RuntimeTaskGraph) -> dict:
        """
        Serialize a RuntimeTaskGraph to a JSON-safe dict for resumption.
        Captures task statuses, instructions, and configs so pending tasks
        can be re-executed in a future session without re-decomposition.
        """
        tasks_data = {}
        for task_id, task in graph.tasks.items():
            tasks_data[task_id] = {
                "task_id": task.task_id,
                "task_name": task.task_name,
                "instruction": task.instruction,
                "depends_on": task.depends_on,
                "depends_on_ids": task.depends_on_ids,
                "input_refs": task.input_refs,
                "status": task.status,
                "mandatory": task.mandatory,
                "context_hints": task.context_hints,
                "config": task.config.model_dump() if task.config else None,
                "is_adhoc": task.is_adhoc,
                "attempt_count": task.attempt_count,
                "validation_feedback": task.validation_feedback,
                "hitl_ask_count": task.hitl_ask_count,
            }
        return {
            "session_id": graph.session_id,
            "tasks": tasks_data,
            "name_to_id": graph.name_to_id,
            "is_partial": graph.is_partial,
        }

    def restore_graph(self, snapshot: dict) -> RuntimeTaskGraph:
        """
        Rebuild a RuntimeTaskGraph from a snapshot dict.
        Completed tasks keep their status; pending/failed tasks are reset to pending
        so they will be re-executed by the wave loop.
        """
        tasks: Dict[str, RuntimeTask] = {}
        for task_id, data in snapshot["tasks"].items():
            config = TaskTypeConfig(**data["config"]) if data.get("config") else None
            # Reset running/failed tasks to pending so they re-execute on resume
            status = data["status"]
            if status in ("running", "failed", "timeout"):
                status = "pending"
            tasks[task_id] = RuntimeTask(
                task_id=data["task_id"],
                task_name=data["task_name"],
                instruction=data["instruction"],
                depends_on=data["depends_on"],
                depends_on_ids=data["depends_on_ids"],
                input_refs=data["input_refs"],
                status=status,
                mandatory=data["mandatory"],
                context_hints=data["context_hints"],
                config=config,
                is_adhoc=data.get("is_adhoc", False),
                attempt_count=data.get("attempt_count", 0),
                validation_feedback=data.get("validation_feedback"),
                hitl_ask_count=data.get("hitl_ask_count", 0),
            )

        ready_tasks = self.get_ready_tasks_from(tasks)
        pending_tasks = [
            t for t in tasks.values()
            if t.status == "pending" and t not in ready_tasks
        ]

        logger.info(
            "Restored graph for session %s: %d complete, %d ready, %d pending",
            snapshot["session_id"],
            sum(1 for t in tasks.values() if t.status == "complete"),
            len(ready_tasks),
            len(pending_tasks),
        )

        return RuntimeTaskGraph(
            session_id=snapshot["session_id"],
            tasks=tasks,
            name_to_id=snapshot["name_to_id"],
            ready_tasks=ready_tasks,
            pending_tasks=pending_tasks,
            is_partial=snapshot.get("is_partial", False),
        )
