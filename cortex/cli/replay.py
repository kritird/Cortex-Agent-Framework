"""cortex replay — replay a session from history."""
import asyncio
import click


@click.command()
@click.argument("session_id")
@click.option("--config", default="cortex.yaml", help="Path to cortex.yaml")
@click.option("--user-id", required=True, help="User ID who owns the session")
def replay_command(session_id: str, config: str, user_id: str):
    """Replay a session from history."""
    asyncio.run(_replay(session_id, config, user_id))


async def _replay(session_id: str, config_path: str, user_id: str):
    from cortex.config.loader import load_config
    from cortex.modules.history_store import HistoryStore
    cfg = load_config(config_path)
    store = HistoryStore(base_path=cfg.storage.base_path)
    record = await store.read_session_detail(user_id, session_id)
    if not record:
        click.echo(f"✗ Session {session_id} not found for user {user_id}", err=True)
        raise SystemExit(1)
    click.echo(f"Session: {record.session_id}")
    click.echo(f"Timestamp: {record.timestamp}")
    click.echo(f"Request: {record.original_request}")
    click.echo()
    click.echo(f"Response summary:\n{record.response_summary}")
    click.echo()
    click.echo(f"Tasks: {record.task_completion.completed_tasks}/{record.task_completion.total_tasks} completed")
    if record.validation_score is not None:
        click.echo(f"Validation: {record.validation_score:.3f} ({'PASS' if record.validation_passed else 'FAIL'})")
    click.echo(f"Duration: {record.duration_seconds:.1f}s")
