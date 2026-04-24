"""LearningEngine — autonomic self-evolution engine for cortex.yaml deltas
and code persistence.

Both concerns live here because they are the same thing: the framework
learning from usage and evolving its own capabilities over time.

The engine is *signal-driven* and *consent-free*. Gating is performed by
:class:`CortexFramework` at end-of-session using the autonomic learning
gate (intent mode + complexity score + validation score); this module
trusts that its callers have already passed that gate.

Flow:

  1. A session completes with ad-hoc tasks (tasks not defined in
     ``cortex.yaml``) that cleared the framework gate.
  2. Framework calls :meth:`persist_evolution` with the completed envelopes
     and the computed complexity score.
  3. LearningEngine stages each envelope as a :class:`DeltaProposal`
     (task type + optional script).
  4. Any ``generated_script`` is persisted to :class:`AgentCodeStore`
     immediately so future sessions can reuse it.
  5. A draft :class:`Blueprint` is seeded from the session's execution data
     under a reserved ``drafts/`` namespace so guidance starts accumulating
     before the task type is formally promoted to ``cortex.yaml``.
  6. Proposals accumulate confirmations from distinct users. Once the
     configured confidence threshold is met,
     :meth:`apply_delta` merges the task type into ``cortex.yaml`` and
     re-links the draft blueprint to its permanent location.

Anti-abuse: one ``user_id`` counts as one confirmation regardless of how
many times that user runs the same ad-hoc task.
"""
import logging
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from cortex.config.schema import LearningConfig
from cortex.exceptions import CortexDeltaError
from cortex.modules.history_store import TaskCompletion
from cortex.modules.validation_agent import ValidationReport

logger = logging.getLogger(__name__)


# ─────────────────────────────── data classes ────────────────────────────────

@dataclass
class SessionConfirmation:
    session_id: str
    user_id: str
    confirmed: bool
    validation_score: float
    complexity_score: float = 0.0


@dataclass
class DeltaProposal:
    task_name: str
    description: str
    output_format: str = "text"
    mandatory: bool = False        # ad-hoc tasks are optional by default
    complexity: str = "adaptive"
    capability_hint: str = "auto"
    tool_servers: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    learned_from_sessions: List[SessionConfirmation] = field(default_factory=list)
    confidence: str = "low"        # high | medium | low
    confirmations: int = 0         # distinct user_id count
    # Code persistence fields
    generated_script: Optional[str] = None      # raw Python source that was generated
    script_path: Optional[str] = None           # filesystem path once persisted
    script_requirements: List[str] = field(default_factory=list)
    is_adhoc: bool = False                       # True when learned from a runtime ad-hoc task
    # Optional tool_servers entries to merge into cortex.yaml on apply
    # {server_name: {url: ..., transport: ..., ...}}
    tool_servers_config: Optional[dict] = None
    # Draft blueprint reference (``drafts/{task_name}__{hash}``) created on
    # first stage. Promoted to a permanent ``blueprint:`` reference when
    # ``apply_delta`` writes this task into cortex.yaml.
    draft_blueprint: Optional[str] = None
    # Peak runtime complexity score observed across all confirmations —
    # used for observability and conflict resolution.
    complexity_score: float = 0.0

    def compute_confidence(self) -> str:
        if self.confirmations >= 5:
            return "high"
        elif self.confirmations >= 3:
            return "medium"
        return "low"


@dataclass
class ApplyResult:
    applied: List[str]    # task type names written into cortex.yaml
    skipped: List[str]    # skipped (confidence below threshold)
    backup_path: str
    timestamp: str


@dataclass
class EvolutionResult:
    """Result returned by persist_evolution()."""
    staged_tasks: List[str]           # task type names staged as proposals
    scripts_persisted: List[str]      # task names whose scripts were saved
    drafts_seeded: List[str]          # task names with a new draft blueprint
    auto_applied: List[str]           # task names immediately applied (auto_apply_delta)
    message: str                      # human-readable summary for the user


# ─────────────────────────────── engine ──────────────────────────────────────

class LearningEngine:
    """Autonomic evolution engine.

    Responsibilities:

      - Accumulate :class:`DeltaProposal` entries for new/ad-hoc task types
        in ``pending.yaml``.
      - Persist generated Python scripts to :class:`AgentCodeStore`.
      - Seed draft blueprints from session execution data so guidance begins
        accumulating before a task is promoted.
      - Apply proposals to ``cortex.yaml`` once the configured confidence
        threshold is met.
      - Hot-reload the running framework config after apply.

    Gating is the caller's responsibility: this engine runs on every call
    it receives. The framework's autonomic gate (``complexity_threshold`` +
    ``validation_threshold``, chat-turn skip, principal identity check) is
    the single source of truth for when to engage the engine.

    Anti-abuse: one ``user_id`` counts as one confirmation toward the
    confidence threshold regardless of how many sessions it contributes.
    """

    def __init__(
        self,
        delta_path: str,
        config: LearningConfig,
        code_store=None,       # cortex.sandbox.code_store.AgentCodeStore (optional)
        blueprint_store=None,  # cortex.modules.blueprint_store.BlueprintStore (optional)
    ):
        self._delta_path = Path(delta_path)
        self._delta_path.mkdir(parents=True, exist_ok=True)
        self._pending_path = self._delta_path / "pending.yaml"
        self._history_path = self._delta_path / "history"
        self._history_path.mkdir(exist_ok=True)
        self._config = config
        self._code_store = code_store
        self._blueprint_store = blueprint_store
        self._reload_callback = None

    def set_reload_callback(self, callback) -> None:
        self._reload_callback = callback

    def set_code_store(self, code_store) -> None:
        """Inject AgentCodeStore after construction (e.g. when sandbox is enabled)."""
        self._code_store = code_store

    def set_blueprint_store(self, blueprint_store) -> None:
        """Inject BlueprintStore after construction so draft blueprints can be seeded."""
        self._blueprint_store = blueprint_store

    # ── primary entry point ───────────────────────────────────────────────────

    async def persist_evolution(
        self,
        ad_hoc_envelopes,        # List[ResultEnvelope] — is_adhoc=True, status="complete"
        user_id: str,
        validation_report: ValidationReport,
        cortex_yaml_path: str,
        complexity_score: float = 0.0,
        decomposed_tasks: Optional[list] = None,
    ) -> EvolutionResult:
        """Stage one or more ad-hoc task envelopes as delta proposals.

        Called by the framework when the autonomic learning gate has cleared
        (intent is task/hybrid, complexity ≥ threshold, validation ≥ threshold).
        The gate is the caller's responsibility; this method does not
        re-validate gating conditions.

        For each ad-hoc envelope:

          1. Build a :class:`DeltaProposal` (task type definition).
          2. If a ``generated_script`` is attached, persist it to
             :class:`AgentCodeStore` and link the script path + handler into
             the proposal.
          3. Seed a draft blueprint under ``drafts/{task_name}__{hash}``
             so guidance starts accumulating immediately.
          4. Stage the proposal into ``pending.yaml``.

        If ``auto_apply_delta`` is enabled and the confidence threshold is
        already met (e.g. after the third distinct principal has run the same
        ad-hoc task), :meth:`apply_delta` runs immediately and the draft
        blueprint is promoted to a permanent reference on the new task type.
        """
        if not self._config.enabled:
            return EvolutionResult(
                staged_tasks=[], scripts_persisted=[], drafts_seeded=[],
                auto_applied=[],
                message="Learning is disabled (learning.enabled: false).",
            )

        staged_tasks: List[str] = []
        scripts_persisted: List[str] = []
        drafts_seeded: List[str] = []

        for envelope in ad_hoc_envelopes:
            if envelope.status != "complete":
                continue

            task_name = envelope.task_id.split("/")[-1]
            # Strip leading index (e.g. "002_fetch_data" → "fetch_data")
            if "_" in task_name and task_name.split("_")[0].isdigit():
                task_name = "_".join(task_name.split("_")[1:])

            description = envelope.context_hints.get("task_description", f"Ad-hoc task: {task_name}")
            output_format = envelope.output_type or "text"

            # ── persist script ──────────────────────────────────────────────
            script_path: Optional[str] = None
            requirements: List[str] = []

            if envelope.generated_script and self._code_store:
                try:
                    requirements = self._code_store._extract_requirements_from_source(
                        envelope.generated_script
                    )
                    record = self._code_store.persist(
                        task_name=task_name,
                        source_code=envelope.generated_script,
                        description=description,
                        requirements=requirements,
                    )
                    script_path = record.script_path
                    scripts_persisted.append(task_name)
                    logger.info("LearningEngine: persisted script for task '%s' at %s", task_name, script_path)
                except Exception as e:
                    logger.warning("LearningEngine: script persist failed for '%s': %s", task_name, e)

            # ── seed draft blueprint ────────────────────────────────────────
            draft_ref: Optional[str] = None
            if self._blueprint_store:
                try:
                    draft_ref = await self._seed_draft_blueprint(
                        task_name=task_name,
                        description=description,
                        envelope=envelope,
                        validation_report=validation_report,
                        has_script=bool(script_path),
                    )
                    if draft_ref:
                        drafts_seeded.append(task_name)
                except Exception as e:
                    logger.warning("LearningEngine: draft blueprint seed failed for '%s': %s", task_name, e)

            # ── build & stage proposal ──────────────────────────────────────
            capability = "code_exec" if envelope.generated_script else "llm_synthesis"
            complexity = "scripted" if script_path else "adaptive"

            proposal = DeltaProposal(
                task_name=task_name,
                description=description,
                output_format=output_format,
                mandatory=False,
                complexity=complexity,
                capability_hint=capability,
                learned_from_sessions=[
                    SessionConfirmation(
                        session_id=envelope.session_id,
                        user_id=user_id,
                        confirmed=True,
                        validation_score=validation_report.composite_score or 0.0,
                        complexity_score=complexity_score,
                    )
                ],
                confirmations=1,
                generated_script=envelope.generated_script,
                script_path=script_path,
                script_requirements=requirements,
                is_adhoc=True,
                draft_blueprint=draft_ref,
                complexity_score=complexity_score,
            )
            proposal.confidence = proposal.compute_confidence()

            await self.stage_delta(proposal)
            staged_tasks.append(task_name)

        # ── auto-apply if enabled ───────────────────────────────────────────
        auto_applied: List[str] = []
        if self._config.auto_apply_delta and staged_tasks:
            try:
                result = await self.apply_delta(
                    delta_path=None,
                    cortex_yaml_path=cortex_yaml_path,
                    min_confidence=self._config.auto_apply_min_confidence,
                )
                auto_applied = result.applied
            except Exception as e:
                logger.warning("LearningEngine: auto-apply failed: %s", e)

        # Build message
        parts = []
        if staged_tasks:
            parts.append(f"Staged {len(staged_tasks)} new task type(s): {', '.join(staged_tasks)}.")
        if scripts_persisted:
            parts.append(f"Saved {len(scripts_persisted)} script(s) for reuse.")
        if drafts_seeded:
            parts.append(f"Seeded {len(drafts_seeded)} draft blueprint(s).")
        if auto_applied:
            parts.append(f"Auto-applied {len(auto_applied)} task type(s) to cortex.yaml.")
        message = " ".join(parts) if parts else "No new tasks to stage."

        return EvolutionResult(
            staged_tasks=staged_tasks,
            scripts_persisted=scripts_persisted,
            drafts_seeded=drafts_seeded,
            auto_applied=auto_applied,
            message=message,
        )

    # ── legacy evaluate_session (kept for non-ad-hoc learning path) ──────────

    async def evaluate_session(
        self,
        session_id: str,
        user_id: str,
        validation_report: ValidationReport,
        task_completion: TaskCompletion,
        config: Optional[LearningConfig] = None,
        complexity_score: float = 0.0,
    ) -> Optional[DeltaProposal]:
        """Legacy single-session evaluation path (non-ad-hoc tasks).

        Historically required positive user consent; that gate has been
        replaced by the framework-level autonomic gate. This method now
        requires only that the session ran successfully and passed
        validation. Retained for callers that evaluate sessions outside the
        main ``persist_evolution`` flow.
        """
        cfg = config or self._config
        if not cfg.enabled:
            return None
        if not validation_report.passed:
            return None
        if task_completion.completed_tasks == 0:
            return None

        proposal = DeltaProposal(
            task_name=f"learned_task_{session_id[-4:]}",
            description=f"Task pattern learned from session {session_id}",
            learned_from_sessions=[
                SessionConfirmation(
                    session_id=session_id,
                    user_id=user_id,
                    confirmed=True,
                    validation_score=validation_report.composite_score or 0.0,
                    complexity_score=complexity_score,
                )
            ],
            confirmations=1,
            complexity_score=complexity_score,
        )
        proposal.confidence = proposal.compute_confidence()
        return proposal

    # ── staging ───────────────────────────────────────────────────────────────

    async def stage_delta(self, proposal: DeltaProposal, delta_path: Optional[str] = None) -> None:
        """Merge proposal into pending.yaml with distinct user_id enforcement."""
        pending_path = Path(delta_path) / "pending.yaml" if delta_path else self._pending_path

        existing: dict = {}
        if pending_path.exists():
            with open(pending_path, "r") as f:
                existing = yaml.safe_load(f) or {}

        task_list = existing.get("task_types", [])
        task_map: Dict[str, dict] = {t["name"]: t for t in task_list}

        prop_dict = {
            "name": proposal.task_name,
            "description": proposal.description,
            "output_format": proposal.output_format,
            "mandatory": proposal.mandatory,
            "complexity": proposal.complexity,
            "capability_hint": proposal.capability_hint,
            "tool_servers": proposal.tool_servers,
            "depends_on": proposal.depends_on,
            "learned_from_sessions": [asdict(s) for s in proposal.learned_from_sessions],
            "confidence": proposal.confidence,
            "confirmations": proposal.confirmations,
            "is_adhoc": proposal.is_adhoc,
            "complexity_score": round(proposal.complexity_score, 3),
        }
        # Persist script metadata (but not the full source — it's in code_store)
        if proposal.script_path:
            prop_dict["handler"] = _script_path_to_handler(proposal.script_path)
        if proposal.script_requirements:
            prop_dict["script_requirements"] = proposal.script_requirements
        if proposal.draft_blueprint:
            prop_dict["draft_blueprint"] = proposal.draft_blueprint

        if proposal.task_name in task_map:
            existing_task = task_map[proposal.task_name]
            # Merge sessions — enforce distinct user_id
            existing_sessions = existing_task.get("learned_from_sessions", [])
            existing_user_ids = {s["user_id"] for s in existing_sessions}
            for new_session in proposal.learned_from_sessions:
                if new_session.user_id not in existing_user_ids:
                    existing_sessions.append(asdict(new_session))
                    existing_user_ids.add(new_session.user_id)
            existing_task["learned_from_sessions"] = existing_sessions
            existing_task["confirmations"] = len(existing_user_ids)
            count = existing_task["confirmations"]
            existing_task["confidence"] = "high" if count >= 5 else "medium" if count >= 3 else "low"
            # Update handler if newly available
            if proposal.script_path and "handler" not in existing_task:
                existing_task["handler"] = _script_path_to_handler(proposal.script_path)
                existing_task["complexity"] = "scripted"
            # Track the peak complexity score across confirmations
            prior_score = float(existing_task.get("complexity_score", 0.0) or 0.0)
            existing_task["complexity_score"] = round(
                max(prior_score, proposal.complexity_score), 3
            )
            # Preserve the draft blueprint reference created at first stage
            if proposal.draft_blueprint and "draft_blueprint" not in existing_task:
                existing_task["draft_blueprint"] = proposal.draft_blueprint
            task_map[proposal.task_name] = existing_task
        else:
            task_map[proposal.task_name] = prop_dict

        existing["task_types"] = list(task_map.values())
        with open(pending_path, "w") as f:
            yaml.dump(existing, f, default_flow_style=False, sort_keys=False)
        logger.info("Staged delta for task type: %s", proposal.task_name)

    # ── draft blueprint seeding ───────────────────────────────────────────────

    async def _seed_draft_blueprint(
        self,
        task_name: str,
        description: str,
        envelope,
        validation_report: ValidationReport,
        has_script: bool,
    ) -> Optional[str]:
        """Create a draft blueprint from first-session execution data.

        Writes the blueprint under ``drafts/{task_name}__{hash}`` so it never
        collides with a permanent blueprint if the task gets promoted later.
        The draft is seeded with:

          - discovery hints (adaptive) or topology (scripted) from the
            observed ``tool_trace`` — whichever matches the task complexity;
          - any validator findings as initial ``don'ts`` so the next run
            starts with known failure modes in mind;
          - an initial ``lesson_summary`` recording the session context.

        Returns the blueprint reference (relative name used by
        :class:`BlueprintStore`) or ``None`` on failure.
        """
        if self._blueprint_store is None:
            return None
        from cortex.modules.blueprint_store import BlueprintStore, Blueprint

        deterministic = has_script  # scripted → topology-locked; adaptive → hints
        draft_name = f"drafts/{BlueprintStore.generate_unique_name(task_name)}"

        bp = await self._blueprint_store.load(draft_name)
        if bp is None:
            bp = Blueprint(name=draft_name, task_name=task_name, version=1)
        bp.deterministic = deterministic

        trace_lines = [
            t for t in (getattr(envelope, "tool_trace", []) or [])
            if isinstance(t, str) and t.strip()
        ]
        trace_block = "\n".join(f"- {t}" for t in trace_lines[:12])

        if deterministic:
            if not bp.topology and trace_block:
                bp.topology = (
                    "Observed tool sequence (first session):\n" + trace_block
                )
        else:
            if not bp.discovery_hints and trace_block:
                bp.discovery_hints = (
                    "Prior tool usage (first session):\n" + trace_block
                )

        # Seed don'ts from validator findings.
        max_chars = max(80, int(getattr(self._config, "max_lesson_chars", 500) or 500))
        if validation_report and getattr(validation_report, "findings", None):
            seen = {d.strip().lower() for d in bp.donts}
            for f in validation_report.findings[:5]:
                issue = (getattr(f, "issue", "") or "").strip()
                suggestion = (getattr(f, "suggestion", "") or "").strip()
                text = (
                    f"{issue} — {suggestion}" if issue and suggestion else issue
                )
                if not text:
                    continue
                text = text[:max_chars]
                if text.lower() not in seen:
                    bp.donts.append(text)
                    seen.add(text.lower())

        if not bp.lessons_learned:
            lesson = (
                f"Seeded from ad-hoc session {getattr(envelope, 'session_id', '?')}: "
                f"{description}"
            )[:max_chars]
            bp.lessons_learned.append(f"[v{bp.version}] {lesson}")

        # Stamp a tentative successful-run timestamp so staleness checks can
        # reason about the draft immediately.
        bp.last_successful_run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        await self._blueprint_store.save(bp)
        logger.info("LearningEngine: seeded draft blueprint '%s'", draft_name)
        return draft_name

    # ── apply ─────────────────────────────────────────────────────────────────

    async def apply_delta(
        self,
        delta_path: Optional[str],
        cortex_yaml_path: str,
        min_confidence: Optional[str] = None,
    ) -> ApplyResult:
        """Merge confidence-cleared proposals from ``pending.yaml`` into
        ``cortex.yaml``.

          1. Load pending.yaml.
          2. Filter by ``min_confidence``.
          3. Merge into cortex.yaml — writing handler + ``complexity: scripted``
             when a script is associated with the task type, and promoting
             any ``draft_blueprint`` reference to a permanent ``blueprint:``
             field.
          4. Backup original, write updated.
          5. Archive applied proposal; truncate pending.
          6. Trigger hot-reload.
        """
        pending_p = Path(delta_path) / "pending.yaml" if delta_path else self._pending_path
        if not pending_p.exists():
            return ApplyResult(applied=[], skipped=[], backup_path="", timestamp="")

        with open(pending_p, "r") as f:
            pending = yaml.safe_load(f) or {}

        pending_tasks = pending.get("task_types", [])
        confidence_order = {"high": 3, "medium": 2, "low": 1}
        min_level = confidence_order.get(
            min_confidence or self._config.auto_apply_min_confidence, 3
        )

        to_apply = []
        skipped = []
        for task in pending_tasks:
            task_conf = confidence_order.get(task.get("confidence", "low"), 1)
            if task_conf >= min_level:
                to_apply.append(task)
            else:
                skipped.append(task.get("name", ""))

        if not to_apply:
            return ApplyResult(applied=[], skipped=skipped, backup_path="", timestamp="")

        cortex_path = Path(cortex_yaml_path)
        if not cortex_path.exists():
            raise CortexDeltaError(f"cortex.yaml not found at: {cortex_yaml_path}")

        with open(cortex_path, "r") as f:
            cortex_data = yaml.safe_load(f) or {}

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = str(cortex_path) + f".bak.{ts}"
        shutil.copy2(cortex_path, backup_path)

        existing_tasks = cortex_data.get("task_types", [])
        existing_map = {t["name"]: t for t in existing_tasks}
        applied_names: List[str] = []

        for new_task in to_apply:
            # Strip internal learning metadata before writing to cortex.yaml
            clean_task = {
                k: v for k, v in new_task.items()
                if k not in (
                    "learned_from_sessions", "confidence", "confirmations",
                    "is_adhoc", "script_requirements", "generated_script",
                    "complexity_score", "draft_blueprint",
                )
            }
            # If a handler is defined, upgrade complexity to scripted
            if clean_task.get("handler"):
                clean_task["complexity"] = "scripted"
                # Also tell code_store it's been wired
                if self._code_store:
                    try:
                        self._code_store.mark_added_to_yaml(new_task["name"])
                    except Exception:
                        pass
            # Promote any draft blueprint to a permanent reference.
            draft_ref = new_task.get("draft_blueprint")
            if draft_ref and self._blueprint_store:
                try:
                    promoted_ref = await self._promote_draft_blueprint(
                        draft_ref=draft_ref,
                        task_name=new_task["name"],
                    )
                    if promoted_ref:
                        clean_task["blueprint"] = promoted_ref
                except Exception as e:
                    logger.warning(
                        "Failed to promote draft blueprint '%s' for task '%s': %s",
                        draft_ref, new_task["name"], e,
                    )
            existing_map[new_task["name"]] = clean_task
            applied_names.append(new_task["name"])

        cortex_data["task_types"] = list(existing_map.values())

        # Also persist any new tool_servers entries (e.g. from ant colony)
        for new_task in to_apply:
            ts_config = new_task.get("tool_servers_config")
            if ts_config and isinstance(ts_config, dict):
                cortex_data.setdefault("tool_servers", {}).update(ts_config)

        with open(cortex_path, "w") as f:
            yaml.dump(cortex_data, f, default_flow_style=False, sort_keys=False)

        # Archive
        archive_path = self._history_path / f"{ts}.yaml"
        with open(archive_path, "w") as f:
            yaml.dump({"applied": to_apply, "timestamp": ts}, f)

        # Truncate pending
        remaining = [t for t in pending_tasks if t.get("name") not in applied_names]
        pending["task_types"] = remaining
        with open(pending_p, "w") as f:
            yaml.dump(pending, f, default_flow_style=False, sort_keys=False)

        # Hot-reload
        if self._reload_callback:
            try:
                from cortex.config.loader import load_config
                new_config = load_config(cortex_yaml_path)
                self._reload_callback(new_config)
            except Exception as e:
                logger.warning("Hot-reload failed after delta apply: %s", e)

        logger.info("Applied delta: %s", applied_names)
        return ApplyResult(
            applied=applied_names,
            skipped=skipped,
            backup_path=backup_path,
            timestamp=ts,
        )

    async def _promote_draft_blueprint(
        self,
        draft_ref: str,
        task_name: str,
    ) -> Optional[str]:
        """Copy a ``drafts/`` blueprint to its permanent location.

        Returns the permanent blueprint reference (the new name) or ``None``
        if promotion failed (in which case the caller should leave the task
        without a blueprint reference and let the blueprint auto-update path
        recreate it on the next run).
        """
        if self._blueprint_store is None:
            return None
        draft = await self._blueprint_store.load(draft_ref)
        if draft is None:
            return None

        from cortex.modules.blueprint_store import BlueprintStore
        permanent_name = BlueprintStore.generate_unique_name(task_name)
        draft.name = permanent_name
        await self._blueprint_store.save(draft)
        return permanent_name

    # ── misc ──────────────────────────────────────────────────────────────────

    def hot_reload(self, new_config) -> None:
        """Hook point — framework.py CortexFramework.hot_reload() recompiles the graph."""
        logger.info("Hot-reload triggered with new config")

    async def check_staleness(self, config: Optional[LearningConfig] = None) -> List[str]:
        """Return task type names unused for > staleness_warning_days."""
        # Actual staleness tracking is delegated to HistoryStore
        return []

    def resolve_conflicts(self, proposals: List[DeltaProposal]) -> List[DeltaProposal]:
        """When multiple proposals conflict for the same task type.

        Prefer higher confidence > more distinct confirmations > higher avg validation score.
        """
        by_name: Dict[str, List[DeltaProposal]] = {}
        for p in proposals:
            by_name.setdefault(p.task_name, []).append(p)

        resolved = []
        confidence_order = {"high": 3, "medium": 2, "low": 1}
        for name, group in by_name.items():
            if len(group) == 1:
                resolved.append(group[0])
            else:
                def sort_key(p: DeltaProposal):
                    avg_score = (
                        sum(s.validation_score for s in p.learned_from_sessions) /
                        len(p.learned_from_sessions)
                        if p.learned_from_sessions else 0.0
                    )
                    return (confidence_order.get(p.confidence, 0), p.confirmations, avg_score)
                group.sort(key=sort_key, reverse=True)
                logger.warning(
                    "Conflict resolved for task '%s': chose proposal with confidence=%s, confirmations=%d",
                    name, group[0].confidence, group[0].confirmations,
                )
                resolved.append(group[0])

        return resolved


# ── helpers ───────────────────────────────────────────────────────────────────

def _script_path_to_handler(script_path: str) -> str:
    """Convert an absolute script path to a dotted handler string.

    e.g. ``/data/storage/agent_tools/fetch_data_abc12345.py`` →
    ``agent_tools.fetch_data_abc12345.run``.
    """
    stem = Path(script_path).stem   # fetch_data_abc12345
    # Find the "agent_tools" directory component
    parts = Path(script_path).parts
    try:
        idx = next(i for i, p in enumerate(parts) if p == "agent_tools")
        # Everything from agent_tools onward
        relative = ".".join(parts[idx:]).removesuffix(".py")
        return f"{relative}.run"
    except StopIteration:
        return f"agent_tools.{stem}.run"
