"""Run-local telemetry helpers and conservative structured logging."""

from __future__ import annotations

import logging
from time import perf_counter

from .models import GenerationResult, GenerationTelemetry, TokenUsage


logger = logging.getLogger("accoya_email_agent")


def monotonic_ms() -> int:
    """Return a monotonic millisecond counter suitable for durations."""

    return int(perf_counter() * 1000)


def add_usage(
    telemetry: GenerationTelemetry,
    usage: TokenUsage,
    *,
    model_call: bool = True,
) -> GenerationTelemetry:
    """Return telemetry with provider usage and model-call count accumulated."""

    current = telemetry.token_usage
    combined = TokenUsage(
        input_tokens=_sum_optional(current.input_tokens, usage.input_tokens),
        output_tokens=_sum_optional(current.output_tokens, usage.output_tokens),
        total_tokens=_sum_optional(current.total_tokens, usage.total_tokens),
        cached_input_tokens=_sum_optional(
            current.cached_input_tokens, usage.cached_input_tokens
        ),
    )
    return telemetry.model_copy(
        update={
            "token_usage": combined,
            "model_calls": telemetry.model_calls + int(model_call),
        }
    )


def record_node_timing(
    telemetry: GenerationTelemetry,
    node_name: str,
    started_ms: int,
) -> GenerationTelemetry:
    """Return telemetry with one bounded node duration recorded."""

    timings = dict(telemetry.node_timings_ms)
    timings[node_name] = max(0, monotonic_ms() - started_ms)
    return telemetry.model_copy(update={"node_timings_ms": timings})


def finish_telemetry(
    telemetry: GenerationTelemetry,
    *,
    run_started_ms: int,
) -> GenerationTelemetry:
    """Return terminal telemetry with total latency populated."""

    return telemetry.model_copy(
        update={"latency_ms": max(0, monotonic_ms() - run_started_ms)}
    )


def log_result(result: GenerationResult) -> None:
    """Log non-content generation metadata without configuring global handlers."""

    telemetry = result.telemetry
    logger.info(
        "accoya_email_generation_completed",
        extra={
            "lead_id": result.lead_id,
            "generation_status": result.status.value,
            "selected_product_family": result.selected_product_family,
            "selected_application": result.selected_application,
            "nurturing_email_number": result.nurturing_email_number,
            "retrieval_count": telemetry.retrieval_count,
            "retrieved_document_ids": telemetry.retrieved_document_ids,
            "prompt_version": result.prompt_version,
            "model_name": telemetry.model_name,
            "latency_ms": telemetry.latency_ms,
            "input_tokens": telemetry.token_usage.input_tokens,
            "output_tokens": telemetry.token_usage.output_tokens,
            "total_tokens": telemetry.token_usage.total_tokens,
            "cached_input_tokens": telemetry.token_usage.cached_input_tokens,
        },
    )


def _sum_optional(first: int | None, second: int | None) -> int | None:
    if first is None and second is None:
        return None
    return (first or 0) + (second or 0)


__all__ = [
    "add_usage",
    "finish_telemetry",
    "log_result",
    "monotonic_ms",
    "record_node_timing",
]
