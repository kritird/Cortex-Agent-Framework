"""cortex ants — manage the ant colony (self-spawned specialist agent mesh)."""
import asyncio
import click


@click.group("ants")
def ants_group():
    """Manage the Ant Colony — self-spawning specialist agent mesh."""
    pass


@ants_group.command("list")
@click.option("--config", default="cortex.yaml", help="Path to cortex.yaml")
def ants_list(config: str):
    """List all ants in the colony with their status."""
    from cortex.ants.ant_colony import AntColony
    from cortex.config.loader import load_config
    try:
        cfg = load_config(config)
    except Exception as e:
        click.echo(f"Could not load config: {e}", err=True)
        return

    colony = AntColony(
        base_path=cfg.storage.base_path,
        base_port=cfg.ant_colony.base_port,
    )
    ants = colony.list_ants()
    if not ants:
        click.echo("No ants in the colony.")
        return

    click.echo(f"\n{'NAME':<24} {'CAPABILITY':<20} {'PORT':<8} {'STATUS':<10} {'PID':<8} RESTARTS")
    click.echo("-" * 80)
    for ant in ants:
        status_color = {
            "running": "green",
            "stopped": "yellow",
            "crashed": "red",
        }.get(ant.status, "white")
        click.echo(
            f"{ant.name:<24} {ant.capability:<20} {ant.port:<8} "
            f"{click.style(ant.status, fg=status_color):<19} {ant.pid:<8} {ant.restart_count}"
        )
    click.echo()


@ants_group.command("status")
@click.argument("name")
@click.option("--config", default="cortex.yaml")
def ants_status(name: str, config: str):
    """Show detailed status of a specific ant."""
    from cortex.ants.ant_colony import AntColony
    from cortex.config.loader import load_config
    try:
        cfg = load_config(config)
    except Exception as e:
        click.echo(f"Could not load config: {e}", err=True)
        return

    colony = AntColony(base_path=cfg.storage.base_path)
    ant = colony.get_ant(name)
    if not ant:
        click.echo(f"No ant named '{name}'.", err=True)
        return

    click.echo(f"\nAnt: {ant.name}")
    click.echo(f"  Capability : {ant.capability}")
    click.echo(f"  Description: {ant.description}")
    click.echo(f"  URL        : {ant.url}")
    click.echo(f"  Port       : {ant.port}")
    click.echo(f"  PID        : {ant.pid}")
    click.echo(f"  Status     : {ant.status}")
    click.echo(f"  Restarts   : {ant.restart_count}")
    click.echo(f"  Created    : {ant.created_at}")
    click.echo(f"  Config     : {ant.cortex_yaml_path}")
    click.echo()


@ants_group.command("hatch")
@click.argument("name")
@click.option("--capability", required=True, help="Capability hint (e.g. web_search)")
@click.option("--description", default="", help="Description of what this ant does")
@click.option("--config", default="cortex.yaml")
def ants_hatch(name: str, capability: str, description: str, config: str):
    """Manually hatch a new ant agent."""
    async def _run():
        from cortex.ants.ant_colony import AntColony
        from cortex.config.loader import load_config
        try:
            cfg = load_config(config)
        except Exception as e:
            click.echo(f"Could not load config: {e}", err=True)
            return

        if not cfg.ant_colony.enabled:
            click.echo("Ant Colony is not enabled. Set ant_colony.enabled: true in cortex.yaml.", err=True)
            return

        colony = AntColony(
            base_path=cfg.storage.base_path,
            base_port=cfg.ant_colony.base_port,
            max_ants=cfg.ant_colony.max_ants,
            auto_restart=cfg.ant_colony.auto_restart,
            llm_provider=cfg.ant_colony.llm_provider,
            llm_model=cfg.ant_colony.llm_model,
            api_key_env_var=cfg.ant_colony.api_key_env_var,
        )
        click.echo(f"Hatching ant '{name}' for capability '{capability}'...")
        try:
            info = await colony.hatch(
                name=name,
                capability=capability,
                description=description or f"Specialist agent for {capability}",
            )
            click.echo(click.style(f"✓ Ant '{info.name}' hatched successfully!", fg="green"))
            click.echo(f"  URL  : {info.url}")
            click.echo(f"  Port : {info.port}")
            click.echo(f"  PID  : {info.pid}")
        except Exception as e:
            click.echo(click.style(f"✗ Failed to hatch ant: {e}", fg="red"), err=True)

    asyncio.run(_run())


@ants_group.command("stop")
@click.argument("name")
@click.option("--config", default="cortex.yaml")
def ants_stop(name: str, config: str):
    """Stop a running ant by name."""
    async def _run():
        from cortex.ants.ant_colony import AntColony
        from cortex.config.loader import load_config
        try:
            cfg = load_config(config)
        except Exception as e:
            click.echo(f"Could not load config: {e}", err=True)
            return

        colony = AntColony(base_path=cfg.storage.base_path)
        try:
            await colony.stop(name)
            click.echo(click.style(f"✓ Ant '{name}' stopped.", fg="green"))
        except KeyError:
            click.echo(f"No ant named '{name}'.", err=True)
        except Exception as e:
            click.echo(click.style(f"✗ Error stopping ant: {e}", fg="red"), err=True)

    asyncio.run(_run())


@ants_group.command("stop-all")
@click.option("--config", default="cortex.yaml")
@click.confirmation_option(prompt="Stop all running ants?")
def ants_stop_all(config: str):
    """Stop all running ants in the colony."""
    async def _run():
        from cortex.ants.ant_colony import AntColony
        from cortex.config.loader import load_config
        try:
            cfg = load_config(config)
        except Exception as e:
            click.echo(f"Could not load config: {e}", err=True)
            return

        colony = AntColony(base_path=cfg.storage.base_path)
        running = [a for a in colony.list_ants() if a.status == "running"]
        if not running:
            click.echo("No running ants to stop.")
            return
        await colony.stop_all()
        click.echo(click.style(f"✓ Stopped {len(running)} ant(s).", fg="green"))

    asyncio.run(_run())
