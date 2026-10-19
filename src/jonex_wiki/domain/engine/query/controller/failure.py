"""Deterministic CTRL-001 failure classification contracts.

The source-closed classifier is suitable for evaluation and incident audit: it
requires an authoritative source-support decision, then finds the first failed
layer in the source -> compile -> retrieve/read -> reason -> format -> evidence
state chain.  Runtime classification is deliberately narrower.  It labels only
facts observed by the query process and never claims source-level closure.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Tuple

from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict

from jonex_wiki.domain.engine.query.controller.control_state import FailureClass


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
class FailureTrace(_FrozenModel):
    """Source-closed observations for one answerable evaluation case."""

    source_support_present: bool
    compiled_support_present: bool
    supporting_candidate_present: bool
    supporting_evidence_read: bool
    required_reasoning_complete: bool
    answer_semantically_correct: bool
    answer_format_valid: bool
    evidence_state_valid: bool

    @model_validator(mode="after")
    def _pipeline_order_is_possible(self):
        if self.supporting_candidate_present and not self.compiled_support_present:
            raise ValueError("FAILURE_CANDIDATE_WITHOUT_COMPILE")
        if self.supporting_evidence_read and not self.supporting_candidate_present:
            raise ValueError("FAILURE_READ_WITHOUT_CANDIDATE")
        return self


class RuntimeFailureSignals(_FrozenModel):
    """Facts the live query loop can observe without a gold/source oracle."""

    stop_reason: str
    compiled_supported_claim_count: int = Field(ge=0)
    successful_read: bool
    synthesis_invalid: bool = False
    answer_not_produced: bool = False
    required_slots_incomplete: bool = False
    evidence_budget_exhausted: bool = False
    controller_answer_rejected: bool = False
    grounded_incomplete: bool = False

    @field_validator("stop_reason")
    def _non_blank_reason(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("RUNTIME_FAILURE_STOP_REASON_INVALID")
        return value


class FailureDecision(_FrozenModel):
    """One mutually exclusive classification plus an auditable reason code."""

    failure_class: FailureClass
    reason_code: str
    classification_scope: Literal["source_closed", "runtime_observed"]
    decisive_observations: Tuple[str, ...]

    @field_validator("reason_code")
    def _non_blank_code(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("FAILURE_REASON_CODE_INVALID")
        return value

    @field_validator("decisive_observations")
    def _observations_present(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        if not values or any(not value.strip() for value in values):
            raise ValueError("FAILURE_OBSERVATIONS_EMPTY")
        return values


def _decision(
    failure_class: FailureClass,
    reason_code: str,
    scope: Literal["source_closed", "runtime_observed"],
    *observations: str,
) -> FailureDecision:
    return FailureDecision(
        failure_class=failure_class,
        reason_code=reason_code,
        classification_scope=scope,
        decisive_observations=tuple(observations),
    )


def classify_failure_trace(trace: FailureTrace) -> Optional[FailureDecision]:
    """Return the first trustworthy failed layer for one source-closed case."""

    if not trace.source_support_present:
        raise ValueError("FAILURE_SOURCE_SUPPORT_UNCONFIRMED")
    # State corruption is cross-cutting: once observed, downstream stage facts
    # cannot be trusted enough to assign a more specific causal layer.
    if not trace.evidence_state_valid:
        return _decision(
            FailureClass.EVIDENCE_STATE_MISS,
            "evidence_state_invalid",
            "source_closed",
            "evidence_state_valid=false",
        )
    if not trace.compiled_support_present:
        return _decision(
            FailureClass.COMPILE_MISSING,
            "compiled_support_absent",
            "source_closed",
            "source_support_present=true",
            "compiled_support_present=false",
        )
    if (
        not trace.supporting_candidate_present
        or not trace.supporting_evidence_read
    ):
        return _decision(
            FailureClass.RETRIEVAL_MISS,
            "support_not_retrieved_or_read",
            "source_closed",
            "compiled_support_present=true",
            "supporting_candidate_present={}".format(
                str(trace.supporting_candidate_present).lower()
            ),
            "supporting_evidence_read={}".format(
                str(trace.supporting_evidence_read).lower()
            ),
        )
    if (
        not trace.required_reasoning_complete
        or not trace.answer_semantically_correct
    ):
        return _decision(
            FailureClass.REASONING_MISS,
            "reasoning_or_semantic_incorrect",
            "source_closed",
            "supporting_evidence_read=true",
            "required_reasoning_complete={}".format(
                str(trace.required_reasoning_complete).lower()
            ),
            "answer_semantically_correct={}".format(
                str(trace.answer_semantically_correct).lower()
            ),
        )
    if not trace.answer_format_valid:
        return _decision(
            FailureClass.ANSWER_FORMAT_MISS,
            "answer_contract_invalid",
            "source_closed",
            "answer_semantically_correct=true",
            "answer_format_valid=false",
        )
    return None


def classify_runtime_failure(
    signals: RuntimeFailureSignals,
) -> FailureDecision:
    """Classify an incomplete live result using only directly observed facts."""

    if (
        signals.evidence_budget_exhausted
        or signals.controller_answer_rejected
    ):
        return _decision(
            FailureClass.EVIDENCE_STATE_MISS,
            "evidence_budget_or_binding_invalid",
            "runtime_observed",
            "evidence_budget_exhausted={}".format(
                str(signals.evidence_budget_exhausted).lower()
            ),
            "controller_answer_rejected={}".format(
                str(signals.controller_answer_rejected).lower()
            ),
        )
    if signals.grounded_incomplete:
        return _decision(
            FailureClass.REASONING_MISS,
            "grounded_incomplete_observed",
            "runtime_observed",
            "grounded_incomplete=true",
            "successful_read={}".format(str(signals.successful_read).lower()),
        )
    if not signals.successful_read:
        if signals.compiled_supported_claim_count == 0:
            return _decision(
                FailureClass.COMPILE_MISSING,
                "no_supported_compiled_claims",
                "runtime_observed",
                "compiled_supported_claim_count=0",
                "successful_read=false",
            )
        return _decision(
            FailureClass.RETRIEVAL_MISS,
            "no_successful_supporting_read",
            "runtime_observed",
            "compiled_supported_claim_count>0",
            "successful_read=false",
        )
    if signals.required_slots_incomplete:
        return _decision(
            FailureClass.REASONING_MISS,
            "required_control_slot_incomplete",
            "runtime_observed",
            "successful_read=true",
            "required_slots_incomplete=true",
        )
    if signals.stop_reason == "model_declared_incomplete":
        return _decision(
            FailureClass.REASONING_MISS,
            "model_declared_incomplete",
            "runtime_observed",
            "successful_read=true",
            "model_declared_incomplete=true",
        )
    if signals.synthesis_invalid:
        return _decision(
            FailureClass.ANSWER_FORMAT_MISS,
            "clean_synthesis_invalid",
            "runtime_observed",
            "successful_read=true",
            "synthesis_invalid=true",
        )
    if signals.answer_not_produced:
        return _decision(
            FailureClass.ANSWER_FORMAT_MISS,
            "answer_not_produced",
            "runtime_observed",
            "successful_read=true",
            "answer_not_produced=true",
        )
    raise ValueError("RUNTIME_FAILURE_UNCLASSIFIED")


__all__ = [
    "FailureDecision",
    "FailureTrace",
    "RuntimeFailureSignals",
    "classify_failure_trace",
    "classify_runtime_failure",
]
