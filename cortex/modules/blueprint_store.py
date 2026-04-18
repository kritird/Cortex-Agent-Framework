"""BlueprintStore — per-task markdown blueprints with fs / backend storage.

A *blueprint* is a human-readable markdown file that captures the workflow,
dos/don'ts, user clarifications, and accumulated lessons for a single task
type. YAML references a blueprint by name via `task_type.blueprint`. The
framework loads it lazily and injects the content into the primary agent's
system prompt, so the next run is steered by guidance learned from prior runs.

The store supports two storage modes (configured in cortex.yaml):

- ``filesystem``  — blueprints live as ``.md`` files under a directory
  (default: ``{storage.base_path}/blueprints``).
- ``backend``     — blueprints are persisted via the configured
  :class:`StorageBackend` (Redis / SQLite) under ``blueprint:{name}`` keys.

Both modes expose the same ``load`` / ``save`` / ``append_lesson`` API.

Blueprint file format:

    ---
    name: <unique file name, no extension>
    task_name: <task_type.name this blueprint steers>
    version: 3
    updated_at: 2026-04-10T12:00:00Z
    last_successful_run_at: 2026-04-10T11:00:00Z
    ---

    ## Topology              ← pinned/scripted tasks only
    ...

    ## Discovery Hints       ← adaptive tasks only
    ...

    ## Preconditions
    - ...

    ## Known Failure Modes
    - ...

    ## Dos
    - ...

    ## Don'ts
    - ...

    ## Clarifications
    - Q: ...
      A: ...

    ## Lessons Learned
    - [v2] ...
    - [v3] ...
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from cortex.storage.base import StorageBackend

logger = logging.getLogger(__name__)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class Blueprint:
    """In-memory representation of a task blueprint."""
    name: str                       # unique file/key name, no extension
    task_name: str                  # task type this blueprint belongs to
    deterministic: bool = False     # topology-locked flag: True for complexity == 'scripted' or 'pinned' (both use topology section)
    version: int = 1
    updated_at: str = ""            # ISO-8601 UTC
    last_successful_run_at: str = ""  # ISO-8601 UTC — source of truth for staleness

    # Topology / Discovery (mutually exclusive based on deterministic flag)
    topology: str = ""              # pinned/scripted: observed subtask dep graph (hard constraints)
    discovery_hints: str = ""       # adaptive: soft navigation hints for scout

    # Common sections (both task types)
    preconditions: List[str] = field(default_factory=list)
    known_failure_modes: List[str] = field(default_factory=list)
    dos: List[str] = field(default_factory=list)
    donts: List[str] = field(default_factory=list)
    clarifications: List[str] = field(default_factory=list)
    lessons_learned: List[str] = field(default_factory=list)

    # ── staleness ────────────────────────────────────────────────────────────

    def is_stale(self, staleness_warning_days: int) -> bool:
        """True when the blueprint has never been successfully run, or the last
        successful run is older than ``staleness_warning_days``."""
        if not self.last_successful_run_at:
            return True
        try:
            last = datetime.fromisoformat(
                self.last_successful_run_at.replace("Z", "+00:00")
            )
            cutoff = datetime.now(timezone.utc) - timedelta(days=staleness_warning_days)
            return last < cutoff
        except Exception:
            return True

    # ── serialisation ────────────────────────────────────────────────────────

    def to_markdown(self) -> str:
        if not self.updated_at:
            self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines = [
            "---",
            f"name: {self.name}",
            f"task_name: {self.task_name}",
            f"version: {self.version}",
            f"updated_at: {self.updated_at}",
            f"last_successful_run_at: {self.last_successful_run_at or ''}",
            "---",
            "",
        ]

        # Topology / Discovery Hints — mutually exclusive
        if self.deterministic:
            lines += ["## Topology", self.topology or "_(none yet)_", ""]
        else:
            lines += ["## Discovery Hints", self.discovery_hints or "_(none yet)_", ""]

        # Preconditions
        lines.append("## Preconditions")
        if self.preconditions:
            lines.extend(f"- {item}" for item in self.preconditions)
        else:
            lines.append("_(none)_")
        lines.append("")

        # Known Failure Modes
        lines.append("## Known Failure Modes")
        if self.known_failure_modes:
            lines.extend(f"- {item}" for item in self.known_failure_modes)
        else:
            lines.append("_(none)_")
        lines.append("")

        # Dos
        lines.append("## Dos")
        if self.dos:
            lines.extend(f"- {item}" for item in self.dos)
        else:
            lines.append("_(none)_")
        lines.append("")

        # Don'ts
        lines.append("## Don'ts")
        if self.donts:
            lines.extend(f"- {item}" for item in self.donts)
        else:
            lines.append("_(none)_")
        lines.append("")

        # Clarifications
        lines.append("## Clarifications")
        if self.clarifications:
            lines.extend(f"- {item}" for item in self.clarifications)
        else:
            lines.append("_(none)_")
        lines.append("")

        # Lessons Learned
        lines.append("## Lessons Learned")
        if self.lessons_learned:
            lines.extend(f"- {item}" for item in self.lessons_learned)
        else:
            lines.append("_(none)_")

        return "\n".join(lines) + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "Blueprint":
        m = _FRONTMATTER_RE.match(text)
        if not m:
            raise ValueError("Blueprint missing YAML frontmatter")
        fm_block, body = m.group(1), m.group(2)
        meta: dict = {}
        for line in fm_block.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()

        bp = cls(
            name=meta.get("name", ""),
            task_name=meta.get("task_name", ""),
            version=int(meta.get("version", "1") or 1),
            updated_at=meta.get("updated_at", ""),
            last_successful_run_at=meta.get("last_successful_run_at", ""),
        )
        section = None
        buf: List[str] = []

        def flush():
            nonlocal buf, section
            if section is None:
                return
            text_block = "\n".join(buf).strip()
            is_none_placeholder = text_block in ("_(none yet)_", "_(none)_")
            items = [
                ln[2:].strip()
                for ln in text_block.splitlines()
                if ln.strip().startswith("- ")
            ]
            if section == "Topology":
                bp.topology = "" if is_none_placeholder else text_block
            elif section == "Discovery Hints":
                bp.discovery_hints = "" if is_none_placeholder else text_block
            elif section == "Preconditions":
                bp.preconditions = items
            elif section == "Known Failure Modes":
                bp.known_failure_modes = items
            elif section == "Dos":
                bp.dos = items
            elif section == "Don'ts":
                bp.donts = items
            elif section == "Clarifications":
                bp.clarifications = items
            elif section == "Lessons Learned":
                bp.lessons_learned = items
            buf.clear()

        for line in body.splitlines():
            h = re.match(r"^##\s+(.+?)\s*$", line)
            if h:
                flush()
                section = h.group(1).strip()
                continue
            buf.append(line)
        flush()
        return bp

    # ── merge ────────────────────────────────────────────────────────────────

    def merge_update(self, update: dict) -> None:
        """Apply an LLM-generated update in-place, bumping the version.

        Accepted keys (all optional):
          - topology            (str)  — replaces Topology if topology-locked and non-empty
          - discovery_hints     (str)  — replaces Discovery Hints if adaptive and non-empty
          - preconditions       (list) — new bullets appended if not already present
          - known_failure_modes (list) — new bullets appended if not already present
          - dos                 (list) — new bullets appended if not already present
          - donts               (list) — new bullets appended if not already present
          - clarifications      (list) — new Q/A style entries appended
          - lesson_summary      (str)  — one-line lesson appended under Lessons Learned

        Deduplication is case-insensitive on whitespace-trimmed text so repeated
        runs of the same task don't bloat the blueprint.
        """
        def _append_unique(existing: List[str], incoming) -> None:
            if not incoming:
                return
            seen = {s.strip().lower() for s in existing}
            for item in incoming:
                if not isinstance(item, str):
                    continue
                s = item.strip()
                if s and s.lower() not in seen:
                    existing.append(s)
                    seen.add(s.lower())

        self.version += 1

        if self.deterministic:
            topo = update.get("topology")
            if isinstance(topo, str) and topo.strip():
                self.topology = topo.strip()
        else:
            hints = update.get("discovery_hints")
            if isinstance(hints, str) and hints.strip():
                self.discovery_hints = hints.strip()

        _append_unique(self.preconditions, update.get("preconditions") or [])
        _append_unique(self.known_failure_modes, update.get("known_failure_modes") or [])
        _append_unique(self.dos, update.get("dos") or [])
        _append_unique(self.donts, update.get("donts") or [])
        _append_unique(self.clarifications, update.get("clarifications") or [])

        summary = update.get("lesson_summary")
        if isinstance(summary, str) and summary.strip():
            self.lessons_learned.append(f"[v{self.version}] {summary.strip()}")

    # ── prompt injection view ────────────────────────────────────────────────

    def to_prompt_block(self, max_chars: int = 4000, is_stale: bool = False) -> str:
        """Compact view for injection into the primary agent's system prompt.

        Behaviour by task type and staleness:
          - Fresh topology-locked: topology injected as hard constraints
          - Fresh adaptive       : discovery_hints injected as soft guidance
          - Stale (either type)  : re-discovery directive prepended; stored
                                   topology/hints labelled as reference-only
        """
        parts = [f"### Blueprint for task `{self.task_name}` (v{self.version})"]

        if is_stale:
            parts.append(
                "**STALE** — This task's blueprint has not been validated recently. "
                "Re-discover subtasks from scratch; compare against stored guidance below "
                "but do not treat it as authoritative. Update what has changed."
            )

        if self.deterministic:
            if self.topology:
                label = (
                    "Stored topology (reference only — re-discover):"
                    if is_stale else
                    "Topology (hard constraints — follow unless explicitly overridden):"
                )
                parts.append(f"{label}\n{self.topology}")
        else:
            if self.discovery_hints:
                label = (
                    "Prior discovery hints (may be outdated):"
                    if is_stale else
                    "Discovery hints (soft guidance):"
                )
                parts.append(f"{label}\n{self.discovery_hints}")

        if self.preconditions:
            parts.append(
                "Preconditions:\n" + "\n".join(f"  - {p}" for p in self.preconditions)
            )
        if self.known_failure_modes:
            parts.append(
                "Known failure modes:\n" + "\n".join(f"  - {f}" for f in self.known_failure_modes)
            )
        if self.dos:
            parts.append("Do:\n" + "\n".join(f"  - {d}" for d in self.dos))
        if self.donts:
            parts.append("Don't:\n" + "\n".join(f"  - {d}" for d in self.donts))
        if self.clarifications:
            parts.append(
                "Clarifications from prior runs:\n"
                + "\n".join(f"  - {c}" for c in self.clarifications)
            )
        if self.lessons_learned:
            parts.append(
                "Lessons learned:\n" + "\n".join(f"  - {l}" for l in self.lessons_learned)
            )

        block = "\n".join(parts)
        if len(block) > max_chars:
            block = block[: max_chars - 20] + "\n...[truncated]"
        return block


class BlueprintStore:
    """Load / save blueprints from filesystem or a StorageBackend.

    The store is backend-agnostic from the caller's perspective. Which mode
    is used is decided once at construction time from ``BlueprintConfig``.
    """

    _BACKEND_KEY_PREFIX = "blueprint:"

    def __init__(
        self,
        dir_path: str,
        storage_mode: str = "filesystem",
        storage_backend: Optional[StorageBackend] = None,
    ):
        self._dir = Path(dir_path)
        self._mode = storage_mode
        self._backend = storage_backend
        if storage_mode == "backend" and storage_backend is None:
            raise ValueError("BlueprintStore: storage_mode='backend' requires a StorageBackend")
        if storage_mode == "filesystem":
            self._dir.mkdir(parents=True, exist_ok=True)

    # ── naming ────────────────────────────────────────────────────────────────

    @staticmethod
    def generate_unique_name(task_name: str, salt: Optional[str] = None) -> str:
        """Deterministic unique blueprint name for a task.

        Produces ``{sanitized_task_name}__{8char_hash}`` so two task types with
        the same human name (different configs) never collide, while the same
        task in the same config always resolves to the same blueprint.
        """
        safe = _SAFE_NAME_RE.sub("_", task_name).strip("_") or "task"
        salt = salt or task_name
        short = hashlib.sha1(salt.encode("utf-8")).hexdigest()[:8]
        return f"{safe}__{short}"

    def _resolve_path(self, name: str) -> Path:
        """Resolve a blueprint reference to an absolute filesystem path."""
        ref = name if name.endswith(".md") else f"{name}.md"
        p = Path(ref)
        if p.is_absolute():
            return p
        return self._dir / ref

    def _backend_key(self, name: str) -> str:
        stripped = name[:-3] if name.endswith(".md") else name
        return f"{self._BACKEND_KEY_PREFIX}{stripped}"

    # ── load / save ───────────────────────────────────────────────────────────

    async def load(self, name: str) -> Optional[Blueprint]:
        """Load a blueprint by name (or path). Returns None if missing."""
        try:
            if self._mode == "filesystem":
                path = self._resolve_path(name)
                if not path.exists():
                    return None
                text = path.read_text(encoding="utf-8")
            else:
                raw = await self._backend.get(self._backend_key(name))
                if raw is None:
                    return None
                text = raw if isinstance(raw, str) else str(raw)
            return Blueprint.from_markdown(text)
        except Exception as e:
            logger.warning("Blueprint load failed for %s: %s", name, e)
            return None

    async def save(self, blueprint: Blueprint) -> str:
        """Persist the blueprint. Returns the reference (path or key)."""
        blueprint.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        text = blueprint.to_markdown()
        if self._mode == "filesystem":
            path = self._resolve_path(blueprint.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
            logger.debug("Blueprint saved: %s (v%d)", path, blueprint.version)
            return str(path)
        key = self._backend_key(blueprint.name)
        await self._backend.set(key, text)
        logger.debug("Blueprint saved to backend: %s (v%d)", key, blueprint.version)
        return key

    async def load_or_create(self, name: str, task_name: str, deterministic: bool = False) -> Blueprint:
        bp = await self.load(name)
        if bp is None:
            bp = Blueprint(name=name, task_name=task_name, version=1)
        bp.deterministic = deterministic
        return bp

    async def append_lesson(
        self,
        name: str,
        task_name: str,
        lesson: str,
        deterministic: bool = False,
    ) -> Blueprint:
        """Append a new lesson-learned entry and bump the version.

        Called by the auto-update path after a successful (or corrected) run
        when the agent discovers guidance worth persisting for next time.
        """
        bp = await self.load_or_create(name, task_name, deterministic=deterministic)
        bp.version += 1
        bp.lessons_learned.append(f"[v{bp.version}] {lesson}")
        await self.save(bp)
        return bp
