"""CodeSandbox — isolated subprocess environment for executing LLM-generated Python code."""
import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cortex.exceptions import CortexSecurityError
from cortex.sandbox.result_validator import ResultValidator

logger = logging.getLogger(__name__)

# Packages that are never allowed to be installed in the sandbox
BLOCKED_PACKAGES = {
    "subprocess", "os", "sys",          # these are stdlib — can't block import but we
    "paramiko", "fabric",               # SSH clients
    "scapy",                            # network scanning
    "pyautogui",                        # GUI automation
    "pynput",                           # input capture
    "keylogger",                        # obvious
    "mitmproxy",                        # MITM
}

# Blocked import statements in generated code
BLOCKED_IMPORTS_PATTERN = re.compile(
    r"""^\s*(?:import|from)\s+
    (?:subprocess|pty|pty|ctypes|cffi|
       socket(?:server)?|ftplib|telnetlib|
       multiprocessing\.managers|
       importlib\.util\.spec_from_file_location)
    """,
    re.VERBOSE | re.MULTILINE,
)

# The generated script must define a run() function
REQUIRED_ENTRYPOINT = "def run("

# Wrapper that restricts what the script can do at runtime
_SANDBOX_WRAPPER = '''\
import sys
import os

# Block dangerous stdlib access
import builtins
_real_open = builtins.open

def _safe_open(file, mode="r", *args, **kwargs):
    file_str = str(file)
    # Only allow writes inside OUTPUT_DIR
    if any(m in mode for m in ("w", "a", "x")):
        output_dir = os.environ.get("CORTEX_OUTPUT_DIR", "")
        if output_dir and not file_str.startswith(output_dir):
            raise PermissionError(
                f"Sandbox: write access denied outside output directory: {{file_str!r}}"
            )
    return _real_open(file, mode, *args, **kwargs)

builtins.open = _safe_open

# Prevent spawning subprocesses
import subprocess as _subprocess
def _blocked(*a, **kw):
    raise PermissionError("Sandbox: subprocess execution is not allowed.")
_subprocess.run = _blocked
_subprocess.Popen = _blocked
_subprocess.call = _blocked
_subprocess.check_output = _blocked

# Restrict os.system / os.popen
os.system = lambda *a, **kw: (_ for _ in ()).throw(PermissionError("Sandbox: os.system is not allowed."))
os.popen = lambda *a, **kw: (_ for _ in ()).throw(PermissionError("Sandbox: os.popen is not allowed."))

# Inject the user script
{user_code}

# --- entrypoint ---
import json, traceback

input_json = os.environ.get("CORTEX_TASK_INPUT", "{{}}")
try:
    task_input = json.loads(input_json)
except Exception:
    task_input = {{}}

try:
    result = run(task_input)
    if result is None:
        result = ""
    print("__CORTEX_RESULT_START__")
    print(str(result))
    print("__CORTEX_RESULT_END__")
except Exception as e:
    print("__CORTEX_ERROR_START__")
    traceback.print_exc()
    print("__CORTEX_ERROR_END__")
    sys.exit(1)
'''

# Prompt template for asking the LLM to generate code
CODE_GEN_PROMPT = """\
You are writing a Python script to accomplish a task.

TASK NAME: {task_name}
TASK DESCRIPTION: {description}
INSTRUCTION: {instruction}
OUTPUT FORMAT: {output_format}

Write a Python script with exactly ONE function:

    def run(input: dict) -> str:
        ...

Rules:
- The function receives a dict called `input` with any context data provided.
- The function must return a string (the result).
- If output_format is "md", return Markdown. If "json", return valid JSON string.
- You MAY import any standard library module EXCEPT: subprocess, socket, ctypes, pty.
- You MAY use third-party packages — list them in a comment at the top:
    # REQUIREMENTS: pandas, requests
- Do NOT use subprocess, os.system, or any shell execution.
- Do NOT write files unless absolutely necessary. If you do write files,
  use only the path provided in input.get("output_dir").
- Keep the function self-contained and deterministic where possible.

Return ONLY the Python code, no explanation, no markdown fences.
"""


@dataclass
class SandboxResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    output_files: list[str] = field(default_factory=list)
    error: Optional[str] = None
    requirements_installed: list[str] = field(default_factory=list)


class CodeSandbox:
    """
    Executes LLM-generated Python code in an isolated subprocess environment.

    Isolation layers:
    1. Separate process — framework process is never at risk
    2. Dedicated venv — packages installed here don't affect the framework
    3. Runtime monkey-patching — open(), subprocess, os.system blocked/restricted
    4. Output directory jail — file writes restricted to session output dir
    5. ResultValidator — output files checked for type/extension before returning
    6. Timeout — hard kill after configured seconds

    The sandbox venv is created once per agent (in agent_tools/sandbox_venv/)
    and reused across sessions to avoid reinstalling packages.
    """

    def __init__(
        self,
        base_path: str,
        timeout_seconds: int = 60,
        allow_network: bool = False,
    ):
        self._base_path = Path(base_path)
        self._timeout = timeout_seconds
        self._allow_network = allow_network
        self._venv_dir = self._base_path / "agent_tools" / "sandbox_venv"
        self._validator = ResultValidator()
        self._python_bin: Optional[str] = None

    async def ensure_venv(self) -> str:
        """Create the sandbox venv if it doesn't exist. Returns python binary path."""
        if self._python_bin and Path(self._python_bin).exists():
            return self._python_bin

        venv_python = self._venv_dir / "bin" / "python"
        if sys.platform == "win32":
            venv_python = self._venv_dir / "Scripts" / "python.exe"

        if not venv_python.exists():
            logger.info("Creating sandbox venv at %s", self._venv_dir)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "venv", str(self._venv_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"Failed to create sandbox venv at {self._venv_dir}")

        self._python_bin = str(venv_python)
        return self._python_bin

    async def install_requirements(self, requirements: list[str]) -> list[str]:
        """
        Install packages into the sandbox venv.
        Skips any packages in BLOCKED_PACKAGES.
        Returns list of actually installed packages.
        """
        python = await self.ensure_venv()
        pip = str(Path(python).parent / "pip")

        allowed = [r for r in requirements if r.lower().split("[")[0] not in BLOCKED_PACKAGES]
        if not allowed:
            return []

        logger.info("Sandbox: installing %s", allowed)
        proc = await asyncio.create_subprocess_exec(
            pip, "install", "--quiet", *allowed,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("pip install timed out after 120s")

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")
            raise RuntimeError(f"pip install failed: {err[:500]}")

        return allowed

    def _extract_requirements(self, source_code: str) -> list[str]:
        """Parse # REQUIREMENTS: pandas, requests from the script header."""
        for line in source_code.splitlines():
            line = line.strip()
            if line.startswith("# REQUIREMENTS:"):
                raw = line[len("# REQUIREMENTS:"):].strip()
                return [r.strip() for r in raw.split(",") if r.strip()]
            if line and not line.startswith("#"):
                break  # Stop at first non-comment line
        return []

    def _validate_source(self, source_code: str) -> None:
        """Static checks on generated code before execution."""
        if REQUIRED_ENTRYPOINT not in source_code:
            raise CortexSecurityError(
                "Generated code must define a run(input) function."
            )
        if BLOCKED_IMPORTS_PATTERN.search(source_code):
            raise CortexSecurityError(
                "Generated code contains a blocked import statement."
            )

    async def execute(
        self,
        source_code: str,
        task_input: dict,
        session_id: str,
        output_dir: str,
    ) -> SandboxResult:
        """
        Execute source_code in the sandbox.
        task_input is passed to run() as the `input` dict.
        output_dir is the only directory the script may write to.
        """
        self._validate_source(source_code)
        python = await self.ensure_venv()

        # Install any required packages
        requirements = self._extract_requirements(source_code)
        installed = []
        if requirements:
            try:
                installed = await self.install_requirements(requirements)
            except Exception as e:
                return SandboxResult(error=f"Package installation failed: {e}", exit_code=1)

        # Write wrapped script to a temp file
        wrapped = _SANDBOX_WRAPPER.format(user_code=source_code)
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(wrapped)
            tmp_path = tmp.name

        import json as _json
        env = {
            "PATH": os.environ.get("PATH", ""),
            "CORTEX_TASK_INPUT": _json.dumps(task_input),
            "CORTEX_OUTPUT_DIR": str(Path(output_dir).resolve()),
            "PYTHONPATH": "",      # Isolate from framework packages
            "HOME": str(self._base_path),
        }
        if not self._allow_network:
            # On Linux we could use network namespaces; here we at least
            # remove proxy/credential env vars to discourage network use
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env.pop(key, None)

        try:
            proc = await asyncio.create_subprocess_exec(
                python, tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(output_dir),
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                return SandboxResult(
                    error=f"Execution timed out after {self._timeout}s",
                    exit_code=124,
                )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        # Extract result from sentinel markers
        result_text = ""
        error_text = ""
        if "__CORTEX_RESULT_START__" in stdout:
            start = stdout.index("__CORTEX_RESULT_START__") + len("__CORTEX_RESULT_START__\n")
            end = stdout.index("__CORTEX_RESULT_END__")
            result_text = stdout[start:end].strip()
        if "__CORTEX_ERROR_START__" in stdout:
            start = stdout.index("__CORTEX_ERROR_START__") + len("__CORTEX_ERROR_START__\n")
            end = stdout.index("__CORTEX_ERROR_END__")
            error_text = stdout[start:end].strip()

        # Validate text output
        result_text = self._validator.validate_text(result_text or error_text or stderr[:2000])

        # Collect and validate any output files written by the script
        output_files = []
        for file_path, ext in self._validator.collect_output_files(output_dir):
            output_files.append(file_path)

        return SandboxResult(
            stdout=result_text,
            stderr=stderr[:2000] if proc.returncode != 0 else "",
            exit_code=proc.returncode,
            output_files=output_files,
            error=error_text if proc.returncode != 0 else None,
            requirements_installed=installed,
        )

    async def generate_and_execute(
        self,
        task_name: str,
        description: str,
        instruction: str,
        output_format: str,
        task_input: dict,
        session_id: str,
        output_dir: str,
        llm_client,
    ) -> tuple[str, SandboxResult]:
        """
        Ask the LLM to generate code, then execute it.
        Returns (generated_source_code, sandbox_result).
        """
        prompt = CODE_GEN_PROMPT.format(
            task_name=task_name,
            description=description,
            instruction=instruction,
            output_format=output_format,
        )

        tokens = []
        async for token in llm_client.stream(
            messages=[{"role": "user", "content": prompt}],
            system=(
                "You are an expert Python developer. Write clean, focused code. "
                "Return ONLY raw Python code — no markdown fences, no explanation."
            ),
            provider_name="default",
        ):
            tokens.append(token)

        source_code = "".join(tokens).strip()

        # Strip accidental markdown fences
        if source_code.startswith("```"):
            lines = source_code.splitlines()
            source_code = "\n".join(
                l for l in lines if not l.strip().startswith("```")
            )

        result = await self.execute(
            source_code=source_code,
            task_input=task_input,
            session_id=session_id,
            output_dir=output_dir,
        )
        return source_code, result
