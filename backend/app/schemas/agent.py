"""Schemas for invoking the standalone Accoya agent over HTTP."""

from typing import Any

from pydantic import BaseModel, Field

from agent.models import (
    GenerationResult,
    NormalizedLead,
    NurturingRoute,
    ProductSelection,
    RoutingHint,
    StrategyChunk,
)


class AgentGenerateRequest(BaseModel):
    lead: dict[str, Any] = Field(
        ..., description="Raw lead mapping passed directly to the agent workflow."
    )


class AgentNormalizeResponse(BaseModel):
    normalized_lead: NormalizedLead


class AgentRoutingResponse(BaseModel):
    normalized_lead: NormalizedLead
    routing_hints: list[RoutingHint]


class AgentTraceResponse(BaseModel):
    normalized_lead: NormalizedLead
    routing_hints: list[RoutingHint]
    selection: ProductSelection
    nurturing_route: NurturingRoute
    strategy_chunks: list[StrategyChunk]
    nurturing_chunks: list[StrategyChunk]
    warnings: list[str]
    error: str | None = None
    result: GenerationResult
