"""CortexTestFramework — test harness for agent development."""
import asyncio
import tempfile
from pathlib import Path
from typing import Any, List, Optional
import yaml

from cortex.framework import CortexFramework, SessionResult
from cortex.streaming.status_events import StatusEvent


class CortexTestFramework:
    """
    Test harness that wraps CortexFramework with test-friendly utilities.
    Creates a temporary storage directory and minimal config for testing.

    Usage:
        async with CortexTestFramework.from_config_dict({...}) as harness:
            result = await harness.run("user_1", "my request")
            assert result.response is not None
    """

    def __init__(self, framework: CortexFramework, tmpdir: str):
        self._framework = framework
        self._tmpdir = tmpdir
        self._events: List[Any] = []

    @classmethod
    def minimal_config(cls, base_path: str, **overrides) -> dict:
        """Generate a minimal valid cortex.yaml dict for testing."""
        config = {
            "agent": {
                "name": "TestAgent",
                "description": "Test agent for unit testing",
            },
            "llm_access": {
                "default": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5-20251001",
                    "api_key_env_var": "ANTHROPIC_API_KEY",
                    "max_tokens": 1024,
                },
            },
            "storage": {
                "base_path": base_path,
            },
            "task_types": overrides.pop("task_types", []),
            "tool_servers": overrides.pop("tool_servers", {}),
        }
        config.update(overrides)
        return config

    @classmethod
    async def from_config_dict(cls, config_dict: dict) -> "CortexTestFramework":
        """Create a test harness from a config dict (writes to temp file)."""
        tmpdir = tempfile.mkdtemp(prefix="cortex_test_")
        config_dict.setdefault("storage", {})["base_path"] = tmpdir
        config_path = str(Path(tmpdir) / "cortex.yaml")
        with open(config_path, "w") as f:
            yaml.dump(config_dict, f)
        framework = CortexFramework(config_path)
        await framework.initialize()
        return cls(framework, tmpdir)

    async def run(
        self,
        user_id: str,
        request: str,
        file_refs: Optional[List[str]] = None,
        user_consent: str = "none",
    ) -> SessionResult:
        """Run a session and collect all events."""
        q: asyncio.Queue = asyncio.Queue()
        self._events = []

        async def collect_events():
            while True:
                event = await q.get()
                if event is None:
                    break
                self._events.append(event)

        collector = asyncio.create_task(collect_events())
        result = await self._framework.run_session(
            user_id=user_id,
            request=request,
            event_queue=q,
            file_refs=file_refs,
            user_consent=user_consent,
        )
        await collector
        return result

    def get_events(self) -> List[Any]:
        """Return all events emitted during the last run."""
        return list(self._events)

    def get_status_messages(self) -> List[str]:
        """Return all status messages from last run."""
        return [
            e.message for e in self._events
            if isinstance(e, StatusEvent)
        ]

    async def cleanup(self) -> None:
        """Shut down framework and remove temp directory."""
        await self._framework.shutdown()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.cleanup()
