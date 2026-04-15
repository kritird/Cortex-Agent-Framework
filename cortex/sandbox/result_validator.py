"""ResultValidator — ensures sandbox output is text/doc only, never executable."""
import mimetypes
import os
from pathlib import Path
from typing import Optional

from cortex.exceptions import CortexSecurityError

# Allowed output file extensions (text and document formats only)
ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".rst", ".csv", ".json", ".yaml", ".yml",
    ".html", ".xml", ".toml", ".log", ".tsv", ".ndjson",
}

# Explicitly blocked regardless of MIME
BLOCKED_EXTENSIONS = {
    ".py", ".sh", ".bash", ".zsh", ".rb", ".js", ".ts",
    ".exe", ".bin", ".so", ".dylib", ".dll", ".out",
    ".bat", ".cmd", ".ps1", ".vbs",
    ".zip", ".tar", ".gz", ".bz2",
    ".pkl", ".pickle", ".db", ".sqlite",
}

# Max result text size (5 MB)
MAX_TEXT_BYTES = 5 * 1024 * 1024


class ResultValidator:
    """
    Validates sandbox execution results.
    Rules:
    - Text output (stdout): always allowed, size-capped
    - Output files: only text/doc extensions, never executable
    - No symlinks pointing outside the output directory
    - No hidden files
    """

    def validate_text(self, text: str) -> str:
        """Validate and cap plain text output from the sandbox."""
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            text = text[:MAX_TEXT_BYTES // 4]  # approx char cap
            text += "\n\n[OUTPUT TRUNCATED — exceeded 5 MB limit]"
        return text

    def validate_output_file(self, file_path: str, output_dir: str) -> None:
        """
        Validate a file written by sandbox code.
        Raises CortexSecurityError if the file violates policy.
        """
        path = Path(file_path).resolve()
        out_dir = Path(output_dir).resolve()

        # Must be within the designated output directory
        if not str(path).startswith(str(out_dir)):
            raise CortexSecurityError(
                f"Sandbox tried to write outside output directory: {file_path}"
            )

        # No hidden files
        if path.name.startswith("."):
            raise CortexSecurityError(
                f"Sandbox produced a hidden file: {path.name}"
            )

        # No symlinks
        if path.is_symlink():
            raise CortexSecurityError(
                f"Sandbox produced a symlink: {file_path}"
            )

        ext = path.suffix.lower()

        # Blocked extensions take priority
        if ext in BLOCKED_EXTENSIONS:
            raise CortexSecurityError(
                f"Sandbox output file has a blocked extension '{ext}'. "
                f"Only text and document formats are allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )

        # Must be an explicitly allowed extension
        if ext and ext not in ALLOWED_EXTENSIONS:
            raise CortexSecurityError(
                f"Sandbox output file extension '{ext}' is not in the allowed list. "
                f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )

        # Double-check MIME type for files with no extension
        if not ext:
            mime, _ = mimetypes.guess_type(str(path))
            if mime and not mime.startswith("text/"):
                raise CortexSecurityError(
                    f"Sandbox output file has non-text MIME type: {mime}"
                )

        # Check file is not executable
        if os.access(str(path), os.X_OK):
            raise CortexSecurityError(
                f"Sandbox output file has executable bit set: {file_path}"
            )

    def collect_output_files(self, output_dir: str) -> list[tuple[str, str]]:
        """
        Scan output_dir for files written by the sandbox.
        Returns list of (file_path, extension) for valid files.
        Invalid files are deleted silently.
        """
        results = []
        out = Path(output_dir)
        if not out.exists():
            return results
        for f in out.rglob("*"):
            if not f.is_file():
                continue
            try:
                self.validate_output_file(str(f), output_dir)
                results.append((str(f), f.suffix.lower()))
            except CortexSecurityError:
                # Delete policy-violating files
                f.unlink(missing_ok=True)
        return results
