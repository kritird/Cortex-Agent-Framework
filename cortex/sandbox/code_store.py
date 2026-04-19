"""AgentCodeStore — persists LLM-generated scripts at agent scope (not session scope)."""
import hashlib
import logging
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# Where agent-level scripts are stored, relative to storage.base_path
STORE_DIR = "agent_tools"
INDEX_FILE = "index.yaml"


@dataclass
class ScriptRecord:
    """Metadata for a persisted agent script."""
    task_name: str          # cortex.yaml task type name
    script_path: str        # absolute path to the .py file
    description: str        # what this script does
    created_at: str         # ISO 8601
    last_used_at: str       # ISO 8601
    use_count: int = 0
    requirements: list[str] = field(default_factory=list)  # pip packages needed
    added_to_yaml: bool = False  # True once written into cortex.yaml as handler


class AgentCodeStore:
    """
    Stores LLM-generated Python scripts at agent scope.
    Scripts outlive sessions — they belong to the agent, not a user.

    Storage layout:
        {base_path}/agent_tools/
            index.yaml                  ← manifest of all scripts
            {task_name}_{hash}.py       ← the generated script
    """

    def __init__(self, base_path: str):
        self._store_dir = Path(base_path) / STORE_DIR
        self._index_path = self._store_dir / INDEX_FILE
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, ScriptRecord] = {}
        self._load_index()

    def _load_index(self) -> None:
        if not self._index_path.exists():
            return
        try:
            with open(self._index_path, "r") as f:
                raw = yaml.safe_load(f) or {}
            for task_name, data in raw.items():
                self._index[task_name] = ScriptRecord(**data)
        except Exception as e:
            logger.warning("Failed to load code store index: %s", e)

    def _save_index(self) -> None:
        data = {name: asdict(rec) for name, rec in self._index.items()}
        with open(self._index_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def has_script(self, task_name: str) -> bool:
        """Return True if a persisted script exists for this task type."""
        rec = self._index.get(task_name)
        if rec is None:
            return False
        # Verify the file still exists on disk
        return Path(rec.script_path).exists()

    def get_script(self, task_name: str) -> Optional[tuple[str, ScriptRecord]]:
        """
        Return (script_source_code, record) if found, else None.
        Updates last_used_at and use_count.
        """
        rec = self._index.get(task_name)
        if rec is None:
            return None
        script_path = Path(rec.script_path)
        if not script_path.exists():
            logger.warning("Script file missing for task '%s': %s", task_name, script_path)
            del self._index[task_name]
            self._save_index()
            return None
        source = script_path.read_text(encoding="utf-8")
        rec.last_used_at = datetime.now(timezone.utc).isoformat()
        rec.use_count += 1
        self._save_index()
        return source, rec

    def persist(
        self,
        task_name: str,
        source_code: str,
        description: str,
        requirements: list[str] = None,
    ) -> ScriptRecord:
        """
        Save a generated script to the agent tool store.
        Overwrites any existing script for this task_name.
        Returns the ScriptRecord.
        """
        # Generate a stable filename
        code_hash = hashlib.sha1(source_code.encode()).hexdigest()[:8]
        filename = f"{task_name}_{code_hash}.py"
        script_path = self._store_dir / filename

        # Remove old script file if task_name already had one
        existing = self._index.get(task_name)
        if existing:
            old_path = Path(existing.script_path)
            if old_path.exists() and old_path != script_path:
                old_path.unlink(missing_ok=True)

        script_path.write_text(source_code, encoding="utf-8")

        now = datetime.now(timezone.utc).isoformat()
        record = ScriptRecord(
            task_name=task_name,
            script_path=str(script_path),
            description=description,
            created_at=now,
            last_used_at=now,
            use_count=0,
            requirements=requirements or [],
            added_to_yaml=False,
        )
        self._index[task_name] = record
        self._save_index()
        logger.info("Persisted script for task '%s' at %s", task_name, script_path)
        return record

    def mark_added_to_yaml(self, task_name: str) -> None:
        """Mark that this script has been wired into cortex.yaml as a handler."""
        rec = self._index.get(task_name)
        if rec:
            rec.added_to_yaml = True
            self._save_index()

    def add_to_cortex_yaml(
        self,
        task_name: str,
        cortex_yaml_path: str,
    ) -> bool:
        """
        Inject the script as a scripted handler into cortex.yaml.
        Sets complexity: scripted and handler: <dotted.path>.
        Returns True if the YAML was modified.
        """
        rec = self._index.get(task_name)
        if not rec:
            logger.warning("No script record for task '%s'", task_name)
            return False

        cortex_path = Path(cortex_yaml_path)
        if not cortex_path.exists():
            logger.warning("cortex.yaml not found at %s", cortex_yaml_path)
            return False

        with open(cortex_path, "r") as f:
            config = yaml.safe_load(f) or {}

        task_types = config.get("task_types", [])
        modified = False
        for task in task_types:
            if task.get("name") == task_name:
                # Derive a dotted import path for the script
                # e.g. agent_tools.web_scraper_a1b2c3d4
                script_stem = Path(rec.script_path).stem
                handler_path = f"agent_tools.{script_stem}.run"
                task["complexity"] = "scripted"
                task["handler"] = handler_path
                modified = True
                break

        if modified:
            # Backup
            backup_path = str(cortex_path) + ".bak.code_persist"
            shutil.copy2(cortex_path, backup_path)
            with open(cortex_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            self.mark_added_to_yaml(task_name)
            logger.info(
                "Updated cortex.yaml: task '%s' is now a scripted handler",
                task_name,
            )

        return modified

    def _extract_requirements_from_source(self, source_code: str) -> list[str]:
        """
        Parse import statements from Python source and return likely third-party package names.
        Filters out the stdlib top-level modules so only pip-installable packages are returned.
        """
        import ast
        import sys

        # Stdlib top-level module names (Python 3.8+)
        stdlib_modules = sys.stdlib_module_names if hasattr(sys, "stdlib_module_names") else {
            "abc", "ast", "asyncio", "base64", "builtins", "collections", "contextlib",
            "copy", "csv", "dataclasses", "datetime", "email", "enum", "functools",
            "gc", "glob", "hashlib", "hmac", "html", "http", "importlib", "inspect",
            "io", "itertools", "json", "logging", "math", "multiprocessing", "operator",
            "os", "pathlib", "pickle", "platform", "pprint", "queue", "random", "re",
            "shutil", "signal", "socket", "sqlite3", "ssl", "stat", "string", "struct",
            "subprocess", "sys", "tarfile", "tempfile", "textwrap", "threading", "time",
            "traceback", "typing", "unicodedata", "unittest", "urllib", "uuid", "warnings",
            "weakref", "xml", "xmlrpc", "zipfile", "zlib",
        }

        try:
            tree = ast.parse(source_code)
        except SyntaxError:
            return []

        packages: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in stdlib_modules and top not in packages:
                        packages.append(top)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    if top not in stdlib_modules and top not in packages:
                        packages.append(top)

        return packages

    def list_scripts(self) -> list[ScriptRecord]:
        """Return all persisted script records."""
        return list(self._index.values())

    def delete_script(self, task_name: str) -> bool:
        """Remove a script from the store. Returns True if deleted."""
        rec = self._index.pop(task_name, None)
        if rec:
            Path(rec.script_path).unlink(missing_ok=True)
            self._save_index()
            return True
        return False
