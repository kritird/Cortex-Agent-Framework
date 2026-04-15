"""cortex dry-run — validate config and simulate session without LLM calls."""
import asyncio
import click


@click.command()
@click.option("--config", default="cortex.yaml", help="Path to cortex.yaml")
@click.argument("request", default="Test request for dry run")
def dry_run_command(config: str, request: str):
    """Validate config and simulate a session without making LLM calls."""
    asyncio.run(_dry_run(config, request))


async def _dry_run(config_path: str, request: str):
    from cortex.config.loader import load_config
    from cortex.modules.task_graph_compiler import TaskGraphCompiler
    click.echo(f"Dry run: {config_path}")
    click.echo(f"Request: {request[:100]}")
    click.echo()
    try:
        cfg = load_config(config_path)
        click.echo(f"✓ Config valid")
        click.echo(f"  Agent: {cfg.agent.name}")
        click.echo(f"  Task types ({len(cfg.task_types)}):")
        for t in cfg.task_types:
            deps = f" ← {', '.join(t.depends_on)}" if t.depends_on else ""
            click.echo(f"    - {t.name} [{t.output_format}]{deps}")
        compiler = TaskGraphCompiler()
        graph = compiler.compile(cfg.task_types)
        click.echo(f"  ✓ Task graph compiled (topological order: {' → '.join(graph.topo_order[:5])}{'...' if len(graph.topo_order) > 5 else ''})")
        click.echo()
        click.echo("Tool servers:")
        for name, srv in cfg.tool_servers.items():
            click.echo(f"  - {name}: {srv.url or '(stdio)'} [{srv.transport}]")
        click.echo()
        click.echo("✓ Dry run complete. No LLM calls made.")
    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise SystemExit(1)
