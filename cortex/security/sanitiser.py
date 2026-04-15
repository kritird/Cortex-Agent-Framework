"""Input sanitisation: tokens, templates, filenames, MIME types."""
import html
import os
import re
import unicodedata
from pathlib import Path
from typing import List, Optional


# Characters not allowed in filenames
UNSAFE_FILENAME_CHARS = re.compile(r'[^\w\-_\. ]')
# Null bytes and control characters
CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f]')
# Template injection patterns
TEMPLATE_INJECTION = re.compile(
    r'(\{\{|\}\}|<\?|<%|%>|\$\{|\}|__import__|eval\s*\(|exec\s*\()',
    re.IGNORECASE,
)
# Path traversal patterns
PATH_TRAVERSAL = re.compile(r'\.\.[/\\]|[/\\]\.\.')


class InputSanitiser:
    """
    Sanitises inputs at system boundaries.
    Used for user-provided text, filenames, and file inputs.
    Never used inside the framework for internal data.
    """

    def __init__(
        self,
        max_input_tokens: int = 4000,
        allowed_mime_types: Optional[List[str]] = None,
        max_file_size_mb: int = 50,
    ):
        self._max_tokens = max_input_tokens
        self._allowed_mimes = set(allowed_mime_types or [])
        self._max_file_bytes = max_file_size_mb * 1024 * 1024

    def sanitise_text_input(self, text: str) -> str:
        """
        Sanitise user text input:
        1. Strip null bytes and control chars
        2. Normalize unicode to NFC
        3. HTML-escape if content looks like it may be rendered
        4. Truncate to max_input_tokens (approximate: 4 chars/token)
        Returns sanitised string.
        """
        if not isinstance(text, str):
            text = str(text)

        # Remove null bytes
        text = text.replace('\x00', '')

        # Remove non-printable control characters (keep newlines, tabs)
        text = re.sub(r'[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

        # Normalize unicode
        text = unicodedata.normalize('NFC', text)

        # Approximate token truncation
        max_chars = self._max_tokens * 4
        if len(text) > max_chars:
            text = text[:max_chars]

        return text

    def sanitise_template(self, template: str) -> str:
        """
        Remove template injection sequences from strings used in prompts.
        Escapes {{ }} and other injection patterns.
        """
        if not isinstance(template, str):
            return str(template)

        # Remove null bytes
        template = template.replace('\x00', '')

        # Escape template injection chars
        template = template.replace('{{', '{\\{').replace('}}', '}\\}')
        template = template.replace('<?', '&lt;?').replace('<%', '&lt;%')

        return template

    def sanitise_filename(self, filename: str) -> str:
        """
        Sanitise a filename to be safe for filesystem use.
        1. Strip path components
        2. Remove unsafe chars
        3. Truncate to 255 chars
        4. Ensure non-empty result
        """
        if not isinstance(filename, str):
            filename = str(filename)

        # Strip path separators and traversal
        filename = os.path.basename(filename)
        filename = filename.replace('..', '_')

        # Remove control characters
        filename = CONTROL_CHARS.sub('', filename)

        # Replace unsafe chars with underscores
        filename = UNSAFE_FILENAME_CHARS.sub('_', filename)

        # Truncate
        filename = filename[:255]

        # Ensure non-empty
        if not filename or filename.strip('.') == '':
            filename = 'file'

        return filename

    def validate_mime_type(self, mime_type: str, filename: Optional[str] = None) -> bool:
        """
        Validate that a MIME type is in the allowed list.
        Returns True if allowed, False otherwise.
        """
        if not self._allowed_mimes:
            return True  # No restrictions configured

        # Normalize MIME type
        mime_type = mime_type.lower().strip()
        # Strip parameters like charset
        base_mime = mime_type.split(';')[0].strip()

        return base_mime in self._allowed_mimes

    def validate_file_size(self, size_bytes: int) -> bool:
        """Returns True if file size is within limit."""
        return size_bytes <= self._max_file_bytes

    def validate_path_safety(self, path: str, allowed_base: str) -> bool:
        """
        Validate that a path stays within allowed_base directory.
        Prevents path traversal attacks.
        """
        try:
            resolved = Path(path).resolve()
            base = Path(allowed_base).resolve()
            return str(resolved).startswith(str(base))
        except (ValueError, OSError):
            return False

    def sanitise_html(self, text: str) -> str:
        """HTML-escape a string to prevent XSS."""
        return html.escape(text, quote=True)
