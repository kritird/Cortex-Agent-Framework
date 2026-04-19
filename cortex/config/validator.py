"""Schema validation with detailed error messages."""
from typing import Any, Dict, List



def validate_config(data: Dict[str, Any]) -> List[str]:
    """
    Validate cortex.yaml data dict. Returns list of error strings.
    Empty list means valid.
    """
    errors = []

    # Required top-level keys
    for key in ["agent", "llm_access", "storage"]:
        if key not in data:
            errors.append(f'Missing required top-level key: "{key}"')

    if "agent" in data:
        agent = data["agent"]
        if not isinstance(agent, dict):
            errors.append("agent: must be a mapping")
        else:
            for field in ["name", "description"]:
                if not agent.get(field):
                    errors.append(f"agent.{field}: is required and must not be empty")

    if "llm_access" in data:
        llm = data["llm_access"]
        if not isinstance(llm, dict):
            errors.append("llm_access: must be a mapping")
        elif "default" not in llm:
            errors.append('llm_access: missing required key "default"')
        else:
            default_prov = llm["default"]
            if not isinstance(default_prov, dict):
                errors.append("llm_access.default: must be a mapping")
            elif "provider" not in default_prov:
                errors.append('llm_access.default: missing required key "provider"')

    if "storage" in data:
        storage = data["storage"]
        if not isinstance(storage, dict):
            errors.append("storage: must be a mapping")
        elif not storage.get("base_path"):
            errors.append("storage.base_path: is required and must not be empty")

    # Validate task_types depends_on references
    if "task_types" in data and isinstance(data["task_types"], list):
        task_names = {
            t["name"] for t in data["task_types"]
            if isinstance(t, dict) and "name" in t
        }
        for i, task in enumerate(data["task_types"]):
            if not isinstance(task, dict):
                continue
            for dep in task.get("depends_on", []):
                if dep not in task_names:
                    errors.append(
                        f'task_types[{i}].depends_on[]: "{dep}" is not defined in task_types. '
                        f'Add a task type named "{dep}" or correct this reference.'
                    )

    # Validate tool_servers referenced by task_types
    if "task_types" in data and "tool_servers" in data:
        server_names = set(data.get("tool_servers", {}).keys())
        for i, task in enumerate(data.get("task_types", [])):
            if not isinstance(task, dict):
                continue
            for srv in task.get("tool_servers", []):
                if srv not in server_names:
                    errors.append(
                        f'task_types[{i}].tool_servers[]: "{srv}" is not declared in tool_servers.'
                    )

    # Validate validation threshold floor
    if "validation" in data and isinstance(data["validation"], dict):
        threshold = data["validation"].get("threshold", 0.75)
        if isinstance(threshold, (int, float)) and threshold < 0.60:
            errors.append(
                f"validation.threshold: value {threshold} is below the minimum allowed floor of 0.60. "
                f"Set threshold >= 0.60 to protect response quality."
            )

    return errors
