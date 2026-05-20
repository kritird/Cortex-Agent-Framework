"""cortex mcps — manage the external MCP server registry."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click


def _registry_path(config_path: str) -> str:
    from cortex.config.loader import load_config
    cfg = load_config(config_path)
    return str(Path(cfg.storage.base_path) / "cortex_auto_mcps.yaml")


# ── group ─────────────────────────────────────────────────────────────────────

@click.group("mcps")
def mcps_group():
    """Manage auto-discovered external MCP server registry."""
    pass


# ── list ──────────────────────────────────────────────────────────────────────

@mcps_group.command("list")
@click.option("--config", default="cortex.yaml")
@click.option("--filter", "status_filter",
              type=click.Choice(["all", "verified", "auth-pending", "failed"]),
              default="all", show_default=True)
@click.option("--format", "fmt", default="table", type=click.Choice(["table", "json"]))
def mcps_list(config: str, status_filter: str, fmt: str):
    """List known external MCP servers."""
    from cortex.modules.external_mcp_registry import ExternalMCPRegistry

    store_path = _registry_path(config)
    reg = ExternalMCPRegistry(store_path=store_path)
    reg.load()

    all_records = list(reg._records.values())
    if status_filter == "verified":
        records = reg.get_all_verified()
    elif status_filter == "auth-pending":
        records = reg.get_auth_pending()
    elif status_filter == "failed":
        records = [r for r in all_records if getattr(r, "status", "") == "failed"]
    else:
        records = all_records

    if not records:
        click.echo("No MCP servers found in registry.")
        return

    if fmt == "json":
        import dataclasses
        click.echo(json.dumps(
            [dataclasses.asdict(r) if hasattr(r, "__dataclass_fields__") else vars(r)
             for r in records],
            indent=2, default=str,
        ))
        return

    click.echo(f"\n{'NAME':<24} {'URL':<40} {'STATUS':<12} {'CAPABILITIES'}")
    click.echo("-" * 100)
    for r in records:
        name = getattr(r, "name", "") or ""
        url = getattr(r, "url", "") or ""
        status = getattr(r, "status", "") or ""
        caps = ", ".join(getattr(r, "capabilities", []) or [])
        status_color = {"verified": "green", "auth_required": "yellow",
                        "failed": "red"}.get(status, "white")
        click.echo(
            f"{name:<24} {url[:39]:<40} "
            f"{click.style(status, fg=status_color):<21} {caps[:40]}"
        )
    click.echo()


# ── test ──────────────────────────────────────────────────────────────────────

@mcps_group.command("test")
@click.argument("url")
@click.option("--timeout", default=10.0, show_default=True, help="Connection timeout seconds")
def mcps_test(url: str, timeout: float):
    """Test connectivity to an MCP server URL."""
    asyncio.run(_mcps_test(url, timeout))


async def _mcps_test(url: str, timeout: float) -> None:
    import aiohttp
    click.echo(f"Testing {url} …")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                click.echo(
                    click.style(f"✓ Reachable — HTTP {resp.status}", fg="green")
                )
    except Exception as exc:
        click.echo(click.style(f"✗ Unreachable: {exc}", fg="red"), err=True)
        raise SystemExit(1)


# ── add ───────────────────────────────────────────────────────────────────────

@mcps_group.command("add")
@click.argument("url")
@click.option("--config", default="cortex.yaml")
@click.option("--name", default="", help="Human-readable name for this MCP")
@click.option("--capability", "capabilities", multiple=True,
              help="Capability tag (repeatable, e.g. --capability web_search)")
def mcps_add(url: str, config: str, name: str, capabilities: tuple):
    """Register an external MCP server in the registry."""
    from cortex.modules.external_mcp_registry import ExternalMCPRegistry
    from cortex.config.auto_discovery_schema import AutoDiscoveredMCPRecord

    store_path = _registry_path(config)
    reg = ExternalMCPRegistry(store_path=store_path)
    reg.load()

    if reg.has_url(url):
        click.echo(f"URL already registered: {url}", err=True)
        raise SystemExit(1)

    record = AutoDiscoveredMCPRecord(
        url=url,
        name=name or url,
        capabilities=list(capabilities),
        status="verified",
    )
    reg.register(record)
    click.echo(click.style(f"✓ Registered {url}", fg="green"))
    if capabilities:
        click.echo(f"  Capabilities: {', '.join(capabilities)}")


# ── remove ────────────────────────────────────────────────────────────────────

@mcps_group.command("remove")
@click.argument("url")
@click.option("--config", default="cortex.yaml")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def mcps_remove(url: str, config: str, yes: bool):
    """Remove an MCP server from the registry."""
    from cortex.modules.external_mcp_registry import ExternalMCPRegistry

    store_path = _registry_path(config)
    reg = ExternalMCPRegistry(store_path=store_path)
    reg.load()

    if not reg.has_url(url):
        click.echo(f"URL not found in registry: {url}", err=True)
        raise SystemExit(1)

    if not yes:
        click.confirm(f"Remove '{url}' from the MCP registry?", abort=True)

    del reg._records[url]
    reg._pending_auth = [p for p in reg._pending_auth if p.url != url]
    reg.persist()
    click.echo(click.style(f"✓ Removed {url}", fg="green"))
