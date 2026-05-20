"""cortex stats — show token usage and session statistics."""
from __future__ import annotations

import asyncio
import json

import click


@click.command("stats")
@click.option("--config", default="cortex.yaml")
@click.option("--user-id", default=None, help="Limit stats to one user (omit for aggregate)")
@click.option("--limit", default=100, show_default=True,
              help="Max sessions to scan per user")
@click.option("--format", "fmt", default="text", type=click.Choice(["text", "json"]))
def stats_command(config: str, user_id: str | None, limit: int, fmt: str):
    """Show token usage and session statistics."""
    asyncio.run(_stats(config, user_id, limit, fmt))


async def _stats(config_path: str, user_id: str | None, limit: int, fmt: str) -> None:
    from pathlib import Path
    from cortex.config.loader import load_config
    from cortex.modules.history_store import HistoryStore

    cfg = load_config(config_path)
    store = HistoryStore(base_path=cfg.storage.base_path)

    # Determine user list
    base = Path(cfg.storage.base_path)
    if user_id:
        users = [user_id]
    else:
        user_root = base / "history"
        if user_root.exists():
            users = [d.name for d in user_root.iterdir() if d.is_dir()]
        else:
            users = []

    if not users:
        click.echo("No session history found.")
        return

    # Aggregate
    totals: dict = {
        "users": 0,
        "sessions": 0,
        "total_tokens": 0,
        "primary_tokens": 0,
        "mcp_tokens": 0,
        "validation_tokens": 0,
        "total_duration_s": 0.0,
        "completed_tasks": 0,
        "total_tasks": 0,
        "failed_tasks": 0,
        "validation_scores": [],
        "by_provider": {},
        "by_user": {},
    }

    for uid in users:
        try:
            records, _ = await store.read_user_history(uid, max_records=limit)
        except Exception:
            continue
        if not records:
            continue

        totals["users"] += 1
        user_tokens = 0
        user_sessions = 0

        for r in records:
            totals["sessions"] += 1
            user_sessions += 1

            tu = r.token_usage
            totals["total_tokens"] += tu.total_tokens
            totals["primary_tokens"] += tu.primary_agent_tokens
            totals["mcp_tokens"] += tu.mcp_agent_tokens
            totals["validation_tokens"] += tu.validation_agent_tokens
            totals["total_duration_s"] += r.duration_seconds
            totals["completed_tasks"] += r.task_completion.completed_tasks
            totals["total_tasks"] += r.task_completion.total_tasks
            totals["failed_tasks"] += r.task_completion.failed_tasks
            user_tokens += tu.total_tokens

            if r.validation_score is not None:
                totals["validation_scores"].append(r.validation_score)

            for provider, count in (tu.by_provider or {}).items():
                totals["by_provider"][provider] = totals["by_provider"].get(provider, 0) + count

        totals["by_user"][uid] = {"sessions": user_sessions, "tokens": user_tokens}

    if fmt == "json":
        output = dict(totals)
        output.pop("validation_scores")
        scores = totals["validation_scores"]
        output["avg_validation_score"] = (sum(scores) / len(scores)) if scores else None
        output["avg_tokens_per_session"] = (
            totals["total_tokens"] // totals["sessions"] if totals["sessions"] else 0
        )
        click.echo(json.dumps(output, indent=2, default=str))
        return

    # Text output
    sessions = totals["sessions"]
    tokens = totals["total_tokens"]
    dur = totals["total_duration_s"]
    scores = totals["validation_scores"]
    avg_score = f"{sum(scores)/len(scores):.3f}" if scores else "n/a"
    avg_tokens = tokens // sessions if sessions else 0
    avg_dur = dur / sessions if sessions else 0.0
    task_rate = (
        f"{totals['completed_tasks']}/{totals['total_tasks']}"
        if totals["total_tasks"] else "0/0"
    )

    click.echo()
    click.echo(click.style("Session Statistics", bold=True))
    click.echo(f"  Users            : {totals['users']}")
    click.echo(f"  Sessions         : {sessions}")
    click.echo(f"  Avg duration     : {avg_dur:.1f}s")
    click.echo()
    click.echo(click.style("Token Usage", bold=True))
    click.echo(f"  Total tokens     : {tokens:,}")
    click.echo(f"  Avg / session    : {avg_tokens:,}")
    click.echo(f"  Primary agent    : {totals['primary_tokens']:,}")
    click.echo(f"  MCP agents       : {totals['mcp_tokens']:,}")
    click.echo(f"  Validation agent : {totals['validation_tokens']:,}")
    if totals["by_provider"]:
        click.echo("  By provider      :")
        for prov, cnt in sorted(totals["by_provider"].items(), key=lambda x: -x[1]):
            click.echo(f"    {prov:<20} {cnt:,}")
    click.echo()
    click.echo(click.style("Task Completion", bold=True))
    click.echo(f"  Tasks            : {task_rate} completed  ({totals['failed_tasks']} failed)")
    click.echo(f"  Avg val. score   : {avg_score}")
    if not user_id and totals["by_user"]:
        click.echo()
        click.echo(click.style("By User", bold=True))
        for uid, udata in sorted(totals["by_user"].items(), key=lambda x: -x[1]["tokens"]):
            click.echo(f"  {uid:<30} {udata['sessions']:>4} sessions   {udata['tokens']:>10,} tokens")
    click.echo()
