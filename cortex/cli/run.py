"""cortex run / cortex chat — invoke the agent from the CLI."""
from __future__ import annotations

import asyncio
import os
import select
import sys
from typing import List, Optional

import click


# ── helpers ───────────────────────────────────────────────────────────────────

def _event_label(event_type: str) -> str:
    return {
        "status": click.style("·", fg="cyan"),
        "task_start": click.style("▶", fg="blue"),
        "task_complete": click.style("✓", fg="green"),
        "task_failed": click.style("✗", fg="red"),
        "clarification": click.style("?", fg="yellow"),
        "learning": click.style("⚙", fg="magenta"),
        "result": click.style("◆", fg="white", bold=True),
        "user_interrupt": click.style("↩", fg="yellow", bold=True),
    }.get(event_type, click.style("·", fg="white"))


async def _drain_queue(
    queue: asyncio.Queue,
    stop_event: asyncio.Event,
    verbose: bool,
    session_id_out: Optional[List[str]] = None,
) -> None:
    """Print streaming events from the framework event queue.

    When *session_id_out* is a one-element list, the first SESSION_START event's
    session_id is stored there so the interrupt reader can use it.
    """
    while not stop_event.is_set() or not queue.empty():
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            continue
        if event is None:
            continue
        event_type_val = getattr(event, "event_type", None)
        event_type = event_type_val.value if hasattr(event_type_val, "value") else str(event_type_val).lower()

        # Capture session_id from first SESSION_START event
        if (
            session_id_out is not None
            and not session_id_out[0]
            and event_type == "session_start"
        ):
            session_id_out[0] = getattr(event, "session_id", "") or ""

        if not verbose and event_type in ("status", "session_start", "session_end",
                                          "learning", "session_token_usage"):
            continue

        label = _event_label(event_type)

        if event_type == "user_interrupt":
            action = getattr(event, "action", "queued")
            msg = getattr(event, "message", "")
            if action == "queued":
                click.echo(f"  {label}  interrupt queued: \"{msg[:60]}\"")
            elif action == "terminate":
                click.echo(click.style(f"  {label}  terminating session per your request", fg="yellow"))
            elif action == "replan":
                click.echo(click.style(f"  {label}  agent is rethinking its plan", fg="yellow"))
            continue

        message = getattr(event, "message", None) or getattr(event, "content", None) or str(event)
        click.echo(f"  {label}  {message}")


async def _interrupt_reader(
    framework,
    session_id_ref: List[str],
    done_event: asyncio.Event,
) -> None:
    """Poll stdin for mid-run user messages and inject them into the session.

    Uses select.select() with a zero timeout to non-blockingly check for
    pending input on Unix/macOS. Yields to the event loop between polls so
    other tasks (drain, session) continue running.

    When the user types a line while the agent is working, the line is injected
    via framework.inject_user_message(). The agent processes it at the next
    wave boundary.
    """
    if not hasattr(select, "select"):
        # Windows fallback: no-op (select.select not available on Windows stdin)
        return

    click.echo(
        click.style(
            "  · Type a message and press Enter at any time to interrupt the agent.",
            fg="bright_black",
        )
    )

    while not done_event.is_set():
        await asyncio.sleep(0.15)
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
        except (ValueError, OSError):
            break
        if not ready:
            continue
        try:
            line = sys.stdin.readline()
        except (EOFError, OSError):
            break
        line = line.strip()
        if not line:
            continue
        sid = session_id_ref[0] if session_id_ref else ""
        if sid and framework.inject_user_message(sid, line):
            click.echo(
                click.style(
                    "  ↩  interrupt queued — will be processed after the current wave",
                    fg="yellow",
                )
            )
        elif not sid:
            click.echo(
                click.style("  · Session not ready yet — interrupt ignored", fg="bright_black")
            )


async def _run_once(
    config_path: str,
    user_id: str,
    request: str,
    verbose: bool,
) -> int:
    """Execute a single session and return exit code."""
    from cortex.framework import CortexFramework

    click.echo(click.style(f"Cortex › {request[:80]}{'…' if len(request) > 80 else ''}", bold=True))
    click.echo()

    try:
        framework = await CortexFramework(config_path).initialize()
    except Exception as exc:
        click.echo(click.style(f"✗ Failed to initialize framework: {exc}", fg="red"), err=True)
        return 1

    queue: asyncio.Queue = asyncio.Queue()
    stop_event = asyncio.Event()
    session_id_ref: List[str] = [""]

    drain_task = asyncio.create_task(
        _drain_queue(queue, stop_event, verbose, session_id_out=session_id_ref)
    )
    interrupt_task = asyncio.create_task(
        _interrupt_reader(framework, session_id_ref, stop_event)
    )

    try:
        result = await framework.run_session(
            user_id=user_id,
            request=request,
            event_queue=queue,
        )
    except Exception as exc:
        stop_event.set()
        await drain_task
        interrupt_task.cancel()
        click.echo(click.style(f"\n✗ Session error: {exc}", fg="red"), err=True)
        return 1
    finally:
        stop_event.set()
        await drain_task
        interrupt_task.cancel()
        try:
            await interrupt_task
        except asyncio.CancelledError:
            pass

    if result.error:
        click.echo(click.style(f"\n✗ {result.error}", fg="red"), err=True)
        return 1

    click.echo()
    click.echo(click.style("Response", bold=True, underline=True))
    click.echo(result.response or "(no response)")
    click.echo()

    # Summary line
    tc = result.task_completion
    tokens = result.token_usage.total_tokens
    dur = result.duration_seconds
    val = ""
    if result.validation_report is not None:
        score = getattr(result.validation_report, "score", None)
        passed = getattr(result.validation_report, "passed", None)
        if score is not None:
            val = f"  validation={score:.2f}({'pass' if passed else 'fail'})"
    click.echo(
        click.style(
            f"tasks={tc.completed_tasks}/{tc.total_tasks}  tokens={tokens}  "
            f"time={dur:.1f}s{val}",
            fg="bright_black",
        )
    )
    return 0


# ── commands ──────────────────────────────────────────────────────────────────

@click.command("run")
@click.argument("request")
@click.option("--config", default="cortex.yaml", help="Path to cortex.yaml")
@click.option("--user-id", default="cli-user", show_default=True, help="User identity")
@click.option("--verbose", "-v", is_flag=True, help="Show all streaming events")
def run_command(request: str, config: str, user_id: str, verbose: bool):
    """Run a single request through the Cortex agent and print the response."""
    code = asyncio.run(_run_once(config, user_id, request, verbose))
    sys.exit(code)


@click.command("chat")
@click.option("--config", default="cortex.yaml", help="Path to cortex.yaml")
@click.option("--user-id", default="cli-user", show_default=True, help="User identity")
@click.option("--verbose", "-v", is_flag=True, help="Show all streaming events")
def chat_command(config: str, user_id: str, verbose: bool):
    """Start an interactive chat REPL with the Cortex agent."""
    asyncio.run(_chat_loop(config, user_id, verbose))


async def _chat_loop(config_path: str, user_id: str, verbose: bool) -> None:
    from cortex.framework import CortexFramework

    click.echo(click.style("Cortex Chat", bold=True) + "  (type 'exit' or Ctrl-C to quit)\n")

    try:
        framework = await CortexFramework(config_path).initialize()
    except Exception as exc:
        click.echo(click.style(f"✗ Failed to initialize framework: {exc}", fg="red"), err=True)
        return

    while True:
        try:
            request = click.prompt(click.style("you", fg="cyan", bold=True))
        except (click.Abort, EOFError):
            click.echo("\nBye.")
            break

        if request.strip().lower() in ("exit", "quit", "bye"):
            click.echo("Bye.")
            break

        if not request.strip():
            continue

        queue: asyncio.Queue = asyncio.Queue()
        stop_event = asyncio.Event()
        session_id_ref: List[str] = [""]

        drain_task = asyncio.create_task(
            _drain_queue(queue, stop_event, verbose, session_id_out=session_id_ref)
        )
        interrupt_task = asyncio.create_task(
            _interrupt_reader(framework, session_id_ref, stop_event)
        )

        try:
            result = await framework.run_session(
                user_id=user_id,
                request=request,
                event_queue=queue,
            )
        except Exception as exc:
            stop_event.set()
            await drain_task
            interrupt_task.cancel()
            click.echo(click.style(f"✗ Error: {exc}", fg="red"), err=True)
            continue
        finally:
            stop_event.set()
            await drain_task
            interrupt_task.cancel()
            try:
                await interrupt_task
            except asyncio.CancelledError:
                pass

        click.echo()
        click.echo(click.style("cortex", fg="green", bold=True) + "  " + (result.response or "(no response)"))
        click.echo()
