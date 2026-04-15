"""ObservabilityEmitter — dual-stream emitter for operational logs and audit log."""
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple

from cortex.modules.history_store import HistoryRecord
from cortex.modules.result_envelope_store import ResultEnvelope

logger = logging.getLogger(__name__)


class ObservabilityEmitter:
    """
    Dual-stream emitter:
    - Operational stream: OTel or stdout JSON (sanitised)
    - Audit log: separate append-only file for security events

    Security events NEVER appear in the OTel/operational stream.
    Rolling baselines per task type for anomaly detection.
    """

    def __init__(
        self,
        audit_log_path: str,
        otel_enabled: bool = False,
        otel_endpoint: Optional[str] = None,
        service_name: str = "cortex-agent",
    ):
        self._audit_path = Path(audit_log_path)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._otel_enabled = otel_enabled
        self._service_name = service_name
        # Rolling baselines: {task_name: deque of duration_ms samples}
        self._baselines: Dict[str, Deque[float]] = {}
        self._baseline_max = 100  # samples per task type
        self._tracer = None

        if otel_enabled and otel_endpoint:
            self._setup_otel(otel_endpoint)

    def _setup_otel(self, endpoint: str) -> None:
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            provider = TracerProvider()
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer(self._service_name)
        except ImportError:
            logger.warning("OpenTelemetry packages not available; using stdout JSON logging")

    def _emit_operational(self, event_type: str, data: dict) -> None:
        """Emit to OTel span or structured JSON stdout."""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "service": self._service_name,
            **data,
        }
        if self._tracer:
            try:
                with self._tracer.start_as_current_span(event_type) as span:
                    for k, v in data.items():
                        span.set_attribute(k, str(v))
            except Exception:
                pass
        else:
            print(json.dumps(record), flush=True)

    def _write_audit(self, entry: dict) -> None:
        """Append to audit log file. Never to operational stream."""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            **entry,
        }
        try:
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            logger.error("Audit log write failed: %s", e)

    def emit_session_start(self, session_id: str, user_id: str) -> None:
        self._emit_operational("session_start", {
            "session_id": session_id,
            "user_id": user_id,
        })

    def emit_session_end(self, session_id: str, record: HistoryRecord) -> None:
        self._emit_operational("session_end", {
            "session_id": session_id,
            "user_id": record.user_id,
            "duration_seconds": record.duration_seconds,
            "validation_score": record.validation_score,
            "validation_passed": record.validation_passed,
            "total_tokens": record.token_usage.total_tokens,
            "tasks_completed": record.task_completion.completed_tasks,
            "tasks_total": record.task_completion.total_tasks,
        })

    def emit_task_dispatch(self, session_id: str, task_id: str, task_name: str) -> None:
        self._emit_operational("task_dispatch", {
            "session_id": session_id,
            "task_id": task_id,
            "task_name": task_name,
        })

    def emit_task_complete(self, session_id: str, envelope: ResultEnvelope) -> None:
        self._emit_operational("task_complete", {
            "session_id": session_id,
            "task_id": envelope.task_id,
            "status": envelope.status,
            "duration_ms": envelope.duration_ms,
            "output_type": envelope.output_type,
        })
        self.update_baseline(
            envelope.task_id.split("_", 1)[-1] if "_" in envelope.task_id else envelope.task_id,
            envelope.duration_ms,
        )

    def emit_llm_call(
        self,
        session_id: str,
        call_type: str,
        latency_ms: int,
        tokens: int,
        provider: str,
    ) -> None:
        self._emit_operational("llm_call", {
            "session_id": session_id,
            "call_type": call_type,
            "latency_ms": latency_ms,
            "tokens": tokens,
            "provider": provider,
        })

    def emit_validation_result(self, session_id: str, report) -> None:
        self._emit_operational("validation_result", {
            "session_id": session_id,
            "composite_score": report.composite_score,
            "passed": report.passed,
            "status": report.status,
        })

    def emit_status_event(self, session_id: str, message: str) -> None:
        self._emit_operational("status_event", {
            "session_id": session_id,
            "message": message,
        })

    def audit_security_event(
        self,
        event_type: str,
        detail: str,
        session_id: str,
    ) -> None:
        """Write to audit log only. Never to OTel or stdout operational stream."""
        self._write_audit({
            "event_type": event_type,
            "detail": detail,
            "session_id": session_id,
        })

    def update_baseline(self, task_name: str, duration_ms: int) -> None:
        """Maintain rolling average duration per task type (last 100 samples)."""
        if task_name not in self._baselines:
            self._baselines[task_name] = deque(maxlen=self._baseline_max)
        self._baselines[task_name].append(duration_ms)

        # Check for anomaly: current > 3x baseline
        samples = self._baselines[task_name]
        if len(samples) >= 5:  # Need enough samples for a meaningful baseline
            baseline = sum(list(samples)[:-1]) / (len(samples) - 1)
            if baseline > 0 and duration_ms > 3 * baseline:
                self.emit_anomaly(task_name, duration_ms, baseline)

    def emit_anomaly(self, task_name: str, actual_ms: int, baseline_ms: float) -> None:
        self._emit_operational("task_anomaly", {
            "task_name": task_name,
            "actual_ms": actual_ms,
            "baseline_ms": round(baseline_ms, 1),
            "ratio": round(actual_ms / baseline_ms, 2) if baseline_ms > 0 else 0,
        })
        logger.warning(
            "Task anomaly detected: %s took %dms (baseline: %.1fms, %.1fx)",
            task_name, actual_ms, baseline_ms,
            actual_ms / baseline_ms if baseline_ms > 0 else 0,
        )
