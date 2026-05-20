"""Cortex Config Studio — aiohttp server serving the config editor UI."""
import logging
import os
import re
from pathlib import Path
from typing import List

import yaml
from aiohttp import web

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

# ── YAML helpers ──────────────────────────────────────────────────────────────

def _load_raw(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_atomic(path: str, data: dict) -> None:
    tmp = path + ".config_ui_tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)


# ── Section extraction / merging ──────────────────────────────────────────────

def _extract_section(raw: dict, section_id: str) -> dict:
    if section_id == "core_agent":
        agent = dict(raw.get("agent", {}))
        agent.pop("intent_gate", None)
        agent.pop("capability_scout", None)
        return {"agent": agent}
    elif section_id == "intent_gate":
        agent = raw.get("agent", {})
        return {
            "agent": {
                "intent_gate": dict(agent.get("intent_gate", {})),
                "capability_scout": {
                    k: v for k, v in agent.get("capability_scout", {}).items()
                    if k != "external_discovery"
                },
                "capability_scout_external_discovery": (
                    agent.get("capability_scout", {}).get("external_discovery", {})
                ),
            }
        }
    elif section_id == "task_types":
        return {"task_types": list(raw.get("task_types", []))}
    elif section_id == "tool_servers":
        return {"tool_servers": dict(raw.get("tool_servers", {}))}
    elif section_id == "llm_providers":
        return {"llm_access": dict(raw.get("llm_access", {}))}
    elif section_id == "blueprints":
        return {"blueprint": dict(raw.get("blueprint", {}))}
    elif section_id == "code_registry":
        return {"code_sandbox": dict(raw.get("code_sandbox", {}))}
    elif section_id == "app_control":
        return {"app_control": dict(raw.get("app_control", {}))}
    elif section_id == "playwright_mcp":
        return {"playwright_mcp": dict(raw.get("playwright_mcp", {}))}
    elif section_id == "auto_mcps":
        agent = raw.get("agent", {})
        ext = agent.get("capability_scout", {}).get("external_discovery", {})
        return {"external_discovery": dict(ext)}
    elif section_id == "ant_colony":
        return {"ant_colony": dict(raw.get("ant_colony", {}))}
    elif section_id == "tool_forge":
        return {"tool_forge": dict(raw.get("tool_forge", {}))}
    elif section_id == "adaptive_model_routing":
        return {"adaptive_model_routing": dict(raw.get("adaptive_model_routing", {}))}
    elif section_id == "system":
        keys = ["learning", "validation", "history", "storage", "sqlite",
                "redis", "security", "startup", "user_config", "ui"]
        return {k: dict(raw.get(k, {})) for k in keys}
    return {}


def _merge_section(raw: dict, section_id: str, data: dict) -> dict:
    raw = dict(raw)
    if section_id == "core_agent":
        agent_in = data.get("agent", {})
        existing = dict(raw.get("agent", {}))
        preserved = {k: existing[k] for k in ("intent_gate", "capability_scout") if k in existing}
        existing.update(agent_in)
        existing.update(preserved)
        raw["agent"] = existing
    elif section_id == "intent_gate":
        agent_in = data.get("agent", {})
        existing = dict(raw.get("agent", {}))
        if "intent_gate" in agent_in:
            existing["intent_gate"] = agent_in["intent_gate"]
        if "capability_scout" in agent_in:
            cs = dict(existing.get("capability_scout", {}))
            ext = cs.get("external_discovery")
            cs.update(agent_in["capability_scout"])
            if ext is not None and "external_discovery" not in agent_in["capability_scout"]:
                cs["external_discovery"] = ext
            existing["capability_scout"] = cs
        if "capability_scout_external_discovery" in agent_in:
            cs = dict(existing.get("capability_scout", {}))
            cs["external_discovery"] = agent_in["capability_scout_external_discovery"]
            existing["capability_scout"] = cs
        raw["agent"] = existing
    elif section_id == "task_types":
        raw["task_types"] = data.get("task_types", [])
    elif section_id == "tool_servers":
        raw["tool_servers"] = data.get("tool_servers", {})
    elif section_id == "llm_providers":
        raw["llm_access"] = data.get("llm_access", {})
    elif section_id == "blueprints":
        raw["blueprint"] = data.get("blueprint", {})
    elif section_id == "code_registry":
        raw["code_sandbox"] = data.get("code_sandbox", {})
    elif section_id == "app_control":
        raw["app_control"] = data.get("app_control", {})
    elif section_id == "playwright_mcp":
        raw["playwright_mcp"] = data.get("playwright_mcp", {})
    elif section_id == "auto_mcps":
        agent = dict(raw.get("agent", {}))
        cs = dict(agent.get("capability_scout", {}))
        cs["external_discovery"] = data.get("external_discovery", {})
        agent["capability_scout"] = cs
        raw["agent"] = agent
    elif section_id == "ant_colony":
        raw["ant_colony"] = data.get("ant_colony", {})
    elif section_id == "tool_forge":
        raw["tool_forge"] = data.get("tool_forge", {})
    elif section_id == "adaptive_model_routing":
        raw["adaptive_model_routing"] = data.get("adaptive_model_routing", {})
    elif section_id == "system":
        for k in ["learning", "validation", "history", "storage", "sqlite",
                  "redis", "security", "startup", "user_config", "ui"]:
            if k in data:
                raw[k] = data[k]
    return raw


# ── Runtime file readers ───────────────────────────────────────────────────────

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def _read_blueprints(storage_base: Path) -> List[dict]:
    bp_dir = storage_base / "blueprints"
    if not bp_dir.exists():
        return []
    results = []
    for md_file in sorted(bp_dir.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
            fm: dict = {}
            body = content
            m = _FM_RE.match(content)
            if m:
                try:
                    fm = yaml.safe_load(m.group(1)) or {}
                except Exception:
                    pass
                body = m.group(2).strip()
            lessons = len([ln for ln in body.split("\n") if ln.strip().startswith("- [v")])
            results.append({
                "file": md_file.name,
                "name": fm.get("name", md_file.stem),
                "task_name": fm.get("task_name", ""),
                "version": fm.get("version", 1),
                "updated_at": fm.get("updated_at", ""),
                "last_successful_run_at": fm.get("last_successful_run_at", ""),
                "lessons_count": lessons,
                "body": body,
                "frontmatter": fm,
            })
        except Exception as exc:
            results.append({"file": md_file.name, "error": str(exc)})
    return results


def _read_agent_tools(storage_base: Path) -> dict:
    index_path = storage_base / "agent_tools" / "index.yaml"
    raw = _load_raw(str(index_path))
    scripts: List[dict] = []
    for _key, rec in (raw.items() if isinstance(raw, dict) else {}.items()):
        entry = dict(rec) if isinstance(rec, dict) else {}
        sp_str = entry.get("script_path", "")
        sp = Path(sp_str) if Path(sp_str).is_absolute() else storage_base / "agent_tools" / sp_str
        if sp.exists():
            try:
                entry["source"] = sp.read_text(encoding="utf-8")
            except Exception:
                entry["source"] = ""
        else:
            entry["source"] = ""
        scripts.append(entry)
    return {"scripts": scripts}


def _read_ants(storage_base: Path) -> dict:
    data = _load_raw(str(storage_base / "ants.yaml"))
    ants = data.get("ants", [])
    if not ants and isinstance(data, list):
        ants = data
    return {"ants": ants}


def _read_auto_mcps(storage_base: Path) -> dict:
    data = _load_raw(str(storage_base / "cortex_auto_mcps.yaml"))
    return {
        "version": data.get("version", 1),
        "records": data.get("records", []),
        "pending_auth": data.get("pending_auth", []),
    }


def _read_delta_queue(storage_base: Path) -> dict:
    data = _load_raw(str(storage_base / "cortex_delta" / "pending.yaml"))
    proposals = data.get("proposals", [])
    if not proposals and isinstance(data, list):
        proposals = data
    return {"proposals": proposals}


# ── Middleware & handlers ──────────────────────────────────────────────────────

@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=200)
    else:
        try:
            resp = await handler(request)
        except Exception as exc:
            logger.exception("Unhandled error in %s %s", request.method, request.path)
            resp = web.json_response({"ok": False, "error": str(exc)}, status=500)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def handle_index(request: web.Request) -> web.Response:
    index = STATIC_DIR / "index.html"
    return web.FileResponse(index)


async def handle_status(request: web.Request) -> web.Response:
    app = request.app
    config_path = app["config_path"]
    storage_base = app["storage_base"]
    raw = _load_raw(config_path)
    agent_name = raw.get("agent", {}).get("name", "")
    return web.json_response({
        "ok": True,
        "config_path": config_path,
        "storage_base": storage_base,
        "agent_name": agent_name,
    })


async def handle_get_section(request: web.Request) -> web.Response:
    sid = request.match_info["section_id"]
    config_path = request.app["config_path"]
    raw = _load_raw(config_path)
    data = _extract_section(raw, sid)
    return web.json_response({"ok": True, "data": data, "file_path": config_path})


async def handle_post_section(request: web.Request) -> web.Response:
    sid = request.match_info["section_id"]
    config_path = request.app["config_path"]
    body = await request.json()
    new_data = body.get("data", {})
    raw = _load_raw(config_path)
    merged = _merge_section(raw, sid, new_data)
    _write_atomic(config_path, merged)
    return web.json_response({"ok": True})


async def handle_get_runtime(request: web.Request) -> web.Response:
    sid = request.match_info["section_id"]
    storage_base = Path(request.app["storage_base"])
    if sid == "blueprints":
        data = _read_blueprints(storage_base)
    elif sid == "code_registry":
        data = _read_agent_tools(storage_base)
    elif sid == "ant_colony":
        data = _read_ants(storage_base)
    elif sid == "auto_mcps":
        data = _read_auto_mcps(storage_base)
    elif sid == "delta_queue":
        data = _read_delta_queue(storage_base)
    else:
        return web.json_response({"ok": False, "error": f"No runtime for {sid}"}, status=404)
    return web.json_response({"ok": True, "data": data})


async def handle_trust_mcp(request: web.Request) -> web.Response:
    storage_base = Path(request.app["storage_base"])
    body = await request.json()
    name = body.get("name")
    trust_tier = body.get("trust_tier")
    if not name or not trust_tier:
        return web.json_response({"ok": False, "error": "name and trust_tier required"}, status=400)
    auto_path = storage_base / "cortex_auto_mcps.yaml"
    data = _load_raw(str(auto_path))
    for rec in data.get("records", []):
        if rec.get("name") == name:
            rec["trust_tier"] = trust_tier
            break
    _write_atomic(str(auto_path), data)
    return web.json_response({"ok": True})


async def handle_promote_delta(request: web.Request) -> web.Response:
    storage_base = Path(request.app["storage_base"])
    config_path = request.app["config_path"]
    body = await request.json()
    task_name = body.get("task_name")
    action = body.get("action", "discard")  # promote | discard

    delta_path = storage_base / "cortex_delta" / "pending.yaml"
    data = _load_raw(str(delta_path))
    proposals = data.get("proposals", [])

    if action == "promote":
        proposal = next((p for p in proposals if p.get("task_name") == task_name), None)
        if proposal:
            raw = _load_raw(config_path)
            task_types = raw.get("task_types", [])
            task_types.append({
                "name": proposal.get("task_name"),
                "description": proposal.get("description", ""),
                "output_format": proposal.get("output_format", "text"),
                "complexity": proposal.get("complexity", "adaptive"),
            })
            raw["task_types"] = task_types
            _write_atomic(config_path, raw)

    data["proposals"] = [p for p in proposals if p.get("task_name") != task_name]
    _write_atomic(str(delta_path), data)
    return web.json_response({"ok": True})


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(config_path: str, storage_base: str) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["config_path"] = str(Path(config_path).resolve())
    app["storage_base"] = str(Path(storage_base).resolve())

    app.router.add_get("/", handle_index)
    app.router.add_get("/index.html", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/section/{section_id}", handle_get_section)
    app.router.add_post("/api/section/{section_id}", handle_post_section)
    app.router.add_get("/api/runtime/{section_id}", handle_get_runtime)
    app.router.add_post("/api/runtime/auto_mcps/trust", handle_trust_mcp)
    app.router.add_post("/api/runtime/delta/action", handle_promote_delta)
    app.router.add_static("/", STATIC_DIR)

    return app
