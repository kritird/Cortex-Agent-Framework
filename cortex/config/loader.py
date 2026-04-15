"""YAML config loader with env var interpolation and line number tracking."""
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from cortex.config.schema import CortexConfig
from cortex.config.validator import validate_config
from cortex.exceptions import CortexConfigError


ENV_VAR_PATTERN = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)')


def _interpolate_env_vars(value: Any, source_loc: Optional[Dict] = None) -> Any:
    """Recursively interpolate ${VAR} and $VAR references from environment."""
    if isinstance(value, str):
        def replace_var(match: re.Match) -> str:
            var_name = match.group(1) or match.group(2)
            env_val = os.environ.get(var_name)
            if env_val is None:
                # Store unresolved for later validator reporting
                return match.group(0)
            return env_val
        return ENV_VAR_PATTERN.sub(replace_var, value)
    elif isinstance(value, dict):
        return {k: _interpolate_env_vars(v, source_loc) for k, v in value.items()}
    elif isinstance(value, list):
        return [_interpolate_env_vars(item, source_loc) for item in value]
    return value


def _find_unresolved_env_vars(data: Any, path: str = "") -> list[tuple[str, str]]:
    """Find any remaining ${VAR} references that weren't resolved."""
    unresolved = []
    if isinstance(data, str):
        for match in ENV_VAR_PATTERN.finditer(data):
            var_name = match.group(1) or match.group(2)
            if os.environ.get(var_name) is None:
                unresolved.append((path, var_name))
    elif isinstance(data, dict):
        for k, v in data.items():
            sub_path = f"{path}.{k}" if path else k
            unresolved.extend(_find_unresolved_env_vars(v, sub_path))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            sub_path = f"{path}[{i}]"
            unresolved.extend(_find_unresolved_env_vars(item, sub_path))
    return unresolved


class _LineNumberLoader(yaml.SafeLoader):
    """YAML loader that tracks line numbers."""
    pass


def _construct_yaml_map_with_line_numbers(loader, node):
    pairs = loader.construct_pairs(node, deep=True)
    return dict(pairs)


_LineNumberLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_yaml_map_with_line_numbers,
)


def load_config(path: str) -> CortexConfig:
    """
    Load and parse cortex.yaml, interpolating env vars and validating schema.

    Raises CortexConfigError with structured messages on any error.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise CortexConfigError(f"Configuration file not found: {path}")

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
    except OSError as e:
        raise CortexConfigError(f"Cannot read configuration file: {e}")

    try:
        raw_data = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        line = 0
        col = 0
        if hasattr(e, 'problem_mark'):
            line = e.problem_mark.line + 1
            col = e.problem_mark.column + 1
        raise CortexConfigError(
            f"YAML parse error: {e.problem if hasattr(e, 'problem') else str(e)}",
            yaml_path="(root)",
            line=line,
            column=col,
        )

    if not isinstance(raw_data, dict):
        raise CortexConfigError("cortex.yaml must be a YAML mapping (dict) at the root level.")

    # Interpolate environment variables
    interpolated = _interpolate_env_vars(raw_data)

    # Check for unresolved env vars (only in known env-var fields)
    unresolved = _find_unresolved_env_vars(interpolated)

    errors = []
    for yaml_path, var_name in unresolved:
        errors.append(
            f'  {yaml_path}: environment variable "{var_name}" is not set. '
            f'Set it in .env or export before starting.'
        )

    if errors:
        error_list = "\n".join(f"  [{i+1}] {e}" for i, e in enumerate(errors))
        raise CortexConfigError(
            f"CORTEX CONFIG ERROR — {len(errors)} issue(s) found in {path}\n\n{error_list}"
        )

    # Validate and parse through Pydantic
    errors = validate_config(interpolated)
    if errors:
        error_list = "\n".join(f"  [{i+1}] {e}" for i, e in enumerate(errors))
        raise CortexConfigError(
            f"CORTEX CONFIG ERROR — {len(errors)} issue(s) found in {path}\n\n{error_list}"
        )

    try:
        config = CortexConfig.model_validate(interpolated)
    except Exception as e:
        raise CortexConfigError(f"Configuration validation failed: {e}")

    return config
