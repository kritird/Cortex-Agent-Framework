"""Local HTTP server for the setup wizard at localhost:7799."""
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from aiohttp import web


STATIC_DIR = Path(__file__).parent / "static"


class WizardServer:
    """Serves the setup wizard HTML and handles config generation API."""

    def __init__(self, port: int = 7799, host: str = "127.0.0.1"):
        self._port = port
        self._host = host
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site = None

    def _build_app(self) -> web.Application:
        app = web.Application()

        async def handle_index(request):
            index_path = STATIC_DIR / "index.html"
            if index_path.exists():
                return web.FileResponse(index_path)
            return web.Response(text="<h1>Setup wizard UI not found.</h1>", content_type="text/html")

        async def handle_logo(request):
            # Serve the repo-level cortex-logo.svg so the wizard always renders
            # the canonical brand asset, regardless of where cortex is installed.
            repo_logo = Path(__file__).resolve().parents[2] / "logo" / "cortex-logo.svg"
            if repo_logo.exists():
                return web.FileResponse(repo_logo)
            return web.Response(status=404, text="logo not found")

        async def handle_generate(request):
            data = await request.json()
            config_yaml = _generate_config(data)
            return web.json_response({"config": config_yaml, "status": "ok"})

        async def handle_validate(request):
            data = await request.json()
            config_text = data.get("config", "")
            mode = (data.get("mode") or "").strip()
            config_path = "_cortex_wizard_tmp.yaml"
            try:
                with open(config_path, "w") as f:
                    f.write(config_text)
                from cortex.config.loader import load_config
                load_config(config_path)
            except Exception as e:
                return web.json_response({"valid": False, "errors": [str(e)]})
            finally:
                if os.path.exists(config_path):
                    os.unlink(config_path)

            mode_errors = _validate_publish_mode(config_text, mode) if mode else []
            if mode_errors:
                return web.json_response({"valid": False, "errors": mode_errors})
            return web.json_response({"valid": True, "errors": []})

        async def handle_save(request):
            data = await request.json()
            config_text = data.get("config", "")
            path = data.get("path", "cortex.yaml")
            with open(path, "w") as f:
                f.write(config_text)
            return web.json_response({"saved": True, "path": path})

        async def handle_publish(request):
            data = await request.json()
            mode = data.get("mode", "")
            config_path = data.get("config_path", "cortex.yaml")
            result = _run_publish(mode, config_path, data)
            return web.json_response(result)

        async def handle_providers(request):
            """Return list of supported LLM providers."""
            return web.json_response({"providers": _get_providers()})

        async def handle_load_config(request):
            """Load existing cortex.yaml and return parsed data for pre-population."""
            config_path = request.query.get("path", "cortex.yaml")
            result = _load_existing_config(config_path)
            return web.json_response(result)

        async def handle_blueprint_get(request):
            """Fetch a blueprint by task name for the 'show blueprint' button.

            Query params:
              task_name     (required) — task type name as in cortex.yaml
              blueprint_ref (required) — the value of task_type.blueprint
              config_path   (optional) — cortex.yaml to resolve blueprint dir from
            """
            task_name = request.query.get("task_name", "")
            ref = request.query.get("blueprint_ref", "")
            config_path = request.query.get("config_path", "cortex.yaml")
            if not task_name or not ref:
                return web.json_response(
                    {"exists": False, "error": "task_name and blueprint_ref required"},
                    status=400,
                )
            result = _load_or_preview_blueprint(config_path, task_name, ref)
            return web.json_response(result)

        async def handle_blueprint_save(request):
            """Save edited blueprint content from the wizard back to disk/backend."""
            data = await request.json()
            task_name = data.get("task_name", "")
            ref = data.get("blueprint_ref", "")
            content = data.get("content", "")
            config_path = data.get("config_path", "cortex.yaml")
            if not task_name or not ref or not content:
                return web.json_response(
                    {"saved": False, "error": "task_name, blueprint_ref, content required"},
                    status=400,
                )
            result = _save_blueprint_content(config_path, task_name, ref, content)
            return web.json_response(result)

        app.router.add_get("/", handle_index)
        app.router.add_get("/logo.svg", handle_logo)
        app.router.add_post("/api/generate", handle_generate)
        app.router.add_post("/api/validate", handle_validate)
        app.router.add_post("/api/save", handle_save)
        app.router.add_post("/api/publish", handle_publish)
        app.router.add_get("/api/providers", handle_providers)
        app.router.add_get("/api/load-config", handle_load_config)
        app.router.add_get("/api/blueprint", handle_blueprint_get)
        app.router.add_post("/api/blueprint", handle_blueprint_save)
        if STATIC_DIR.exists():
            app.router.add_static("/static", STATIC_DIR)
        return app

    async def start(self) -> str:
        self._app = self._build_app()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        return f"http://{self._host}:{self._port}"

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()


def _validate_publish_mode(config_text: str, mode: str) -> list:
    """Check that the YAML has the mandatory fields for the chosen publish target.

    The base schema load already covers framework-wide requirements. This only
    catches gaps that matter for a specific ``cortex publish <mode>`` command —
    e.g. publishing a Chat UI with ``ui.enabled: false``.
    """
    import yaml
    try:
        raw = yaml.safe_load(config_text) or {}
    except Exception as e:
        return [f"YAML parse failed: {e}"]

    errors: list = []

    if mode == "ui":
        ui = raw.get("ui") or {}
        if not ui.get("enabled"):
            errors.append(
                "Chat UI publish target requires `ui.enabled: true`. "
                "Enable the Chat UI step in the wizard before publishing as UI."
            )
        auth = (ui.get("auth") or {})
        auth_mode = auth.get("mode", "none")
        if auth_mode == "token" and not auth.get("token"):
            errors.append(
                "Chat UI auth.mode is 'token' but no `ui.auth.token` is set. "
                "Set a token in the Chat UI step or switch auth to 'none'."
            )
        if auth_mode == "basic" and (not auth.get("username") or not auth.get("password")):
            errors.append(
                "Chat UI auth.mode is 'basic' but `ui.auth.username` and/or "
                "`ui.auth.password` are missing. Fill both in the Chat UI step."
            )

    if mode == "mcp":
        if not raw.get("task_types"):
            errors.append(
                "MCP publish target needs at least one entry under `task_types` — "
                "that's what the MCP server exposes to callers. Add a task type "
                "in the Task Types step before publishing as MCP."
            )

    if mode == "docker":
        # Docker containerises whatever dev mode runs, so the baseline schema
        # is enough. Only flag the single gotcha: a local LLM base_url pointing
        # at the host's loopback will not be reachable from inside the container.
        llm_default = ((raw.get("llm_access") or {}).get("default") or {})
        base_url = (llm_default.get("base_url") or "").strip()
        if base_url and ("localhost" in base_url or "127.0.0.1" in base_url):
            errors.append(
                "Docker publish target: `llm_access.default.base_url` points at "
                f"'{base_url}', which won't resolve inside a container. Use "
                "'host.docker.internal' (macOS/Windows) or the host's LAN IP."
            )

    return errors


def _get_providers():
    return [
        {"value": "anthropic", "label": "Anthropic (Direct)", "models": ["claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-haiku-4-5-20251001"], "default_env": "ANTHROPIC_API_KEY"},
        {"value": "anthropic_compatible", "label": "Anthropic Compatible (Proxy/Gateway)", "models": ["claude-sonnet-4-20250514"], "default_env": "ANTHROPIC_API_KEY", "needs_base_url": True},
        {"value": "openai", "label": "OpenAI", "models": ["gpt-4o", "gpt-4o-mini", "o3-mini"], "default_env": "OPENAI_API_KEY"},
        {"value": "gemini", "label": "Google Gemini", "models": ["gemini-2.5-pro", "gemini-2.5-flash"], "default_env": "GEMINI_API_KEY"},
        {"value": "bedrock", "label": "AWS Bedrock", "models": ["anthropic.claude-sonnet-4-20250514-v1:0"], "default_env": "AWS_ACCESS_KEY_ID"},
        {"value": "azure_ai", "label": "Azure AI", "models": ["claude-sonnet-4-20250514"], "default_env": "AZURE_API_KEY", "needs_base_url": True},
        {"value": "grok", "label": "Grok (xAI)", "models": ["grok-3", "grok-3-mini"], "default_env": "XAI_API_KEY"},
        {"value": "mistral", "label": "Mistral", "models": ["mistral-large-latest"], "default_env": "MISTRAL_API_KEY"},
        {"value": "deepseek", "label": "DeepSeek", "models": ["deepseek-chat", "deepseek-reasoner"], "default_env": "DEEPSEEK_API_KEY"},
        {
            "value": "local",
            "label": "Local LLM (Ollama / LM Studio / vLLM)",
            "models": [
                "gemma4:e2b", "gemma4:e4b", "gemma4:26b", "gemma4:31b",
                "llama3.1", "llama3.2", "qwen2.5", "mistral", "phi4",
            ],
            "default_env": "",
            "default_base_url": "http://localhost:11434/v1",
            "needs_base_url": True,
            "api_key_optional": True,
        },
    ]


def _only_if(cfg: dict, key: str, value, default):
    """Set cfg[key]=value only if value differs from default.

    Small helper used by the advanced-field emitter so the generated YAML stays
    minimal — users who leave every Advanced expander untouched get the same
    clean output as before.
    """
    if value is None:
        return
    if isinstance(value, str) and value.strip() == "":
        return
    if value == default:
        return
    cfg[key] = value


def _parse_kv_text(text: str) -> dict:
    """Parse a textarea like 'Authorization: Bearer abc\\nX-Trace: 1' into a dict."""
    out: dict = {}
    if not text:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
        elif "=" in line:
            k, _, v = line.partition("=")
        else:
            continue
        k = k.strip()
        v = v.strip()
        if k:
            out[k] = v
    return out


def _parse_csv(text) -> list:
    if not text:
        return []
    if isinstance(text, list):
        return [str(x).strip() for x in text if str(x).strip()]
    return [x.strip() for x in str(text).split(",") if x.strip()]


def _parse_lines(text) -> list:
    if not text:
        return []
    if isinstance(text, list):
        return [str(x).strip() for x in text if str(x).strip()]
    return [x.strip() for x in str(text).splitlines() if x.strip()]


def _generate_config(data: dict) -> str:
    import json as _json
    import yaml

    # ─── Agent block ───
    agent_block = {
        "name": data.get("agent_name", "MyAgent"),
        "description": data.get("agent_description", "An AI agent"),
    }
    _only_if(agent_block, "synthesis_guidance", data.get("agent_synthesis_guidance"), "")

    time_cfg = {}
    _only_if(time_cfg, "default_max_wait_seconds", data.get("max_wait_seconds"), 120)
    _only_if(time_cfg, "default_task_timeout_seconds", data.get("default_task_timeout"), 40)
    if time_cfg:
        agent_block["time"] = time_cfg

    perf_cfg = {}
    _only_if(perf_cfg, "streaming_decomposition", data.get("agent_streaming_decomposition"), False)
    if perf_cfg:
        agent_block["performance"] = perf_cfg

    concurrency_cfg = {}
    _only_if(concurrency_cfg, "max_concurrent_sessions", data.get("max_concurrent_sessions"), 50)
    _only_if(concurrency_cfg, "max_concurrent_sessions_per_user", data.get("max_sessions_per_user"), 3)
    _only_if(concurrency_cfg, "max_tasks_per_session", data.get("max_tasks_per_session"), 20)
    _only_if(concurrency_cfg, "max_parallel_tasks", data.get("max_parallel_tasks"), 5)
    _only_if(concurrency_cfg, "max_mcp_agent_llm_calls", data.get("agent_max_mcp_agent_llm_calls"), 10)
    if concurrency_cfg:
        agent_block["concurrency"] = concurrency_cfg

    streaming_cfg = {}
    _only_if(streaming_cfg, "status_updates", data.get("agent_streaming_status_updates"), False)
    _only_if(streaming_cfg, "include_task_detail", data.get("agent_streaming_include_task_detail"), True)
    _only_if(streaming_cfg, "mcp_agent_updates", data.get("agent_streaming_mcp_agent_updates"), False)
    _only_if(streaming_cfg, "reconnect_buffer_size", data.get("agent_streaming_reconnect_buffer_size"), 50)
    _only_if(streaming_cfg, "min_delivery_interval_ms", data.get("agent_streaming_min_delivery_interval_ms"), 200)
    if streaming_cfg:
        agent_block["streaming"] = streaming_cfg

    if data.get("agent_clarification_enabled"):
        agent_block["clarification"] = {"enabled": True}

    scout_cfg = {}
    _only_if(scout_cfg, "enabled", data.get("scout_enabled"), True)
    _only_if(scout_cfg, "max_capabilities", data.get("scout_max_capabilities"), 30)
    _only_if(scout_cfg, "timeout_seconds", data.get("scout_timeout_seconds"), 10)
    ext_cfg = {}
    _only_if(ext_cfg, "enabled", data.get("scout_ext_enabled"), True)
    _only_if(ext_cfg, "auto_discovery_file", data.get("scout_ext_auto_discovery_file"), "cortex_auto_mcps.yaml")
    reg_sources = _parse_lines(data.get("scout_ext_registry_sources"))
    default_reg = [
        "https://registry.smithery.ai",
        "https://www.pulsemcp.com",
        "https://glama.ai",
        "https://mcp.so",
    ]
    if reg_sources and reg_sources != default_reg:
        ext_cfg["registry_sources"] = reg_sources
    _only_if(ext_cfg, "max_new_per_session", data.get("scout_ext_max_new_per_session"), 5)
    _only_if(ext_cfg, "max_stale_days", data.get("scout_ext_max_stale_days"), 30)
    _only_if(ext_cfg, "search_timeout_s", data.get("scout_ext_search_timeout_s"), 10.0)
    if ext_cfg:
        scout_cfg["external_discovery"] = ext_cfg
    if scout_cfg:
        agent_block["capability_scout"] = scout_cfg

    # ─── LLM default provider block ───
    llm_default = {
        "provider": data.get("provider", "anthropic"),
        "model": data.get("model", "claude-sonnet-4-20250514"),
        "api_key_env_var": data.get("api_key_env_var", "ANTHROPIC_API_KEY"),
        "max_tokens": data.get("max_tokens", 4096),
    }
    if data.get("base_url"):
        llm_default["base_url"] = data["base_url"]
    if data.get("temperature") is not None:
        llm_default["temperature"] = data["temperature"]
    headers = _parse_kv_text(data.get("llm_headers", ""))
    if headers:
        llm_default["headers"] = headers
    for src, dst in [
        ("llm_region_env_var", "region_env_var"),
        ("llm_access_key_env_var", "access_key_env_var"),
        ("llm_secret_key_env_var", "secret_key_env_var"),
        ("llm_session_token_env_var", "session_token_env_var"),
        ("llm_endpoint_env_var", "endpoint_env_var"),
        ("llm_api_version", "api_version"),
    ]:
        v = data.get(src)
        if v:
            llm_default[dst] = v

    llm_access_block = {"default": llm_default}
    providers_in = data.get("llm_providers", []) or []
    extra_providers = {}
    for p in providers_in:
        alias = (p.get("name") or "").strip()
        if not alias:
            continue
        prov_entry = {
            "provider": p.get("provider", "anthropic"),
            "model": p.get("model") or "",
        }
        if p.get("api_key_env_var"):
            prov_entry["api_key_env_var"] = p["api_key_env_var"]
        if p.get("base_url"):
            prov_entry["base_url"] = p["base_url"]
        mt = p.get("max_tokens")
        if mt:
            prov_entry["max_tokens"] = int(mt)
        extra_providers[alias] = prov_entry
    if extra_providers:
        llm_access_block["providers"] = extra_providers

    config = {
        "agent": agent_block,
        "llm_access": llm_access_block,
        "storage": {"base_path": data.get("storage_path", "./cortex_storage")},
    }

    # Storage advanced
    storage_cfg = config["storage"]
    _only_if(storage_cfg, "large_file_threshold_mb", data.get("storage_large_file_threshold_mb"), 5)
    _only_if(storage_cfg, "result_envelope_max_kb", data.get("storage_result_envelope_max_kb"), 64)
    _only_if(storage_cfg, "session_quota_mb", data.get("storage_session_quota_mb"), 500)
    _only_if(storage_cfg, "health_warning_free_gb", data.get("storage_health_warning_free_gb"), 10.0)
    _only_if(storage_cfg, "health_critical_free_gb", data.get("storage_health_critical_free_gb"), 2.0)
    _only_if(storage_cfg, "atomic_cleanup", data.get("storage_atomic_cleanup"), True)

    # Storage backend
    storage_backend = data.get("storage_backend", "memory")
    if storage_backend == "sqlite":
        wal_mode_in = data.get("sqlite_wal_mode")
        sq = {
            "enabled": True,
            "path": data.get("sqlite_path", "./cortex_storage/cortex.db"),
            "wal_mode": True if wal_mode_in is None else bool(wal_mode_in),
        }
        _only_if(sq, "connection_timeout_seconds", data.get("sqlite_connection_timeout_seconds"), 5)
        _only_if(sq, "ttl_session_data_seconds", data.get("sqlite_ttl_session_data_seconds"), 3600)
        _only_if(sq, "ttl_session_index_seconds", data.get("sqlite_ttl_session_index_seconds"), 86400)
        config["sqlite"] = sq
    elif storage_backend == "redis":
        rd = {
            "enabled": True,
            "host": data.get("redis_host", "127.0.0.1"),
            "port": data.get("redis_port", 6379),
        }
        _only_if(rd, "db", data.get("redis_db"), 1)
        _only_if(rd, "username", data.get("redis_username"), "")
        _only_if(rd, "password_env_var", data.get("redis_password_env_var"), "")
        _only_if(rd, "tls_enabled", data.get("redis_tls_enabled"), False)
        _only_if(rd, "tls_verify_peer", data.get("redis_tls_verify_peer"), True)
        _only_if(rd, "tls_cert_file", data.get("redis_tls_cert_file"), "")
        _only_if(rd, "tls_key_file", data.get("redis_tls_key_file"), "")
        _only_if(rd, "tls_ca_cert_file", data.get("redis_tls_ca_cert_file"), "")
        _only_if(rd, "pool_max_connections", data.get("redis_pool_max_connections"), 20)
        _only_if(rd, "pool_min_idle", data.get("redis_pool_min_idle"), 5)
        _only_if(rd, "connection_timeout_ms", data.get("redis_connection_timeout_ms"), 2000)
        _only_if(rd, "socket_timeout_ms", data.get("redis_socket_timeout_ms"), 1000)
        _only_if(rd, "key_prefix", data.get("redis_key_prefix"), "cortex")
        _only_if(rd, "ttl_session_data_seconds", data.get("redis_ttl_session_data_seconds"), 3600)
        _only_if(rd, "ttl_session_index_seconds", data.get("redis_ttl_session_index_seconds"), 86400)
        _only_if(rd, "ttl_pubsub_seconds", data.get("redis_ttl_pubsub_seconds"), 300)
        config["redis"] = rd

    # File input
    file_input_cfg = {}
    _only_if(file_input_cfg, "max_size_mb", data.get("file_input_max_size_mb"), 50)
    mimes = _parse_lines(data.get("file_input_allowed_mime_types"))
    default_mimes = [
        "text/plain", "text/markdown", "text/csv", "text/html",
        "application/json", "application/xml", "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/png", "image/jpeg", "image/gif", "image/webp",
        "audio/mpeg", "audio/wav",
    ]
    if mimes and mimes != default_mimes:
        file_input_cfg["allowed_mime_types"] = mimes
    if file_input_cfg:
        config["file_input"] = file_input_cfg

    # Chat UI
    if data.get("ui_enabled"):
        ui_cfg = {"enabled": True}
        _only_if(ui_cfg, "host", data.get("ui_host"), "0.0.0.0")
        _only_if(ui_cfg, "port", data.get("ui_port"), 8090)
        _only_if(ui_cfg, "title", data.get("ui_title"), "Cortex Agent")
        auth_cfg = {}
        mode = data.get("ui_auth_mode", "none") or "none"
        if mode != "none":
            auth_cfg["mode"] = mode
            if mode == "token" and data.get("ui_auth_token"):
                auth_cfg["token"] = data["ui_auth_token"]
            elif mode == "basic":
                if data.get("ui_auth_username"):
                    auth_cfg["username"] = data["ui_auth_username"]
                if data.get("ui_auth_password"):
                    auth_cfg["password"] = data["ui_auth_password"]
        if auth_cfg:
            ui_cfg["auth"] = auth_cfg
        config["ui"] = ui_cfg

    # Task types — basic fields plus optional per-task advanced
    task_types_in = data.get("task_types", [])
    task_types_out = []
    for tt in task_types_in:
        entry = {
            "name": tt.get("name"),
            "description": tt.get("description", ""),
            "output_format": tt.get("output_format", "text"),
            "capability_hint": tt.get("capability_hint", "auto"),
            "timeout_seconds": tt.get("timeout_seconds", 40),
        }
        _only_if(entry, "mandatory", tt.get("mandatory"), True)
        _only_if(entry, "complexity", tt.get("complexity"), "adaptive")
        _only_if(entry, "llm_provider", tt.get("llm_provider"), "default")
        if tt.get("handler"):
            entry["handler"] = tt["handler"]
        deps = _parse_csv(tt.get("depends_on"))
        if deps:
            entry["depends_on"] = deps
        srv = _parse_csv(tt.get("tool_servers_list"))
        if srv:
            entry["tool_servers"] = srv
        retry_cfg = {}
        _only_if(retry_cfg, "max_attempts", tt.get("retry_max_attempts"), 2)
        _only_if(retry_cfg, "backoff_initial_ms", tt.get("retry_backoff_initial_ms"), 500)
        if retry_cfg:
            entry["retry"] = retry_cfg
        output_cfg = {}
        _only_if(output_cfg, "max_size_mb", tt.get("output_max_size_mb"), 100)
        _only_if(output_cfg, "content_summary_tokens", tt.get("output_content_summary_tokens"), 400)
        if output_cfg:
            entry["output"] = output_cfg
        if tt.get("validation_notes"):
            entry["validation_notes"] = tt["validation_notes"]
        if tt.get("human_in_loop"):
            entry["human_in_loop"] = True
        schema_text = tt.get("output_schema_json", "").strip() if isinstance(tt.get("output_schema_json"), str) else ""
        if schema_text:
            try:
                entry["output_schema"] = _json.loads(schema_text)
            except Exception:
                pass  # invalid JSON — silently skip so save still succeeds
        if tt.get("blueprint") and str(tt["blueprint"]).strip():
            entry["blueprint"] = tt["blueprint"].strip()
        task_types_out.append(entry)
    if task_types_out:
        config["task_types"] = task_types_out

    # Blueprint feature
    if data.get("blueprint_enabled"):
        bp_block = {"enabled": True}
        mode = data.get("blueprint_storage_mode", "filesystem")
        if mode != "filesystem":
            bp_block["storage_mode"] = mode
        if data.get("blueprint_dir"):
            bp_block["dir"] = data["blueprint_dir"]
        if data.get("blueprint_auto_update") is False:
            bp_block["auto_update"] = False
        _only_if(bp_block, "inject_max_chars", data.get("blueprint_inject_max_chars"), 4000)
        _only_if(bp_block, "staleness_warning_days", data.get("blueprint_staleness_warning_days"), 90)
        config["blueprint"] = bp_block

    # Tool servers — dict keyed by name, with optional per-server advanced
    tool_servers_in = data.get("tool_servers", {})
    if tool_servers_in:
        config["tool_servers"] = tool_servers_in

    # Validation
    if data.get("validation_enabled"):
        validation_cfg = {
            "threshold": data.get("validation_threshold", 0.75),
        }
        wave_gate_provider = data.get("wave_gate_llm_provider")
        if wave_gate_provider and wave_gate_provider != "default":
            validation_cfg["wave_gate_llm_provider"] = wave_gate_provider
        _only_if(validation_cfg, "critical_threshold", data.get("validation_critical_threshold"), 0.40)
        _only_if(validation_cfg, "timeout_seconds", data.get("validation_timeout_seconds"), 15)
        _only_if(validation_cfg, "weights_intent_match", data.get("validation_weights_intent_match"), 0.50)
        _only_if(validation_cfg, "weights_completeness", data.get("validation_weights_completeness"), 0.30)
        _only_if(validation_cfg, "weights_coherence", data.get("validation_weights_coherence"), 0.20)
        _only_if(validation_cfg, "expose_report_to_user", data.get("validation_expose_report_to_user"), True)
        _only_if(validation_cfg, "expose_score_to_user", data.get("validation_expose_score_to_user"), False)
        config["validation"] = validation_cfg

    # History
    if data.get("history_enabled"):
        hist_cfg = {
            "enabled": True,
            "retention_days": data.get("history_retention_days", 90),
        }
        _only_if(hist_cfg, "max_sessions_in_context", data.get("history_max_sessions_in_context"), 5)
        persist = _parse_csv(data.get("history_persist_task_outputs"))
        if persist:
            hist_cfg["persist_task_outputs"] = persist
        _only_if(hist_cfg, "search_enabled", data.get("history_search_enabled"), True)
        _only_if(hist_cfg, "encryption_enabled", data.get("history_encryption_enabled"), False)
        _only_if(hist_cfg, "encryption_key_env_var", data.get("history_encryption_key_env_var"), "")
        config["history"] = hist_cfg

    # Learning
    if data.get("learning_enabled"):
        learn_cfg = {
            "consent_enabled": True,
            "auto_apply_delta": data.get("auto_apply_delta", False),
        }
        _only_if(learn_cfg, "auto_apply_min_confidence", data.get("learning_auto_apply_min_confidence"), "high")
        _only_if(learn_cfg, "auto_apply_min_confirmations", data.get("learning_auto_apply_min_confirmations"), 3)
        _only_if(learn_cfg, "notify_on_apply", data.get("learning_notify_on_apply"), True)
        config["learning"] = learn_cfg

    # Security
    sec_cfg = {}
    _only_if(sec_cfg, "max_input_tokens", data.get("security_max_input_tokens"), 4000)
    scrub = _parse_lines(data.get("security_secret_scrub_patterns"))
    default_scrub = [
        r"Bearer \S+",
        r"api[_-]?key[_-]?=\S+",
        r"password[_-]?=\S+",
        r"token[_-]?=\S+",
    ]
    if scrub and scrub != default_scrub:
        sec_cfg["secret_scrub_patterns"] = scrub
    if sec_cfg:
        config["security"] = sec_cfg

    # Startup
    start_cfg = {}
    _only_if(start_cfg, "require_all_servers", data.get("startup_require_all_servers"), False)
    _only_if(start_cfg, "discovery_timeout_seconds", data.get("startup_discovery_timeout_seconds"), 15)
    _only_if(start_cfg, "log_discovered_tools", data.get("startup_log_discovered_tools"), True)
    _only_if(start_cfg, "verify_auth", data.get("startup_verify_auth"), True)
    _only_if(start_cfg, "eager_discovery", data.get("startup_eager_discovery"), False)
    if data.get("startup_capability_registry_path"):
        start_cfg["capability_registry_path"] = data["startup_capability_registry_path"]
    _only_if(start_cfg, "background_discovery_concurrency", data.get("startup_background_discovery_concurrency"), 10)
    if start_cfg:
        config["startup"] = start_cfg

    # User config
    user_cfg = {}
    _only_if(user_cfg, "allow_user_cortex_mcp", data.get("user_allow_user_cortex_mcp"), True)
    _only_if(user_cfg, "allow_user_tool_servers", data.get("user_allow_user_tool_servers"), False)
    if user_cfg:
        config["user_config"] = user_cfg

    # Code sandbox
    if data.get("code_sandbox_enabled"):
        sb_cfg = {"enabled": True}
        _only_if(sb_cfg, "timeout_seconds", data.get("code_sandbox_timeout_seconds"), 60)
        _only_if(sb_cfg, "allow_network", data.get("code_sandbox_allow_network"), False)
        _only_if(sb_cfg, "ask_persist_consent", data.get("code_sandbox_ask_persist_consent"), True)
        _only_if(sb_cfg, "auto_add_to_yaml", data.get("code_sandbox_auto_add_to_yaml"), False)
        config["code_sandbox"] = sb_cfg

    # Ant Colony
    if data.get("ant_colony_enabled"):
        ant_cfg = {"enabled": True}
        _only_if(ant_cfg, "base_port", data.get("ant_colony_base_port"), 8100)
        _only_if(ant_cfg, "max_ants", data.get("ant_colony_max_ants"), 20)
        _only_if(ant_cfg, "auto_restart", data.get("ant_colony_auto_restart"), True)
        _only_if(ant_cfg, "auto_hatch_on_gap", data.get("ant_colony_auto_hatch_on_gap"), False)
        _only_if(ant_cfg, "llm_provider", data.get("ant_colony_llm_provider"), "default")
        _only_if(ant_cfg, "llm_model", data.get("ant_colony_llm_model"), "claude-haiku-4-5-20251001")
        _only_if(ant_cfg, "api_key_env_var", data.get("ant_colony_api_key_env_var"), "ANTHROPIC_API_KEY")
        config["ant_colony"] = ant_cfg

    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def _load_existing_config(config_path: str) -> dict:
    """Load existing cortex.yaml and return structured data for the wizard."""
    import yaml

    if not os.path.exists(config_path):
        return {"exists": False, "data": {}, "locked_fields": []}

    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        return {"exists": False, "data": {}, "error": str(e), "locked_fields": []}

    import json as _json

    agent = raw.get("agent", {}) or {}
    llm = (raw.get("llm_access", {}) or {}).get("default", {}) or {}
    storage = raw.get("storage", {}) or {}

    time_cfg = agent.get("time", {}) or {}
    concurrency_cfg = agent.get("concurrency", {}) or {}
    perf_cfg = agent.get("performance", {}) or {}
    streaming_cfg = agent.get("streaming", {}) or {}
    clarification_cfg = agent.get("clarification", {}) or {}
    scout_cfg = agent.get("capability_scout", {}) or {}
    scout_ext = (scout_cfg.get("external_discovery", {}) or {})

    validation_raw = raw.get("validation", {}) or {}
    history_raw = raw.get("history", {}) or {}
    learning_raw = raw.get("learning", {}) or {}
    security_raw = raw.get("security", {}) or {}
    startup_raw = raw.get("startup", {}) or {}
    user_raw = raw.get("user_config", {}) or {}
    sandbox_raw = raw.get("code_sandbox", {}) or {}
    file_input_raw = raw.get("file_input", {}) or {}
    ui_raw = raw.get("ui", {}) or {}
    ui_auth_raw = ui_raw.get("auth", {}) or {}
    ant_colony_raw = raw.get("ant_colony", {}) or {}

    def _kv_to_text(d):
        if not isinstance(d, dict):
            return ""
        return "\n".join(f"{k}: {v}" for k, v in d.items())

    def _list_to_lines(lst):
        if not isinstance(lst, list):
            return ""
        return "\n".join(str(x) for x in lst)

    def _list_to_csv(lst):
        if not isinstance(lst, list):
            return ""
        return ", ".join(str(x) for x in lst)

    data = {
        # ── Agent ──
        "agent_name": agent.get("name", ""),
        "agent_description": agent.get("description", ""),
        "agent_synthesis_guidance": agent.get("synthesis_guidance", ""),
        "max_wait_seconds": time_cfg.get("default_max_wait_seconds", 120),
        "default_task_timeout": time_cfg.get("default_task_timeout_seconds", 40),
        "agent_streaming_decomposition": perf_cfg.get("streaming_decomposition", False),
        "max_concurrent_sessions": concurrency_cfg.get("max_concurrent_sessions", 50),
        "max_sessions_per_user": concurrency_cfg.get("max_concurrent_sessions_per_user", 3),
        "max_parallel_tasks": concurrency_cfg.get("max_parallel_tasks", 5),
        "max_tasks_per_session": concurrency_cfg.get("max_tasks_per_session", 20),
        "agent_max_mcp_agent_llm_calls": concurrency_cfg.get("max_mcp_agent_llm_calls", 10),
        "agent_streaming_status_updates": streaming_cfg.get("status_updates", False),
        "agent_streaming_include_task_detail": streaming_cfg.get("include_task_detail", True),
        "agent_streaming_mcp_agent_updates": streaming_cfg.get("mcp_agent_updates", False),
        "agent_streaming_reconnect_buffer_size": streaming_cfg.get("reconnect_buffer_size", 50),
        "agent_streaming_min_delivery_interval_ms": streaming_cfg.get("min_delivery_interval_ms", 200),
        "agent_clarification_enabled": clarification_cfg.get("enabled", False),
        "scout_enabled": scout_cfg.get("enabled", True),
        "scout_max_capabilities": scout_cfg.get("max_capabilities", 30),
        "scout_timeout_seconds": scout_cfg.get("timeout_seconds", 10),
        "scout_ext_enabled": scout_ext.get("enabled", True),
        "scout_ext_auto_discovery_file": scout_ext.get("auto_discovery_file", "cortex_auto_mcps.yaml"),
        "scout_ext_registry_sources": _list_to_lines(scout_ext.get("registry_sources", [])),
        "scout_ext_max_new_per_session": scout_ext.get("max_new_per_session", 5),
        "scout_ext_max_stale_days": scout_ext.get("max_stale_days", 30),
        "scout_ext_search_timeout_s": scout_ext.get("search_timeout_s", 10.0),
        # ── LLM ──
        "provider": llm.get("provider", "anthropic"),
        "model": llm.get("model", ""),
        "api_key_env_var": llm.get("api_key_env_var", ""),
        "base_url": llm.get("base_url", ""),
        "max_tokens": llm.get("max_tokens", 4096),
        "temperature": llm.get("temperature", 1.0),
        "llm_headers": _kv_to_text(llm.get("headers", {})),
        "llm_region_env_var": llm.get("region_env_var", ""),
        "llm_access_key_env_var": llm.get("access_key_env_var", ""),
        "llm_secret_key_env_var": llm.get("secret_key_env_var", ""),
        "llm_session_token_env_var": llm.get("session_token_env_var", ""),
        "llm_endpoint_env_var": llm.get("endpoint_env_var", ""),
        "llm_api_version": llm.get("api_version", ""),
        # ── Storage ──
        "storage_path": storage.get("base_path", "./cortex_storage"),
        "storage_large_file_threshold_mb": storage.get("large_file_threshold_mb", 5),
        "storage_result_envelope_max_kb": storage.get("result_envelope_max_kb", 64),
        "storage_session_quota_mb": storage.get("session_quota_mb", 500),
        "storage_health_warning_free_gb": storage.get("health_warning_free_gb", 10.0),
        "storage_health_critical_free_gb": storage.get("health_critical_free_gb", 2.0),
        "storage_atomic_cleanup": storage.get("atomic_cleanup", True),
        # ── File input ──
        "file_input_max_size_mb": file_input_raw.get("max_size_mb", 50),
        "file_input_allowed_mime_types": _list_to_lines(file_input_raw.get("allowed_mime_types", [])),
        # ── Validation ──
        "validation_enabled": "threshold" in validation_raw,
        "wave_gate_llm_provider": validation_raw.get("wave_gate_llm_provider", "default"),
        "validation_threshold": validation_raw.get("threshold", 0.75),
        "validation_critical_threshold": validation_raw.get("critical_threshold", 0.40),
        "validation_timeout_seconds": validation_raw.get("timeout_seconds", 15),
        "validation_weights_intent_match": validation_raw.get("weights_intent_match", 0.50),
        "validation_weights_completeness": validation_raw.get("weights_completeness", 0.30),
        "validation_weights_coherence": validation_raw.get("weights_coherence", 0.20),
        "validation_expose_report_to_user": validation_raw.get("expose_report_to_user", True),
        "validation_expose_score_to_user": validation_raw.get("expose_score_to_user", False),
        # ── History ──
        "history_enabled": history_raw.get("enabled", False),
        "history_retention_days": history_raw.get("retention_days", 90),
        "history_max_sessions_in_context": history_raw.get("max_sessions_in_context", 5),
        "history_persist_task_outputs": _list_to_csv(history_raw.get("persist_task_outputs", [])),
        "history_search_enabled": history_raw.get("search_enabled", True),
        "history_encryption_enabled": history_raw.get("encryption_enabled", False),
        "history_encryption_key_env_var": history_raw.get("encryption_key_env_var", ""),
        # ── Learning ──
        "learning_enabled": learning_raw.get("consent_enabled", False),
        "auto_apply_delta": learning_raw.get("auto_apply_delta", False),
        "learning_auto_apply_min_confidence": learning_raw.get("auto_apply_min_confidence", "high"),
        "learning_auto_apply_min_confirmations": learning_raw.get("auto_apply_min_confirmations", 3),
        "learning_notify_on_apply": learning_raw.get("notify_on_apply", True),
        # ── Security ──
        "security_max_input_tokens": security_raw.get("max_input_tokens", 4000),
        "security_secret_scrub_patterns": _list_to_lines(security_raw.get("secret_scrub_patterns", [])),
        # ── Startup ──
        "startup_require_all_servers": startup_raw.get("require_all_servers", False),
        "startup_discovery_timeout_seconds": startup_raw.get("discovery_timeout_seconds", 15),
        "startup_log_discovered_tools": startup_raw.get("log_discovered_tools", True),
        "startup_verify_auth": startup_raw.get("verify_auth", True),
        "startup_eager_discovery": startup_raw.get("eager_discovery", False),
        "startup_capability_registry_path": startup_raw.get("capability_registry_path", "") or "",
        "startup_background_discovery_concurrency": startup_raw.get("background_discovery_concurrency", 10),
        # ── User config ──
        "user_allow_user_cortex_mcp": user_raw.get("allow_user_cortex_mcp", True),
        "user_allow_user_tool_servers": user_raw.get("allow_user_tool_servers", False),
        # ── Code sandbox ──
        "code_sandbox_enabled": sandbox_raw.get("enabled", False),
        "code_sandbox_timeout_seconds": sandbox_raw.get("timeout_seconds", 60),
        "code_sandbox_allow_network": sandbox_raw.get("allow_network", False),
        "code_sandbox_ask_persist_consent": sandbox_raw.get("ask_persist_consent", True),
        "code_sandbox_auto_add_to_yaml": sandbox_raw.get("auto_add_to_yaml", False),
        # ── Ant Colony ──
        "ant_colony_enabled": ant_colony_raw.get("enabled", False),
        "ant_colony_base_port": ant_colony_raw.get("base_port", 8100),
        "ant_colony_max_ants": ant_colony_raw.get("max_ants", 20),
        "ant_colony_auto_restart": ant_colony_raw.get("auto_restart", True),
        "ant_colony_auto_hatch_on_gap": ant_colony_raw.get("auto_hatch_on_gap", False),
        "ant_colony_llm_provider": ant_colony_raw.get("llm_provider", "default"),
        "ant_colony_llm_model": ant_colony_raw.get("llm_model", "claude-haiku-4-5-20251001"),
        "ant_colony_api_key_env_var": ant_colony_raw.get("api_key_env_var", "ANTHROPIC_API_KEY"),
        # ── Chat UI ──
        "ui_enabled": ui_raw.get("enabled", False),
        "ui_host": ui_raw.get("host", "0.0.0.0"),
        "ui_port": ui_raw.get("port", 8090),
        "ui_title": ui_raw.get("title", "Cortex Agent"),
        "ui_auth_mode": ui_auth_raw.get("mode", "none"),
        "ui_auth_token": ui_auth_raw.get("token", "") or "",
        "ui_auth_username": ui_auth_raw.get("username", "") or "",
        "ui_auth_password": ui_auth_raw.get("password", "") or "",
    }

    # Storage backend detection
    if raw.get("redis", {}).get("enabled"):
        data["storage_backend"] = "redis"
        data["redis_host"] = raw["redis"].get("host", "127.0.0.1")
        data["redis_port"] = raw["redis"].get("port", 6379)
    elif raw.get("sqlite", {}).get("enabled"):
        data["storage_backend"] = "sqlite"
        data["sqlite_path"] = raw["sqlite"].get("path", "./cortex_storage/cortex.db")
        data["sqlite_wal_mode"] = raw["sqlite"].get("wal_mode", True)
    else:
        data["storage_backend"] = "memory"

    # Named LLM providers
    providers_raw = (raw.get("llm_access", {}) or {}).get("providers", {}) or {}
    llm_providers_out = []
    for alias, pcfg in providers_raw.items():
        if not isinstance(pcfg, dict):
            continue
        llm_providers_out.append({
            "name": alias,
            "provider": pcfg.get("provider", "anthropic"),
            "model": pcfg.get("model", ""),
            "api_key_env_var": pcfg.get("api_key_env_var", ""),
            "base_url": pcfg.get("base_url", ""),
            "max_tokens": pcfg.get("max_tokens", 4096),
        })
    data["llm_providers"] = llm_providers_out

    # Tool servers
    tool_servers = []
    for name, cfg in raw.get("tool_servers", {}).items():
        auth = cfg.get("auth", {}) or {}
        conn = cfg.get("connection", {}) or {}
        tls = cfg.get("tls", {}) or {}
        pool = cfg.get("pool", {}) or {}
        disc = cfg.get("discovery", {}) or {}
        hc = cfg.get("health_check", {}) or {}
        env = cfg.get("env", {}) or {}
        headers = cfg.get("headers", {}) or {}
        ts = {
            "name": name,
            "transport": cfg.get("transport", "sse"),
            "url": cfg.get("url", ""),
            "command": cfg.get("command", ""),
            "args": ", ".join(cfg.get("args", [])) if isinstance(cfg.get("args"), list) else "",
            "description": cfg.get("description", ""),
            "working_dir": cfg.get("working_dir", ""),
            "startup_timeout_seconds": cfg.get("startup_timeout_seconds", 10),
            "conn_timeout": conn.get("timeout_seconds", 10),
            "conn_read_timeout": conn.get("read_timeout_seconds", 60),
            "conn_max_retries": conn.get("max_retries", 3),
            "conn_retry_backoff_ms": conn.get("retry_backoff_ms", 500),
            "auth_type": auth.get("type", "none"),
            "auth_token_env_var": auth.get("token_env_var", ""),
            "auth_header": auth.get("header", ""),
            "auth_key_env_var": auth.get("key_env_var", ""),
            "auth_username_env_var": auth.get("username_env_var", ""),
            "auth_password_env_var": auth.get("password_env_var", ""),
            "auth_token_url": auth.get("token_url", ""),
            "auth_client_id_env_var": auth.get("client_id_env_var", ""),
            "auth_client_secret_env_var": auth.get("client_secret_env_var", ""),
            "auth_scope": auth.get("scope", ""),
            "tls_enabled": tls.get("enabled", False),
            "tls_verify_cert": tls.get("verify_cert", True),
            "tls_ca_cert_file": tls.get("ca_cert_file", ""),
            "tls_client_cert_file": tls.get("client_cert_file", ""),
            "tls_client_key_file": tls.get("client_key_file", ""),
            "pool_min_connections": pool.get("min_connections", 1),
            "pool_max_connections": pool.get("max_connections", 10),
            "discovery_auto": disc.get("auto", True),
            "discovery_capability_hints": ", ".join(disc.get("capability_hints", []) or []),
            "discovery_domain_hints": ", ".join(disc.get("domain_hints", []) or []),
            "env_text": "\n".join(f"{k}={v}" for k, v in env.items()),
            "headers_text": "\n".join(f"{k}: {v}" for k, v in headers.items()),
            "health_enabled": hc.get("enabled", True),
            "health_endpoint": hc.get("endpoint", ""),
            "health_interval_seconds": hc.get("interval_seconds", 30),
            "health_failure_threshold": hc.get("failure_threshold", 3),
            "health_recovery_threshold": hc.get("recovery_threshold", 2),
        }
        tool_servers.append(ts)
    data["tool_servers"] = tool_servers

    # Task types
    task_types = []
    for tt in raw.get("task_types", []):
        retry = tt.get("retry", {}) or {}
        output = tt.get("output", {}) or {}
        schema_obj = tt.get("output_schema")
        schema_text = ""
        if schema_obj is not None:
            try:
                schema_text = _json.dumps(schema_obj, indent=2)
            except Exception:
                schema_text = ""
        task_types.append({
            "name": tt.get("name", ""),
            "description": tt.get("description", ""),
            "output_format": tt.get("output_format", "text"),
            "capability_hint": tt.get("capability_hint", "auto"),
            "timeout_seconds": tt.get("timeout_seconds", 40),
            "blueprint": tt.get("blueprint", ""),
            # Advanced
            "mandatory": tt.get("mandatory", True),
            "complexity": tt.get("complexity", "adaptive"),
            "llm_provider": tt.get("llm_provider", "default"),
            "handler": tt.get("handler", "") or "",
            "depends_on": ", ".join(tt.get("depends_on", []) or []),
            "tool_servers_list": ", ".join(tt.get("tool_servers", []) or []),
            "retry_max_attempts": retry.get("max_attempts", 2),
            "retry_backoff_initial_ms": retry.get("backoff_initial_ms", 500),
            "output_max_size_mb": output.get("max_size_mb", 100),
            "output_content_summary_tokens": output.get("content_summary_tokens", 400),
            "validation_notes": tt.get("validation_notes", "") or "",
            "human_in_loop": tt.get("human_in_loop", False),
            "output_schema_json": schema_text,
        })
    data["task_types"] = task_types

    # Blueprint feature
    bp_raw = raw.get("blueprint", {}) or {}
    data["blueprint_enabled"] = bool(bp_raw.get("enabled", False))
    data["blueprint_storage_mode"] = bp_raw.get("storage_mode", "filesystem")
    data["blueprint_dir"] = bp_raw.get("dir", "")
    data["blueprint_auto_update"] = bp_raw.get("auto_update", True)

    # Determine locked fields — these are dangerous to change after setup
    locked_fields = []
    # Agent name is identity — changing it can break session history references
    if agent.get("name"):
        locked_fields.append("agent_name")
    # Storage backend — switching backends loses existing data
    storage_base = storage.get("base_path", "./cortex_storage")
    if os.path.exists(storage_base) and os.listdir(storage_base):
        locked_fields.append("storage_backend")
        locked_fields.append("storage_path")
    # SQLite path — if the DB file exists, don't change it
    sqlite_path = raw.get("sqlite", {}).get("path", "")
    if sqlite_path and os.path.exists(sqlite_path):
        locked_fields.append("sqlite_path")

    return {"exists": True, "data": data, "locked_fields": locked_fields}


def _resolve_blueprint_path(config_path: str, blueprint_ref: str) -> Optional[Path]:
    """Resolve a blueprint reference to an absolute filesystem path.

    The wizard only handles the ``filesystem`` storage mode. If the user has
    configured ``storage_mode: backend``, the UI returns a hint telling them
    to inspect the blueprint through the running agent instead.
    """
    import yaml as _yaml
    try:
        with open(config_path) as f:
            raw = _yaml.safe_load(f) or {}
    except FileNotFoundError:
        raw = {}
    except Exception:
        return None
    bp_cfg = raw.get("blueprint", {}) or {}
    if bp_cfg.get("storage_mode", "filesystem") != "filesystem":
        return None
    base_path = (raw.get("storage", {}) or {}).get("base_path", "./cortex_storage")
    bp_dir = bp_cfg.get("dir") or str(Path(base_path) / "blueprints")
    ref = blueprint_ref if blueprint_ref.endswith(".md") else f"{blueprint_ref}.md"
    p = Path(ref)
    if not p.is_absolute():
        p = Path(bp_dir) / ref
    return p


def _load_or_preview_blueprint(config_path: str, task_name: str, blueprint_ref: str) -> dict:
    """Return blueprint content if it exists, else a seeded preview the user can save."""
    path = _resolve_blueprint_path(config_path, blueprint_ref)
    if path is None:
        return {
            "exists": False,
            "content": "",
            "error": "Blueprint storage_mode is 'backend' — inspect via the running agent, not the wizard.",
            "path": "",
        }
    if path.exists():
        try:
            return {
                "exists": True,
                "content": path.read_text(encoding="utf-8"),
                "path": str(path),
            }
        except Exception as e:
            return {"exists": False, "content": "", "error": str(e), "path": str(path)}

    # Seeded template so the user can author a blueprint from the wizard
    # before the agent has ever run.
    from cortex.modules.blueprint_store import Blueprint, BlueprintStore
    name = blueprint_ref[:-3] if blueprint_ref.endswith(".md") else blueprint_ref
    if "/" in name:
        name = Path(name).stem
    if "__" not in name:
        name = BlueprintStore.generate_unique_name(task_name, salt=f"{task_name}:{blueprint_ref}")
    seed = Blueprint(name=name, task_name=task_name, version=1)
    return {
        "exists": False,
        "content": seed.to_markdown(),
        "path": str(path),
    }


def _save_blueprint_content(config_path: str, task_name: str, blueprint_ref: str, content: str) -> dict:
    path = _resolve_blueprint_path(config_path, blueprint_ref)
    if path is None:
        return {"saved": False, "error": "Blueprint storage_mode is 'backend' — not editable from the wizard."}
    try:
        # Validate content parses as a Blueprint before writing.
        from cortex.modules.blueprint_store import Blueprint
        Blueprint.from_markdown(content)
    except Exception as e:
        return {"saved": False, "error": f"Invalid blueprint format: {e}"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        return {"saved": True, "path": str(path)}
    except Exception as e:
        return {"saved": False, "error": str(e)}


def _run_publish(mode: str, config_path: str, data: dict) -> dict:
    """Execute the publish command and return status."""
    python = sys.executable
    try:
        if mode == "docker":
            tag = data.get("docker_tag", "cortex-agent:latest")
            result = subprocess.run(
                [python, "-m", "cortex.cli.main", "publish", "docker", "--tag", tag, "--config", config_path],
                capture_output=True, text=True, timeout=60,
            )
            return {
                "success": result.returncode == 0,
                "output": result.stdout + result.stderr,
                "mode": "docker",
                "next_steps": [
                    f"docker build -f Dockerfile.cortex -t {tag} .",
                    f"docker run -p 8080:8080 --env-file .env {tag}",
                ],
            }
        elif mode == "package":
            output_dir = data.get("output_dir", "dist")
            result = subprocess.run(
                [python, "-m", "cortex.cli.main", "publish", "package", "--output-dir", output_dir],
                capture_output=True, text=True, timeout=120,
            )
            return {
                "success": result.returncode == 0,
                "output": result.stdout + result.stderr,
                "mode": "package",
                "next_steps": [
                    f"pip install {output_dir}/*.whl",
                    "cortex dev --config cortex.yaml",
                ],
            }
        elif mode == "mcp":
            port = data.get("mcp_port", 8080)
            result = subprocess.run(
                [python, "-m", "cortex.cli.main", "publish", "mcp", "--config", config_path, "--port", str(port)],
                capture_output=True, text=True, timeout=60,
            )
            return {
                "success": result.returncode == 0,
                "output": result.stdout + result.stderr,
                "mode": "mcp",
                "next_steps": [
                    f"cortex publish mcp --port {port}",
                    f"Add to another agent's cortex.yaml:\n  tool_servers:\n    my_agent:\n      url: http://localhost:{port}/sse\n      transport: sse",
                ],
            }
        elif mode == "ui":
            host = data.get("ui_host", "0.0.0.0")
            port = data.get("ui_port", 8090)
            # We don't start the server from the wizard (it would block the
            # wizard process). Just surface the command the user should run.
            return {
                "success": True,
                "output": (
                    "Chat UI is configured. Start it with:\n"
                    f"  cortex publish ui --config {config_path}\n"
                    f"Then open http://{host}:{port} in your browser."
                ),
                "mode": "ui",
                "next_steps": [
                    f"cortex publish ui --config {config_path}",
                    f"Open http://{host}:{port}",
                    "For Docker: cortex publish docker --with-ui",
                ],
            }
        else:
            return {"success": False, "output": f"Unknown publish mode: {mode}", "mode": mode, "next_steps": []}
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "Publish command timed out.", "mode": mode, "next_steps": []}
    except Exception as e:
        return {"success": False, "output": str(e), "mode": mode, "next_steps": []}
