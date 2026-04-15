"""cortex spec — generate OpenAPI spec or agent capability manifest."""
import click
import json


@click.command()
@click.option("--config", default="cortex.yaml")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "yaml"]))
@click.option("--output", "-o", default=None, help="Output file (default: stdout)")
def spec_command(config: str, fmt: str, output: str):
    """Generate a capability manifest for this agent."""
    from cortex.config.loader import load_config
    try:
        cfg = load_config(config)
    except Exception as e:
        click.echo(f"✗ Config error: {e}", err=True)
        raise SystemExit(1)

    manifest = {
        "agent": {
            "name": cfg.agent.name,
            "description": cfg.agent.description,
        },
        "task_types": [
            {
                "name": t.name,
                "description": t.description,
                "output_format": t.output_format,
                "mandatory": t.mandatory,
                "capability_hint": t.capability_hint,
                "depends_on": t.depends_on,
            }
            for t in cfg.task_types
        ],
        "tool_servers": list(cfg.tool_servers.keys()),
        "llm_provider": cfg.llm_access.default.provider,
        "model": cfg.llm_access.default.model,
    }

    if fmt == "json":
        text = json.dumps(manifest, indent=2)
    else:
        import yaml
        text = yaml.dump(manifest, default_flow_style=False)

    if output:
        with open(output, "w") as f:
            f.write(text)
        click.echo(f"✓ Spec written to {output}")
    else:
        click.echo(text)
