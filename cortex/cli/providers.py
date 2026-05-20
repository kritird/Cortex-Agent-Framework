"""cortex providers — list and test configured LLM providers."""
from __future__ import annotations

import asyncio
import os
import sys

import click


@click.group("providers")
def providers_group():
    """List and test configured LLM providers."""
    pass


# ── list ──────────────────────────────────────────────────────────────────────

@providers_group.command("list")
@click.option("--config", default="cortex.yaml")
def providers_list(config: str):
    """List all LLM providers configured in cortex.yaml."""
    from cortex.config.loader import load_config

    try:
        cfg = load_config(config)
    except Exception as exc:
        click.echo(click.style(f"✗ {exc}", fg="red"), err=True)
        sys.exit(1)

    providers: dict = {"default": cfg.llm_access.default}
    providers.update(cfg.llm_access.providers or {})

    click.echo(f"\n{'KEY':<18} {'PROVIDER':<14} {'MODEL':<40} {'API KEY ENV':<30} STATUS")
    click.echo("-" * 110)

    for key, p in providers.items():
        env_var = p.api_key_env_var or ""
        key_set = bool(env_var and os.environ.get(env_var))
        status = click.style("✓ key set", fg="green") if key_set else (
            click.style("⚠ no key", fg="yellow") if env_var else
            click.style("  (no env var)", fg="bright_black")
        )
        click.echo(
            f"{key:<18} {p.provider:<14} {p.model[:39]:<40} {env_var:<30} {status}"
        )
    click.echo()


# ── test ──────────────────────────────────────────────────────────────────────

@providers_group.command("test")
@click.option("--config", default="cortex.yaml")
@click.option("--provider", "provider_key", default=None,
              help="Provider key to test (default: all configured providers)")
@click.option("--prompt", "test_prompt", default="Reply with the single word: OK",
              show_default=True, help="Test prompt to send")
def providers_test(config: str, provider_key: str | None, test_prompt: str):
    """Send a minimal test prompt to verify each provider is reachable."""
    asyncio.run(_providers_test(config, provider_key, test_prompt))


async def _providers_test(config_path: str, provider_key: str | None, test_prompt: str) -> None:
    from cortex.config.loader import load_config
    from cortex.llm.client import LLMClient

    try:
        cfg = load_config(config_path)
    except Exception as exc:
        click.echo(click.style(f"✗ {exc}", fg="red"), err=True)
        sys.exit(1)

    all_providers: dict = {"default": cfg.llm_access.default}
    all_providers.update(cfg.llm_access.providers or {})

    targets = {provider_key: all_providers[provider_key]} if provider_key else all_providers

    for key, p in targets.items():
        click.echo(f"  Testing {key} ({p.provider} / {p.model}) … ", nl=False)
        try:
            client = LLMClient(provider_config=p)
            response = await client.complete(
                messages=[{"role": "user", "content": test_prompt}],
                max_tokens=16,
            )
            text = (response or "").strip()[:40]
            click.echo(click.style(f"✓  → {text!r}", fg="green"))
        except Exception as exc:
            click.echo(click.style(f"✗  {exc}", fg="red"))

    click.echo()
