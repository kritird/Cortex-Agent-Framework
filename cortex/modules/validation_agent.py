"""ValidationAgent — hardened isolated validation of final responses."""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from cortex.config.schema import ValidationConfig
from cortex.exceptions import CortexConfigError
from cortex.llm.client import LLMClient
from cortex.prompts import VALIDATION_SYSTEM, VALIDATION_USER
from cortex.streaming.status_events import ResultEvent

logger = logging.getLogger(__name__)

# Floor enforcement: threshold cannot go below this
THRESHOLD_FLOOR = 0.60


@dataclass
class ValidationFinding:
    dimension: str  # "intent_match" | "completeness" | "coherence"
    issue: str
    suggestion: str


@dataclass
class ValidationReport:
    schema_version: str = "1.0"
    intent_match_score: Optional[float] = None
    completeness_score: Optional[float] = None
    coherence_score: Optional[float] = None
    composite_score: Optional[float] = None
    passed: Optional[bool] = None
    threshold_used: float = 0.75
    findings: List[ValidationFinding] = field(default_factory=list)
    validator_recommendation: str = ""
    status: str = "complete"  # complete | unavailable



def _parse_validation_response(text: str) -> tuple[Optional[float], Optional[float], Optional[float], List[ValidationFinding], str]:
    """Parse LLM validation response into scores and findings."""
    intent = None
    completeness = None
    coherence = None
    findings = []
    recommendation = ""

    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("INTENT_MATCH_SCORE:"):
            try:
                intent = float(line.split(":", 1)[1].strip())
                intent = max(0.0, min(1.0, intent))
            except ValueError:
                pass
        elif line.startswith("COMPLETENESS_SCORE:"):
            try:
                completeness = float(line.split(":", 1)[1].strip())
                completeness = max(0.0, min(1.0, completeness))
            except ValueError:
                pass
        elif line.startswith("COHERENCE_SCORE:"):
            try:
                coherence = float(line.split(":", 1)[1].strip())
                coherence = max(0.0, min(1.0, coherence))
            except ValueError:
                pass
        elif line.startswith("RECOMMENDATION:"):
            recommendation = line.split(":", 1)[1].strip()
        elif line.startswith("- dimension:") and "issue:" in line and "suggestion:" in line:
            parts = line.lstrip("- ").split(" | ")
            if len(parts) >= 3:
                dim = parts[0].replace("dimension:", "").strip()
                issue = parts[1].replace("issue:", "").strip()
                suggestion = parts[2].replace("suggestion:", "").strip()
                findings.append(ValidationFinding(dimension=dim, issue=issue, suggestion=suggestion))

    return intent, completeness, coherence, findings, recommendation


class ValidationAgent:
    """
    HARDENED — cannot be disabled, cannot be misconfigured below floor.
    Isolated code path: receives ONLY (user_request, final_response).
    Uses llm_access.default — not overridable.
    """

    def __init__(self, llm_client: LLMClient, config: ValidationConfig):
        if config.threshold < THRESHOLD_FLOOR:
            raise CortexConfigError(
                f"validation.threshold {config.threshold} is below the minimum floor of {THRESHOLD_FLOOR}. "
                f"Set threshold >= {THRESHOLD_FLOOR} to protect response quality."
            )
        self._llm = llm_client
        self._config = config

    async def validate(
        self,
        user_request: str,
        final_response: str,
        config: Optional[ValidationConfig] = None,
    ) -> ValidationReport:
        """
        CRITICAL: Receives ONLY user_request and final_response.
        No session_id, no task list, no history.
        """
        cfg = config or self._config
        prompt = VALIDATION_USER.format(
            user_request=user_request,
            final_response=final_response,
        )

        try:
            response = await asyncio.wait_for(
                self._llm.complete(
                    messages=[{"role": "user", "content": prompt}],
                    system=VALIDATION_SYSTEM,
                    provider_name="default",
                    max_tokens=1024,
                ),
                timeout=cfg.timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("ValidationAgent timed out after %ds", cfg.timeout_seconds)
            return ValidationReport(status="unavailable", threshold_used=cfg.threshold)
        except Exception as e:
            logger.error("ValidationAgent LLM call failed: %s", e)
            return ValidationReport(status="unavailable", threshold_used=cfg.threshold)

        intent, completeness, coherence, findings, recommendation = _parse_validation_response(
            response.content
        )

        # Compute composite score
        composite = None
        if intent is not None and completeness is not None and coherence is not None:
            composite = (
                intent * cfg.weights_intent_match
                + completeness * cfg.weights_completeness
                + coherence * cfg.weights_coherence
            )
            composite = round(composite, 4)

        passed = composite is not None and composite >= cfg.threshold

        report = ValidationReport(
            intent_match_score=intent,
            completeness_score=completeness,
            coherence_score=coherence,
            composite_score=composite,
            passed=passed,
            threshold_used=cfg.threshold,
            findings=findings,
            validator_recommendation=recommendation,
            status="complete",
        )

        logger.info(
            "Validation complete: composite=%.3f passed=%s",
            composite or 0.0, passed
        )
        return report

    async def validate_with_remediation(
        self,
        user_request: str,
        initial_response: str,
        primary_agent,
        config: Optional[ValidationConfig],
        session_id: str,
        event_queue: asyncio.Queue,
    ) -> tuple[Optional[str], ValidationReport]:
        """
        Full validation flow with optional remediation.
        Returns (response_to_deliver, report).
        """
        cfg = config or self._config
        report = await self.validate(user_request, initial_response)

        if report.status == "unavailable":
            # Validation unavailable — deliver response with note
            return initial_response, report

        if report.passed:
            return initial_response, report

        score = report.composite_score or 0.0

        if score < cfg.critical_threshold:
            logger.warning(
                "Response below critical threshold (%.3f < %.3f) — not delivering",
                score, cfg.critical_threshold
            )
            return None, report

        # Between critical and threshold — attempt remediation. Up to
        # cfg.max_remediation_attempts passes; each pass sees the prior
        # attempt's response and the findings it still failed on, so it can
        # correct without repeating mistakes. The best-scoring candidate
        # across the original and all attempts is delivered if none passes.
        max_attempts = max(1, getattr(cfg, "max_remediation_attempts", 1))
        logger.info(
            "Attempting remediation: score=%.3f (up to %d pass%s)",
            score, max_attempts, "" if max_attempts == 1 else "es",
        )
        best_response, best_report = initial_response, report
        prior_attempts: list[tuple] = []

        async def _deliver(text: str) -> None:
            """Emit the finally chosen response. Intermediate remediation passes
            run with stream=False, so the user only ever sees the response that
            is actually delivered — never a discarded attempt."""
            await event_queue.put(ResultEvent(
                content=text, session_id=session_id, partial=False,
            ))

        try:
            for attempt in range(1, max_attempts + 1):
                remediated = await primary_agent.remediate(
                    session_id=session_id,
                    original_request=user_request,
                    original_response=initial_response,
                    validation_findings=report.findings,
                    event_queue=event_queue,
                    prior_attempts=prior_attempts,
                    stream=False,
                )
                remediated_report = await self.validate(user_request, remediated)
                r_score = remediated_report.composite_score or 0.0

                if r_score > (best_report.composite_score or 0.0):
                    best_response, best_report = remediated, remediated_report

                if remediated_report.passed:
                    logger.info(
                        "Remediation successful on attempt %d/%d: score=%.3f",
                        attempt, max_attempts, r_score,
                    )
                    await _deliver(remediated)
                    return remediated, remediated_report

                logger.warning(
                    "Remediation attempt %d/%d did not pass: %.3f",
                    attempt, max_attempts, r_score,
                )
                # Feed this failed attempt into the next pass so it is not repeated.
                prior_attempts.append((remediated, remediated_report.findings))

            logger.warning(
                "Remediation exhausted %d attempt(s) — delivering best candidate "
                "(score=%.3f)",
                max_attempts, best_report.composite_score or 0.0,
            )
            await _deliver(best_response)
            return best_response, best_report
        except Exception as e:
            logger.error("Remediation failed: %s", e)
            await _deliver(best_response)
            return best_response, best_report
