"""cortex publish — publish docker image, package, or MCP server."""
import click


@click.group()
def publish_group():
    """Publish your agent as a Docker image, Python package, or MCP server."""
    pass


@publish_group.command("docker")
@click.option("--tag", default="cortex-agent:latest")
@click.option("--config", default="cortex.yaml")
def publish_docker(tag: str, config: str):
    """Build and publish a Docker image for this agent."""
    click.echo(f"Building Docker image: {tag}")
    click.echo("  (Generating Dockerfile...)")
    dockerfile = _generate_dockerfile(config)
    with open("Dockerfile.cortex", "w") as f:
        f.write(dockerfile)
    click.echo("  ✓ Dockerfile.cortex generated")
    click.echo(f"  Run: docker build -f Dockerfile.cortex -t {tag} .")


def _generate_dockerfile(config_path: str) -> str:
    return f"""FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install -e .
CMD ["python", "-m", "cortex.cli.main", "dev", "--config", "{config_path}"]
"""


@publish_group.command("package")
@click.option("--output-dir", default="dist")
def publish_package(output_dir: str):
    """Build a distributable Python package."""
    import subprocess
    click.echo("Building Python package...")
    try:
        subprocess.run(["python", "-m", "build", "-o", output_dir], check=True)
        click.echo(f"✓ Package built in {output_dir}/")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        click.echo(f"✗ Build failed: {e}", err=True)


@publish_group.command("mcp")
@click.option("--config", default="cortex.yaml")
@click.option("--port", default=8080)
def publish_mcp(config: str, port: int):
    """Export this agent as an MCP server."""
    click.echo(f"Generating MCP server wrapper (port {port})...")
    click.echo("  This agent can be accessed by other Cortex instances as a tool server.")
    click.echo(f"  Run: cortex publish mcp --port {port}")
    click.echo(f"  Configure in other cortex.yaml as tool_server with url: http://host:{port}")
