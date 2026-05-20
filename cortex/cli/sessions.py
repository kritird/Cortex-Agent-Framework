"""cortex sessions — manage session history."""
from __future__ import annotations

import asyncio
import json

import click


@click.group("sessions")
def sessions_group():
    """Browse, inspect, export, and delete session history."""
    pass


# ── list ──────────────────────────────────────────────────────────────────────

@sessions_group.command("list")
@click.option("--config", default="cortex.yaml")
@click.option("--user-id", required=True, help="User whose history to list")
@click.option("--limit", default=20, show_default=True, help="Max sessions to show")
@click.option("--format", "fmt", default="table", type=click.Choice(["table", "json"]))
def sessions_list(config: str, user_id: str, limit: int, fmt: str):
    """List recent sessions for a user."""
    asyncio.run(_sessions_list(config, user_id, limit, fmt))


async def _sessions_list(config_path: str, user_id: str, limit: int, fmt: str) -> None:
    from cortex.config.loader import load_config
    from cortex.modules.history_store import HistoryStore

    cfg = load_config(config_path)
    store = HistoryStore(base_path=cfg.storage.base_path)
    records, _ = await store.read_user_history(user_id, max_records=limit)

    if not records:
        click.echo(f"No sessions found for user '{user_id}'.")
        return

    if fmt == "json":
        click.echo(json.dumps([r.to_dict() for r in records], indent=2, default=str))
        return

    click.echo(f"\n{'SESSION ID':<38} {'TIMESTAMP':<22} {'TASKS':<8} {'TOKENS':<8} {'VAL':<6} {'DUR':>6}")
    click.echo("-" * 92)
    for r in records:
        tc = r.task_completion
        val = f"{r.validation_score:.2f}" if r.validation_score is not None else "  -  "
        click.echo(
            f"{r.session_id:<38} {r.timestamp[:19]:<22} "
            f"{tc.completed_tasks}/{tc.total_tasks:<6} "
            f"{r.token_usage.total_tokens:<8} {val:<6} {r.duration_seconds:>5.1f}s"
        )
    click.echo()


# ── show ──────────────────────────────────────────────────────────────────────

@sessions_group.command("show")
@click.argument("session_id")
@click.option("--config", default="cortex.yaml")
@click.option("--user-id", required=True, help="User who owns the session")
@click.option("--format", "fmt", default="text", type=click.Choice(["text", "json"]))
def sessions_show(session_id: str, config: str, user_id: str, fmt: str):
    """Show full detail for a session."""
    asyncio.run(_sessions_show(session_id, config, user_id, fmt))


async def _sessions_show(session_id: str, config_path: str, user_id: str, fmt: str) -> None:
    from cortex.config.loader import load_config
    from cortex.modules.history_store import HistoryStore

    cfg = load_config(config_path)
    store = HistoryStore(base_path=cfg.storage.base_path)
    record = await store.read_session_detail(user_id, session_id)

    if not record:
        click.echo(f"Session '{session_id}' not found for user '{user_id}'.", err=True)
        raise SystemExit(1)

    if fmt == "json":
        click.echo(json.dumps(record.to_dict(), indent=2, default=str))
        return

    tc = record.task_completion
    tu = record.token_usage
    click.echo(f"\nSession   : {record.session_id}")
    click.echo(f"Timestamp : {record.timestamp}")
    click.echo(f"User      : {record.user_id}")
    click.echo()
    click.echo(click.style("Request", bold=True))
    click.echo(f"  {record.original_request}")
    click.echo()
    click.echo(click.style("Response summary", bold=True))
    click.echo(f"  {record.response_summary}")
    click.echo()
    click.echo(click.style("Metrics", bold=True))
    click.echo(f"  Tasks      : {tc.completed_tasks}/{tc.total_tasks} completed"
               f"  (failed={tc.failed_tasks}, skipped={tc.skipped_tasks})")
    click.echo(f"  Duration   : {record.duration_seconds:.1f}s")
    click.echo(f"  Tokens     : {tu.total_tokens} total"
               f"  (primary={tu.primary_agent_tokens}, mcp={tu.mcp_agent_tokens},"
               f" validation={tu.validation_agent_tokens})")
    if record.validation_score is not None:
        status = click.style("PASS", fg="green") if record.validation_passed else click.style("FAIL", fg="red")
        click.echo(f"  Validation : {record.validation_score:.3f}  [{status}]")
    if record.complexity_score is not None:
        click.echo(f"  Complexity : {record.complexity_score:.2f}")
    if record.learned_action:
        click.echo(f"  Learning   : {record.learned_action}")
    if record.persisted_files:
        click.echo(f"  Files      : {len(record.persisted_files)} persisted")
        for f in record.persisted_files:
            click.echo(f"    {f.task_name}: {f.file_path} ({f.size_bytes} bytes)")
    click.echo()


# ── delete ────────────────────────────────────────────────────────────────────

@sessions_group.command("delete")
@click.option("--config", default="cortex.yaml")
@click.option("--user-id", required=True, help="User whose history to delete")
@click.option("--all", "delete_all", is_flag=True, help="Delete ALL history for this user")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def sessions_delete(config: str, user_id: str, delete_all: bool, yes: bool):
    """Delete session history for a user."""
    if not delete_all:
        click.echo("Specify --all to delete all history for a user.", err=True)
        raise SystemExit(1)
    if not yes:
        click.confirm(f"Delete ALL history for user '{user_id}'?", abort=True)
    asyncio.run(_sessions_delete(config, user_id))


async def _sessions_delete(config_path: str, user_id: str) -> None:
    from cortex.config.loader import load_config
    from cortex.modules.history_store import HistoryStore

    cfg = load_config(config_path)
    store = HistoryStore(base_path=cfg.storage.base_path)
    await store.delete_user_history(user_id)
    click.echo(click.style(f"✓ Deleted all history for user '{user_id}'.", fg="green"))


# ── export ────────────────────────────────────────────────────────────────────

@sessions_group.command("export")
@click.argument("session_id")
@click.option("--config", default="cortex.yaml")
@click.option("--user-id", required=True, help="User who owns the session")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "md"]))
@click.option("--output", "-o", default=None, help="Output file (default: stdout)")
def sessions_export(session_id: str, config: str, user_id: str, fmt: str, output: str | None):
    """Export a session to JSON or Markdown."""
    asyncio.run(_sessions_export(session_id, config, user_id, fmt, output))


async def _sessions_export(
    session_id: str, config_path: str, user_id: str, fmt: str, output: str | None
) -> None:
    from cortex.config.loader import load_config
    from cortex.modules.history_store import HistoryStore

    cfg = load_config(config_path)
    store = HistoryStore(base_path=cfg.storage.base_path)
    record = await store.read_session_detail(user_id, session_id)

    if not record:
        click.echo(f"Session '{session_id}' not found for user '{user_id}'.", err=True)
        raise SystemExit(1)

    if fmt == "json":
        content = json.dumps(record.to_dict(), indent=2, default=str)
    else:
        tc = record.task_completion
        lines = [
            f"# Session {record.session_id}",
            "",
            f"**Timestamp:** {record.timestamp}  ",
            f"**User:** {record.user_id}  ",
            f"**Duration:** {record.duration_seconds:.1f}s  ",
            "",
            "## Request",
            "",
            record.original_request,
            "",
            "## Response Summary",
            "",
            record.response_summary,
            "",
            "## Metrics",
            "",
            f"- Tasks: {tc.completed_tasks}/{tc.total_tasks} completed",
            f"- Tokens: {record.token_usage.total_tokens}",
        ]
        if record.validation_score is not None:
            lines.append(
                f"- Validation: {record.validation_score:.3f} "
                f"({'PASS' if record.validation_passed else 'FAIL'})"
            )
        content = "\n".join(lines)

    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(content)
        click.echo(f"✓ Exported to {output}")
    else:
        click.echo(content)
