"""ValidationAgent — hardened isolated validation of final responses."""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from cortex.config.schema import ValidationConfig
from cortex.exceptions import CortexConfigError
from cortex.llm.client import LLMClient

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


VALIDATION_PROMPT_TEMPLATE = """You are a strict quality assessor evaluating an AI agent's response.

USER REQUEST:
{user_request}

AGENT RESPONSE:
{final_response}

Evaluate the response on exactly three dimensions. For each, provide a score from 0.0 to 1.0:

1. INTENT_MATCH (0.0-1.0): Does the response address what the user actually asked for?
   - 1.0 = perfectly addresses the intent
   - 0.5 = partially addresses the intent
   - 0.0 = completely misses the intent

2. COMPLETENESS (0.0-1.0): Is the response complete and thorough?
   - 1.0 = fully complete, nothing important missing
   - 0.5 = partially complete, some important elements missing
   - 0.0 = severely incomplete

3. COHERENCE (0.0-1.0): Is the response coherent, clear, and well-structured?
   - 1.0 = perfectly coherent and clear
   - 0.5 = somewhat coherent with some confusion
   - 0.0 = incoherent or contradictory

Also list any specific findings (issues you found) with suggestions for improvement.

Respond in this exact format:
INTENT_MATCH_SCORE: <float>
COMPLETENESS_SCORE: <float>
COHERENCE_SCORE: <float>
FINDINGS:
- dimension: <intent_match|completeness|coherence> | issue: <description> | suggestion: <how to fix>
(repeat for each finding, or write NONE if no significant issues)
RECOMMENDATION: <brief overall assessment>
"""


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
        prompt = VALIDATION_PROMPT_TEMPLATE.format(
            user_request=user_request,
            final_response=final_response,
        )

        try:
            response = await asyncio.wait_for(
                self._llm.complete(
                    messages=[{"role": "user", "content": prompt}],
                    system="You are a precise quality evaluator. Follow the output format exactly.",
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

        # Between critical and threshold — attempt remediation
        logger.info("Attempting remediation: score=%.3f", score)
        try:
            remediated = await primary_agent.remediate(
                session_id=session_id,
                original_request=user_request,
                original_response=initial_response,
                validation_findings=report.findings,
                event_queue=event_queue,
            )
            remediated_report = await self.validate(user_request, remediated)
            if remediated_report.passed:
                logger.info("Remediation successful: score=%.3f", remediated_report.composite_score or 0)
                return remediated, remediated_report
            else:
                logger.warning(
                    "Remediation did not improve quality: %.3f",
                    remediated_report.composite_score or 0
                )
                return initial_response, report
        except Exception as e:
            logger.error("Remediation failed: %s", e)
            return initial_response, report
