"""CodeSandbox — isolated subprocess environment for executing LLM-generated code.

Supports polyglot execution via a # LANGUAGE: <lang> header comment:
  python (default), node, shell, ruby, go, rust

Python scripts run inside a dedicated venv with a lightweight runtime wrapper
that restricts file writes to the output directory. Subprocess execution is
allowed so the agent can spawn child programs it writes to the output dir.

All other languages are written to the output dir as source files and executed
via the appropriate interpreter. No venv is used for non-Python languages.
"""
import asyncio
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cortex.exceptions import CortexSecurityError
from cortex.prompts import CODE_GEN_SYSTEM, CODE_GEN_USER
from cortex.sandbox.result_validator import ResultValidator

logger = logging.getLogger(__name__)

# Packages that are never allowed to be installed in the sandbox
BLOCKED_PACKAGES = {
    "paramiko", "fabric",   # SSH clients
    "scapy",                # network scanning
    "keylogger",            # obvious
    "mitmproxy",            # MITM
    "pynput",               # raw input capture
}

# Blocked import statements (reduced — subprocess now permitted)
BLOCKED_IMPORTS_PATTERN = re.compile(
    r"""^\s*(?:import|from)\s+
    (?:pty|ctypes|cffi|
       ftplib|telnetlib|
       multiprocessing\.managers|
       importlib\.util\.spec_from_file_location)
    """,
    re.VERBOSE | re.MULTILINE,
)

# The generated Python script must define a run() function
REQUIRED_ENTRYPOINT = "def run("

# Language → (file extension, interpreter argv prefix)
# Rust, Java, and Kotlin are compiled languages handled by _execute_polyglot.
_LANG_RUNNERS: dict[str, tuple[str, list[str]]] = {
    "node":       (".js",  ["node"]),
    "javascript": (".js",  ["node"]),
    "typescript": (".ts",  ["npx", "--yes", "ts-node", "--transpile-only"]),
    "ts":         (".ts",  ["npx", "--yes", "ts-node", "--transpile-only"]),
    "deno":       (".ts",  ["deno", "run", "--allow-all"]),
    "shell":      (".sh",  ["bash"]),
    "bash":       (".sh",  ["bash"]),
    "ruby":       (".rb",  ["ruby"]),
    "go":         (".go",  ["go", "run"]),
}

# Languages that require a compile step before running. The execute_polyglot
# function calls the compiler then runs the produced binary.
_LANG_COMPILED: dict[str, dict] = {
    "rust": {
        "ext": ".rs",
        "compile": ["rustc", "{src}", "-o", "{bin}"],
        "run":     ["{bin}"],
        "bin_name": "cortex_rust_bin",
    },
    "java": {
        # Java 11+ supports single-file source execution: `java MyFile.java`
        "ext": ".java",
        "compile": None,  # no separate compile step
        "run":     ["java", "{src}"],
        "bin_name": None,
    },
    "kotlin": {
        # Kotlin scripts: `kotlinc -script file.kts`
        "ext": ".kts",
        "compile": None,
        "run":     ["kotlinc", "-script", "{src}"],
        "bin_name": None,
    },
    "c": {
        "ext": ".c",
        "compile": ["cc", "{src}", "-o", "{bin}"],
        "run":     ["{bin}"],
        "bin_name": "cortex_c_bin",
    },
}

# Per-language package install hooks. The header in user code declares deps:
#   # NPM_PACKAGES: express, lodash
#   # GEM_PACKAGES: httparty
#   # GO_PACKAGES: github.com/gorilla/mux
# _install_polyglot_packages() runs the appropriate install command.
_LANG_PACKAGE_HEADERS: dict[str, tuple[str, list[str]]] = {
    "node":       ("# NPM_PACKAGES:", ["npm", "install", "--prefix", "{out}", "--silent"]),
    "javascript": ("# NPM_PACKAGES:", ["npm", "install", "--prefix", "{out}", "--silent"]),
    "typescript": ("# NPM_PACKAGES:", ["npm", "install", "--prefix", "{out}", "--silent"]),
    "ts":         ("# NPM_PACKAGES:", ["npm", "install", "--prefix", "{out}", "--silent"]),
    "ruby":       ("# GEM_PACKAGES:", ["gem", "install", "--no-document"]),
    "go":         ("# GO_PACKAGES:", ["go", "get"]),
}

# Python sandbox wrapper: restricts file writes to OUTPUT_DIR.
# Subprocess is allowed so the agent can run programs it generates.
_SANDBOX_WRAPPER = '''\
import sys
import os

# Block writes outside the output directory
import builtins
_real_open = builtins.open
_OUTPUT_DIR = os.environ.get("CORTEX_OUTPUT_DIR", "")

def _safe_open(file, mode="r", *args, **kwargs):
    file_str = str(file)
    if any(m in mode for m in ("w", "a", "x")):
        if _OUTPUT_DIR and not os.path.abspath(file_str).startswith(_OUTPUT_DIR):
            raise PermissionError(
                f"Sandbox: write denied outside output dir: {{file_str!r}}"
            )
    return _real_open(file, mode, *args, **kwargs)

builtins.open = _safe_open

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

# CODE_GEN_USER and CODE_GEN_SYSTEM are imported from cortex.prompts.


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

    def _detect_language(self, source_code: str) -> str:
        """Read # LANGUAGE: <lang> from header comments. Default: python."""
        for line in source_code.splitlines():
            line = line.strip()
            if line.startswith("# LANGUAGE:"):
                lang = line[len("# LANGUAGE:"):].strip().lower()
                return lang
            if line and not line.startswith("#"):
                break
        return "python"

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

    def _validate_source(self, source_code: str, language: str = "python") -> None:
        """Static checks on generated code before execution."""
        if language == "python":
            if REQUIRED_ENTRYPOINT not in source_code:
                raise CortexSecurityError(
                    "Generated Python code must define a run(input) function."
                )
            if BLOCKED_IMPORTS_PATTERN.search(source_code):
                raise CortexSecurityError(
                    "Generated code contains a blocked import statement."
                )

    def _parse_polyglot_packages(self, language: str, source_code: str) -> list[str]:
        """Extract `# NPM_PACKAGES:` / `# GEM_PACKAGES:` / `# GO_PACKAGES:` headers."""
        spec = _LANG_PACKAGE_HEADERS.get(language)
        if not spec:
            return []
        header, _ = spec
        for line in source_code.splitlines():
            line = line.strip()
            if line.startswith(header):
                raw = line[len(header):].strip()
                return [p.strip() for p in raw.split(",") if p.strip()]
            if line and not line.startswith("#") and not line.startswith("//"):
                break
        return []

    async def _install_polyglot_packages(
        self, language: str, packages: list[str], output_dir: str,
    ) -> tuple[bool, str]:
        """Install language-specific packages. Returns (ok, message)."""
        spec = _LANG_PACKAGE_HEADERS.get(language)
        if not spec or not packages:
            return True, ""
        _, install_cmd = spec
        # Filter blocked packages from any ecosystem
        allowed = [p for p in packages if p.lower().split("@")[0] not in BLOCKED_PACKAGES]
        if not allowed:
            return True, "(no packages allowed)"
        # Substitute {out} placeholder
        cmd = [arg.replace("{out}", str(output_dir)) for arg in install_cmd] + allowed
        logger.info("Polyglot install: %s", cmd)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(output_dir),
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=120
            )
            if proc.returncode != 0:
                err = stderr_b.decode("utf-8", errors="replace")
                return False, f"install failed: {err[:300]}"
            return True, f"installed {len(allowed)} packages"
        except FileNotFoundError:
            return False, f"package manager not found: {cmd[0]!r}"
        except asyncio.TimeoutError:
            return False, "package install timed out after 120s"

    async def _execute_compiled(
        self,
        language: str,
        source_code: str,
        task_input: dict,
        output_dir: str,
    ) -> SandboxResult:
        """Compile-then-run path for rust / java / kotlin / c."""
        import json as _json

        spec = _LANG_COMPILED[language]
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        src_path = Path(output_dir) / f"cortex_script{spec['ext']}"
        src_path.write_text(source_code, encoding="utf-8")

        bin_path = (
            str(Path(output_dir) / spec["bin_name"])
            if spec.get("bin_name") else None
        )

        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self._base_path),
            "CORTEX_OUTPUT_DIR": str(Path(output_dir).resolve()),
            "CORTEX_TASK_INPUT": _json.dumps(task_input),
        }

        # Compile step (if needed)
        if spec.get("compile"):
            compile_cmd = [
                arg.replace("{src}", str(src_path))
                   .replace("{bin}", bin_path or "")
                for arg in spec["compile"]
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *compile_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(output_dir),
                )
                _, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout,
                )
                if proc.returncode != 0:
                    return SandboxResult(
                        error=f"{language} compile failed: {stderr_b.decode(errors='replace')[:500]}",
                        exit_code=proc.returncode,
                    )
            except FileNotFoundError:
                return SandboxResult(
                    error=f"Compiler not found for language '{language}': {compile_cmd[0]!r}",
                    exit_code=1,
                )

        # Run step
        run_cmd = [
            arg.replace("{src}", str(src_path)).replace("{bin}", bin_path or "")
            for arg in spec["run"]
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *run_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(output_dir),
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout,
            )
        except FileNotFoundError:
            return SandboxResult(
                error=f"Runtime not found for language '{language}': {run_cmd[0]!r}",
                exit_code=1,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return SandboxResult(
                error=f"Execution timed out after {self._timeout}s",
                exit_code=124,
            )

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        return SandboxResult(
            stdout=self._validator.validate_text(stdout or stderr[:2000]),
            stderr=stderr[:2000] if proc.returncode != 0 else "",
            exit_code=proc.returncode,
            output_files=[fp for fp, _ in self._validator.collect_output_files(output_dir)],
            error=stderr[:2000] if proc.returncode != 0 else None,
        )

    async def _execute_polyglot(
        self,
        language: str,
        source_code: str,
        task_input: dict,
        output_dir: str,
    ) -> SandboxResult:
        """Execute non-Python source code by writing it to output_dir and running it.

        Supports two execution modes:
        - Interpreted (entries in _LANG_RUNNERS): write source, run via interpreter.
        - Compiled (entries in _LANG_COMPILED): write source, compile, run binary.

        Honors `# NPM_PACKAGES:` / `# GEM_PACKAGES:` / `# GO_PACKAGES:` headers
        by installing dependencies into output_dir before execution.
        """
        import json as _json

        if language in _LANG_COMPILED:
            return await self._execute_compiled(language, source_code, task_input, output_dir)

        if language not in _LANG_RUNNERS:
            supported = sorted(set(_LANG_RUNNERS) | set(_LANG_COMPILED) | {"python"})
            return SandboxResult(
                error=f"Unsupported language: {language!r}. Supported: {', '.join(supported)}",
                exit_code=1,
            )

        ext, interpreter = _LANG_RUNNERS[language]
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        src_path = str(Path(output_dir) / f"cortex_script{ext}")

        with open(src_path, "w", encoding="utf-8") as fh:
            fh.write(source_code)

        if language in ("shell", "bash"):
            import stat
            os.chmod(src_path, os.stat(src_path).st_mode | stat.S_IEXEC)

        # Install declared packages (npm/gem/go-get)
        packages = self._parse_polyglot_packages(language, source_code)
        installed = []
        if packages:
            ok, msg = await self._install_polyglot_packages(language, packages, output_dir)
            if not ok:
                return SandboxResult(error=msg, exit_code=1)
            installed = packages

        cmd = interpreter + [src_path]
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self._base_path),
            "CORTEX_OUTPUT_DIR": str(Path(output_dir).resolve()),
            "CORTEX_TASK_INPUT": _json.dumps(task_input),
        }
        # Add node_modules/.bin to PATH for TS/Node so locally-installed CLIs work
        if language in ("typescript", "ts", "node", "javascript"):
            env["PATH"] = f"{output_dir}/node_modules/.bin:{env['PATH']}"

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
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
        except FileNotFoundError:
            return SandboxResult(
                error=f"Interpreter not found for language '{language}': {interpreter[0]!r}",
                exit_code=1,
            )

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        result_text = self._validator.validate_text(stdout or stderr[:2000])

        output_files = []
        for file_path, ext_found in self._validator.collect_output_files(output_dir):
            output_files.append(file_path)

        return SandboxResult(
            stdout=result_text,
            stderr=stderr[:2000] if proc.returncode != 0 else "",
            exit_code=proc.returncode,
            output_files=output_files,
            error=stderr[:2000] if proc.returncode != 0 else None,
            requirements_installed=installed,
        )

    # ── Background / long-running processes ───────────────────────────────────

    async def execute_background(
        self,
        source_code: str,
        task_input: dict,
        session_id: str,
        output_dir: str,
    ) -> SandboxResult:
        """Start a long-running process (server / daemon) and return immediately.

        Writes the PID to {output_dir}/.cortex_pid for downstream tasks to query.
        Stdout/stderr stream to {output_dir}/.cortex_bg.log so app_control can tail it.
        Returns a SandboxResult with stdout="Started PID <n>" — no blocking wait.
        """
        import json as _json
        import subprocess

        language = self._detect_language(source_code)
        self._validate_source(source_code, language)
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        if language == "python":
            python = await self.ensure_venv()
            wrapped = _SANDBOX_WRAPPER.format(user_code=source_code)
            src_path = Path(output_dir) / "cortex_bg.py"
            src_path.write_text(wrapped, encoding="utf-8")
            cmd = [python, str(src_path)]
        elif language in _LANG_RUNNERS:
            ext, interp = _LANG_RUNNERS[language]
            src_path = Path(output_dir) / f"cortex_bg{ext}"
            src_path.write_text(source_code, encoding="utf-8")
            cmd = interp + [str(src_path)]
        else:
            return SandboxResult(
                error=f"Background mode does not support language {language!r}",
                exit_code=1,
            )

        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self._base_path),
            "CORTEX_OUTPUT_DIR": str(Path(output_dir).resolve()),
            "CORTEX_TASK_INPUT": _json.dumps(task_input),
        }

        log_path = Path(output_dir) / ".cortex_bg.log"
        pid_path = Path(output_dir) / ".cortex_pid"

        log_fh = open(log_path, "wb")
        # Use the blocking subprocess (not asyncio) for true detached process
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(output_dir),
            start_new_session=True,
        )
        pid_path.write_text(str(proc.pid))

        return SandboxResult(
            stdout=f"Started PID {proc.pid}\nLog: {log_path}\nPID file: {pid_path}",
            exit_code=0,
            output_files=[str(log_path), str(pid_path)],
        )

    # ── Streaming execution ───────────────────────────────────────────────────

    async def execute_streaming(
        self,
        source_code: str,
        task_input: dict,
        session_id: str,
        output_dir: str,
        on_line=None,
    ):
        """Like execute(), but invokes on_line(text) for each stdout line as it arrives.

        on_line is an optional async callable. Useful for surfacing progress from
        long-running scripts (data pipelines, builds) to the UI as StatusEvents.
        Returns the same SandboxResult as execute() once the process exits.
        """
        import json as _json

        language = self._detect_language(source_code)
        self._validate_source(source_code, language)
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        if language == "python":
            python = await self.ensure_venv()
            wrapped = _SANDBOX_WRAPPER.format(user_code=source_code)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8",
            ) as tmp:
                tmp.write(wrapped)
                tmp_path = tmp.name
            cmd = [python, tmp_path]
        elif language in _LANG_RUNNERS:
            ext, interp = _LANG_RUNNERS[language]
            src_path = Path(output_dir) / f"cortex_script{ext}"
            src_path.write_text(source_code, encoding="utf-8")
            cmd = interp + [str(src_path)]
            tmp_path = None
        else:
            return SandboxResult(
                error=f"Streaming mode does not support language {language!r}",
                exit_code=1,
            )

        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self._base_path),
            "CORTEX_OUTPUT_DIR": str(Path(output_dir).resolve()),
            "CORTEX_TASK_INPUT": _json.dumps(task_input),
        }

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(output_dir),
            )
            collected = []
            async def _pump():
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip("\n")
                    collected.append(text)
                    if on_line is not None:
                        try:
                            await on_line(text)
                        except Exception as e:
                            logger.debug("on_line callback failed: %s", e)

            try:
                await asyncio.wait_for(
                    asyncio.gather(_pump(), proc.wait()),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                return SandboxResult(
                    error=f"Streaming execution timed out after {self._timeout}s",
                    exit_code=124,
                )
            stderr_b = await proc.stderr.read()
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

        stdout = "\n".join(collected)
        stderr = stderr_b.decode("utf-8", errors="replace")

        # Extract sentinel result for Python; pass through stdout for polyglot.
        if language == "python" and "__CORTEX_RESULT_START__" in stdout:
            start = stdout.index("__CORTEX_RESULT_START__") + len("__CORTEX_RESULT_START__\n")
            end = stdout.index("__CORTEX_RESULT_END__")
            result_text = stdout[start:end].strip()
        else:
            result_text = stdout

        return SandboxResult(
            stdout=self._validator.validate_text(result_text or stderr[:2000]),
            stderr=stderr[:2000] if proc.returncode != 0 else "",
            exit_code=proc.returncode or 0,
            output_files=[fp for fp, _ in self._validator.collect_output_files(output_dir)],
            error=stderr[:2000] if proc.returncode else None,
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

        Non-Python languages (# LANGUAGE: node/shell/ruby/go) bypass the venv
        and run directly via the system interpreter.
        """
        language = self._detect_language(source_code)
        self._validate_source(source_code, language)

        if language != "python":
            return await self._execute_polyglot(language, source_code, task_input, output_dir)

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
        prompt = CODE_GEN_USER.format(
            task_name=task_name,
            description=description,
            instruction=instruction,
            output_format=output_format,
        )

        tokens = []
        async for token in llm_client.stream(
            messages=[{"role": "user", "content": prompt}],
            system=CODE_GEN_SYSTEM,
            provider_name="default",
        ):
            tokens.append(token)

        source_code = "".join(tokens).strip()

        # Strip accidental markdown fences
        if source_code.startswith("```"):
            lines = source_code.splitlines()
            source_code = "\n".join(
                line for line in lines if not line.strip().startswith("```")
            )

        logger.debug("Generated code for '%s':\n%s", task_name, source_code[:800])

        result = await self.execute(
            source_code=source_code,
            task_input=task_input,
            session_id=session_id,
            output_dir=output_dir,
        )
        return source_code, result
