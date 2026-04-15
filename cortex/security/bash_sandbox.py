"""Bash sandbox with path jail and command filtering."""
import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import List, Optional

from cortex.exceptions import CortexSecurityError


BLOCKED_COMMANDS = {
    'rm', 'rmdir', 'shred', 'dd', 'mkfs', 'fdisk',
    'sudo', 'su', 'chmod', 'chown', 'chgrp',
    'curl', 'wget', 'nc', 'netcat', 'ssh', 'scp', 'rsync',
    'python', 'python3', 'node', 'ruby', 'perl', 'php',
    'bash', 'sh', 'zsh', 'fish', 'csh', 'ksh',
    'eval', 'exec', 'source',
    'kill', 'pkill', 'killall', 'nohup',
    'crontab', 'at', 'systemctl', 'service',
    'mount', 'umount', 'docker', 'podman', 'kubectl',
}

BLOCKED_PATH_REGEX = re.compile(
    r'/etc/|/proc/|/sys/|/dev/|/root/|~/'
)


class BashSandbox:
    """Executes bash commands within a strict security sandbox."""

    def __init__(self, session_storage_path: str, timeout_seconds: int = 30):
        self._jail = Path(session_storage_path).resolve()
        self._timeout = timeout_seconds

    def _validate_command(self, command: str) -> None:
        if '\x00' in command:
            raise CortexSecurityError("Command contains null bytes")
        try:
            tokens = shlex.split(command)
        except ValueError as e:
            raise CortexSecurityError(f"Cannot parse command: {e}")
        if not tokens:
            raise CortexSecurityError("Empty command")
        cmd_name = os.path.basename(tokens[0]).lower()
        if cmd_name in BLOCKED_COMMANDS:
            raise CortexSecurityError(f"Command '{cmd_name}' is not allowed in the sandbox")
        if '..' in command:
            raise CortexSecurityError("Path traversal (..) is not allowed")
        if BLOCKED_PATH_REGEX.search(command):
            raise CortexSecurityError("Command references restricted paths")
        if any(c in command for c in ['`', '$(']):
            raise CortexSecurityError("Command substitution is not allowed")

    def _build_safe_env(self) -> dict:
        return {
            'PATH': '/usr/local/bin:/usr/bin:/bin',
            'HOME': str(self._jail),
            'TMPDIR': str(self._jail / 'tmp'),
            'LANG': 'en_US.UTF-8',
        }

    async def execute(self, command: str) -> str:
        self._validate_command(command)
        self._jail.mkdir(parents=True, exist_ok=True)
        (self._jail / 'tmp').mkdir(exist_ok=True)
        safe_env = self._build_safe_env()
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._jail),
            env=safe_env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise asyncio.TimeoutError(f"Command timed out after {self._timeout}s")
        output = stdout.decode('utf-8', errors='replace')
        err = stderr.decode('utf-8', errors='replace')
        if proc.returncode != 0 and err:
            output = f"{output}\nSTDERR: {err}".strip()
        return output

    def validate_path(self, path: str) -> Path:
        try:
            resolved = (self._jail / path).resolve()
        except (ValueError, OSError) as e:
            raise CortexSecurityError(f"Invalid path: {e}")
        if not str(resolved).startswith(str(self._jail)):
            raise CortexSecurityError(f"Path '{path}' escapes the session jail")
        return resolved
