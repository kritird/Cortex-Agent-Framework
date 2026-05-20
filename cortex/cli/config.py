"""cortex config — config validation and inspection commands."""
from __future__ import annotations

import click


@click.group("config")
def config_group():
    """Validate and inspect cortex.yaml configuration."""
    pass


@config_group.command("validate")
@click.option("--config", default="cortex.yaml", help="Path to cortex.yaml")
@click.option("--strict", is_flag=True, help="Treat warnings as errors")
def config_validate(config: str, strict: bool):
    """Validate cortex.yaml and report any errors or warnings."""
    import sys
    import yaml
    from pathlib import Path
    from cortex.config.validator import validate_config
    from cortex.config.loader import load_config

    path = Path(config)
    if not path.exists():
        click.echo(click.style(f"✗ File not found: {config}", fg="red"), err=True)
        sys.exit(1)

    # Raw YAML parse
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        click.echo(click.style(f"✗ YAML parse error: {exc}", fg="red"), err=True)
        sys.exit(1)

    click.echo(f"Validating {config} …\n")

    # Schema-level validation
    errors = validate_config(raw)
    warnings: list[str] = []

    # Pydantic model load (catches type errors schema-level validation might miss)
    try:
        cfg = load_config(config)
    except Exception as exc:
        errors.append(str(exc))
        cfg = None

    # Heuristic checks
    if cfg is not None:
        if not cfg.task_types:
            warnings.append("No task_types defined — the agent will have limited capabilities")
        if not cfg.tool_servers:
            warnings.append("No tool_servers configured — agent can only use built-in tools")
        if getattr(cfg.llm_access, "default", None) is None:
            errors.append("llm_access.default is required")
        for name, srv in (cfg.tool_servers or {}).items():
            if not srv.url and srv.transport != "stdio":
                warnings.append(f"Tool server '{name}' has no URL and transport is not stdio")

        # Task graph cycle check
        try:
            from cortex.modules.task_graph_compiler import TaskGraphCompiler
            TaskGraphCompiler().compile(cfg.task_types)
        except Exception as exc:
            errors.append(f"Task graph error: {exc}")

    # Report
    ok = not errors and (not warnings or not strict)

    for w in warnings:
        click.echo(click.style(f"  ⚠  {w}", fg="yellow"))
    for e in errors:
        click.echo(click.style(f"  ✗  {e}", fg="red"))

    if ok:
        if cfg is not None:
            click.echo(click.style("✓ Config is valid", fg="green"))
            click.echo(f"  Agent       : {cfg.agent.name}")
            click.echo(f"  Provider    : {cfg.llm_access.default.provider} / {cfg.llm_access.default.model}")
            click.echo(f"  Task types  : {len(cfg.task_types)}")
            click.echo(f"  Tool servers: {len(cfg.tool_servers)}")
        else:
            click.echo(click.style("✓ YAML is parseable (full model load failed)", fg="yellow"))
    else:
        click.echo(click.style(f"\n✗ Validation failed ({len(errors)} error(s))", fg="red"), err=True)
        sys.exit(1)


@config_group.command("show")
@click.option("--config", default="cortex.yaml")
@click.option("--section",
              type=click.Choice(["agent", "llm", "tasks", "tools", "storage", "learning", "all"]),
              default="all", show_default=True)
def config_show(config: str, section: str):
    """Print a summary of the loaded configuration."""
    import sys
    from cortex.config.loader import load_config

    try:
        cfg = load_config(config)
    except Exception as exc:
        click.echo(click.style(f"✗ {exc}", fg="red"), err=True)
        sys.exit(1)

    if section in ("agent", "all"):
        click.echo(click.style("\n[agent]", bold=True))
        click.echo(f"  name         : {cfg.agent.name}")
        click.echo(f"  description  : {getattr(cfg.agent, 'description', '')[:80]}")

    if section in ("llm", "all"):
        click.echo(click.style("\n[llm_access]", bold=True))
        d = cfg.llm_access.default
        click.echo(f"  default      : {d.provider} / {d.model}")
        for key, p in (cfg.llm_access.providers or {}).items():
            click.echo(f"  {key:<14}: {p.provider} / {p.model}")

    if section in ("tasks", "all"):
        click.echo(click.style(f"\n[task_types]  ({len(cfg.task_types)})", bold=True))
        for t in cfg.task_types:
            deps = f" ← {', '.join(t.depends_on)}" if t.depends_on else ""
            click.echo(f"  {t.name:<30} [{t.output_format}]{deps}")

    if section in ("tools", "all"):
        click.echo(click.style(f"\n[tool_servers]  ({len(cfg.tool_servers)})", bold=True))
        for name, srv in cfg.tool_servers.items():
            addr = srv.url or "(stdio)"
            click.echo(f"  {name:<24} {addr}  [{srv.transport}]")

    if section in ("storage", "all"):
        click.echo(click.style("\n[storage]", bold=True))
        click.echo(f"  base_path    : {cfg.storage.base_path}")
        click.echo(f"  backend      : {getattr(cfg.storage, 'backend', 'filesystem')}")

    if section in ("learning", "all"):
        lc = cfg.learning
        click.echo(click.style("\n[learning]", bold=True))
        click.echo(f"  enabled      : {lc.enabled}")
        click.echo(f"  min_confidence_to_promote : {getattr(lc, 'min_confidence_to_promote', 'n/a')}")

    click.echo()
