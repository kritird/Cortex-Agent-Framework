"""AntColony — lifecycle manager for self-spawned specialist Cortex agents.

An 'ant' is a Cortex agent running as an MCP server (trust_tier='ant').
The colony hatches ants on demand, persists their state to ants.yaml,
supervises their processes, and restarts crashed ants automatically.
"""
import asyncio
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import aiohttp
import yaml

logger = logging.getLogger(__name__)

_ANT_READY_SENTINEL = "__ANT_READY__"
_HEALTH_CHECK_RETRIES = 10
_HEALTH_CHECK_INTERVAL_S = 0.5
_DEFAULT_BASE_PORT = 8100
_PORT_SCAN_RANGE = 200  # scan up to base_port + 200


@dataclass
class AntInfo:
    name: str
    capability: str
    description: str
    port: int
    pid: int
    url: str
    status: str           # running | stopped | crashed
    cortex_yaml_path: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    restart_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AntInfo":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


_ANT_YAML_TEMPLATE = """\
agent:
  name: {agent_name}
  description: {description}

llm_access:
  default:
    provider: {llm_provider}
    model: {llm_model}
    api_key_env_var: {api_key_env_var}

task_types:
  - name: {task_name}
    description: {description}
    capability_hint: {capability}
    output_format: text

storage:
  base_path: {storage_path}
"""


class AntColony:
    """
    Manages the lifecycle of ant agents — self-spawned specialist Cortex
    agents that fill capability gaps identified by the CapabilityScout.

    Responsibilities:
    - Allocate ports and directories for new ants.
    - Generate cortex.yaml for each ant from an LLM-refined template.
    - Spawn ant processes using the current Python interpreter.
    - Health-check and register ants with the ToolServerRegistry.
    - Supervise running ants and restart crashed ones.
    - Persist colony state to ants.yaml.
    - Expose CLI-friendly status/stop methods.
    """

    def __init__(
        self,
        base_path: str,
        base_port: int = _DEFAULT_BASE_PORT,
        max_ants: int = 20,
        auto_restart: bool = True,
        llm_provider: str = "default",
        llm_model: str = "claude-haiku-4-5-20251001",
        api_key_env_var: str = "ANTHROPIC_API_KEY",
    ):
        self._base_path = Path(base_path) / "ants"
        self._ants_yaml = Path(base_path) / "ants.yaml"
        self._base_port = base_port
        self._max_ants = max_ants
        self._auto_restart = auto_restart
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._api_key_env_var = api_key_env_var

        self._ants: Dict[str, AntInfo] = {}
        self._procs: Dict[str, asyncio.subprocess.Process] = {}
        self._supervisor_task: Optional[asyncio.Task] = None
        self._register_callback: Optional[Callable] = None
        self._deregister_callback: Optional[Callable] = None

        self._base_path.mkdir(parents=True, exist_ok=True)
        self._load()

    # ── public API ─────────────────────────────────────────────────────────────

    def set_register_callback(self, cb: Callable) -> None:
        """Called with (name, url) when an ant is ready to register."""
        self._register_callback = cb

    def set_deregister_callback(self, cb: Callable) -> None:
        """Called with (name,) when an ant is stopped/crashed."""
        self._deregister_callback = cb

    async def hatch(
        self,
        name: str,
        capability: str,
        description: str,
        llm_client=None,
    ) -> AntInfo:
        """
        Hatch a new ant for the given capability.

        1. Allocate a port.
        2. Generate cortex.yaml (optionally LLM-refined).
        3. Spawn the ant process.
        4. Health-check and register.
        5. Persist to ants.yaml.
        """
        if name in self._ants and self._ants[name].status == "running":
            logger.info("AntColony: ant '%s' already running", name)
            return self._ants[name]

        if len([a for a in self._ants.values() if a.status == "running"]) >= self._max_ants:
            raise RuntimeError(f"AntColony: max_ants ({self._max_ants}) reached, cannot hatch '{name}'")

        port = await self._allocate_port()
        ant_dir = self._base_path / name
        ant_dir.mkdir(parents=True, exist_ok=True)

        # Generate cortex.yaml for the ant
        cortex_yaml_path = await self._generate_ant_yaml(
            ant_dir=ant_dir,
            name=name,
            capability=capability,
            description=description,
            llm_client=llm_client,
        )

        # Spawn process
        pid, proc = await self._spawn(name=name, cortex_yaml_path=cortex_yaml_path, port=port)

        url = f"http://127.0.0.1:{port}"
        info = AntInfo(
            name=name,
            capability=capability,
            description=description,
            port=port,
            pid=pid,
            url=url,
            status="running",
            cortex_yaml_path=cortex_yaml_path,
        )
        self._ants[name] = info
        self._procs[name] = proc
        self._save()

        if self._register_callback:
            await self._register_callback(name, url)

        if self._auto_restart and self._supervisor_task is None:
            self._supervisor_task = asyncio.create_task(self._supervise())

        logger.info("AntColony: hatched ant '%s' on port %d (pid=%d)", name, port, pid)
        return info

    async def stop(self, name: str) -> None:
        """Stop a running ant by name."""
        info = self._ants.get(name)
        if not info:
            raise KeyError(f"AntColony: no ant named '{name}'")

        proc = self._procs.get(name)
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
            except ProcessLookupError:
                pass

        info.status = "stopped"
        self._procs.pop(name, None)
        self._save()

        if self._deregister_callback:
            await self._deregister_callback(name)

        logger.info("AntColony: stopped ant '%s'", name)

    async def stop_all(self) -> None:
        """Stop all running ants."""
        names = [n for n, a in self._ants.items() if a.status == "running"]
        for name in names:
            try:
                await self.stop(name)
            except Exception as exc:
                logger.warning("AntColony: error stopping ant '%s': %s", name, exc)

        if self._supervisor_task:
            self._supervisor_task.cancel()
            self._supervisor_task = None

    async def resume_colony(self, registry=None) -> None:
        """Re-hatch ants that were running before the last shutdown.

        Called during framework initialization. Ants loaded from ants.yaml
        that were previously running are marked 'crashed' on load; this method
        restarts them and re-registers them with the ToolServerRegistry.
        """
        crashed = [a for a in self._ants.values() if a.status == "crashed"]
        if not crashed:
            return
        logger.info("AntColony: resuming %d ant(s) from previous run", len(crashed))
        for info in crashed:
            try:
                pid, proc = await self._spawn(
                    name=info.name,
                    cortex_yaml_path=info.cortex_yaml_path,
                    port=info.port,
                )
                info.pid = pid
                info.status = "running"
                info.restart_count += 1
                self._procs[info.name] = proc
                self._save()
                if registry is not None:
                    await registry.register_ant_server(
                        name=info.name, url=info.url, capability=info.capability
                    )
                logger.info("AntColony: resumed ant '%s' on port %d", info.name, info.port)
            except Exception as exc:
                logger.warning("AntColony: could not resume ant '%s': %s", info.name, exc)

        if self._auto_restart and self._supervisor_task is None:
            self._supervisor_task = asyncio.create_task(self._supervise())

    def list_ants(self) -> List[AntInfo]:
        """Return all known ants (any status)."""
        return list(self._ants.values())

    def get_ant(self, name: str) -> Optional[AntInfo]:
        return self._ants.get(name)

    # ── port allocation ────────────────────────────────────────────────────────

    async def _allocate_port(self) -> int:
        used = {a.port for a in self._ants.values()}
        for candidate in range(self._base_port, self._base_port + _PORT_SCAN_RANGE):
            if candidate in used:
                continue
            if await self._port_free(candidate):
                return candidate
        raise RuntimeError("AntColony: no free port found in range")

    @staticmethod
    async def _port_free(port: int) -> bool:
        import socket
        loop = asyncio.get_event_loop()
        def _check():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("127.0.0.1", port))
                    return True
                except OSError:
                    return False
        return await loop.run_in_executor(None, _check)

    # ── ant yaml generation ────────────────────────────────────────────────────

    async def _generate_ant_yaml(
        self,
        ant_dir: Path,
        name: str,
        capability: str,
        description: str,
        llm_client=None,
    ) -> str:
        yaml_path = ant_dir / "cortex.yaml"
        storage_path = str(ant_dir / "storage")

        # Use LLM to refine the task description if available
        task_description = description
        if llm_client:
            try:
                prompt = (
                    f"You are generating a cortex.yaml task description for a specialist AI agent.\n"
                    f"Capability: {capability}\n"
                    f"Description: {description}\n\n"
                    f"Write a concise one-sentence task description (max 120 chars) for a task_type "
                    f"named '{name}' that fills this capability. Return ONLY the description string."
                )
                tokens = []
                async for token in llm_client.stream(
                    messages=[{"role": "user", "content": prompt}],
                    system="You are a concise technical writer. Return only the requested string.",
                    provider_name="default",
                ):
                    tokens.append(token)
                refined = "".join(tokens).strip().strip('"').strip("'")
                if refined and len(refined) < 200:
                    task_description = refined
            except Exception as exc:
                logger.debug("AntColony: LLM description refinement failed: %s", exc)

        content = _ANT_YAML_TEMPLATE.format(
            agent_name=name.replace("_", " ").title(),
            description=task_description,
            llm_provider=self._llm_provider,
            llm_model=self._llm_model,
            api_key_env_var=self._api_key_env_var,
            task_name=name,
            capability=capability,
            storage_path=storage_path,
        )
        with open(yaml_path, "w") as f:
            f.write(content)

        return str(yaml_path)

    # ── process spawning ───────────────────────────────────────────────────────

    async def _spawn(
        self, name: str, cortex_yaml_path: str, port: int
    ) -> tuple:
        """Spawn the ant MCP server process. Returns (pid, proc)."""
        from cortex.ants.ant_server import generate_bootstrap

        # Locate the framework root so the subprocess can import cortex
        framework_path = str(Path(__file__).resolve().parents[2])

        bootstrap = generate_bootstrap(
            framework_path=framework_path,
            cortex_yaml_path=cortex_yaml_path,
            name=name,
            port=port,
        )

        bootstrap_path = str(self._base_path / name / "_ant_bootstrap.py")
        with open(bootstrap_path, "w") as f:
            f.write(bootstrap)

        env = {**os.environ}  # inherit API keys from parent

        proc = await asyncio.create_subprocess_exec(
            sys.executable, bootstrap_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Wait for the ready sentinel or process death
        ready = await self._wait_for_ready(proc, port)
        if not ready:
            proc.kill()
            stderr = b""
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
            except asyncio.TimeoutError:
                pass
            raise RuntimeError(
                f"AntColony: ant '{name}' failed to start on port {port}. "
                f"stderr: {stderr.decode(errors='replace')[:500]}"
            )

        return proc.pid, proc

    async def _wait_for_ready(self, proc: asyncio.subprocess.Process, port: int, timeout: float = 30.0) -> bool:
        """Wait until the ant prints __ANT_READY__ or health check passes."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if proc.returncode is not None:
                return False  # process died
            # Check health endpoint
            if await self._health_check(f"http://127.0.0.1:{port}"):
                return True
            await asyncio.sleep(_HEALTH_CHECK_INTERVAL_S)
        return False

    @staticmethod
    async def _health_check(url: str) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{url}/health", timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    return resp.status == 200
        except Exception:
            return False

    # ── supervisor ─────────────────────────────────────────────────────────────

    async def _supervise(self) -> None:
        """Background loop: detect crashed ants and restart them."""
        while True:
            await asyncio.sleep(5)
            for name, info in list(self._ants.items()):
                if info.status != "running":
                    continue
                proc = self._procs.get(name)
                crashed = (proc is None) or (proc.returncode is not None)
                if not crashed:
                    # Verify health
                    if not await self._health_check(info.url):
                        crashed = True

                if crashed:
                    info.status = "crashed"
                    self._save()
                    logger.warning("AntColony: ant '%s' crashed, restarting...", name)
                    if self._deregister_callback:
                        try:
                            await self._deregister_callback(name)
                        except Exception:
                            pass
                    try:
                        pid, proc = await self._spawn(
                            name=name,
                            cortex_yaml_path=info.cortex_yaml_path,
                            port=info.port,
                        )
                        info.pid = pid
                        info.status = "running"
                        info.restart_count += 1
                        self._procs[name] = proc
                        self._save()
                        logger.info("AntColony: ant '%s' restarted (attempt %d)", name, info.restart_count)
                        if self._register_callback:
                            await self._register_callback(name, info.url)
                    except Exception as exc:
                        logger.error("AntColony: failed to restart ant '%s': %s", name, exc)

    # ── persistence ────────────────────────────────────────────────────────────

    def _save(self) -> None:
        data = {"ants": [a.to_dict() for a in self._ants.values()]}
        with open(self._ants_yaml, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def _load(self) -> None:
        if not self._ants_yaml.exists():
            return
        try:
            with open(self._ants_yaml) as f:
                data = yaml.safe_load(f) or {}
            for d in data.get("ants", []):
                info = AntInfo.from_dict(d)
                # Mark as crashed on load — supervisor will restart if auto_restart
                if info.status == "running":
                    info.status = "crashed"
                self._ants[info.name] = info
            logger.info("AntColony: loaded %d ant(s) from ants.yaml", len(self._ants))
        except Exception as exc:
            logger.warning("AntColony: could not load ants.yaml: %s", exc)
