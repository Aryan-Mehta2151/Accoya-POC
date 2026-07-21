"""HTTP wrapper around the standalone Accoya email agent."""

from copy import deepcopy

from fastapi import APIRouter, Depends, HTTPException

from agent import AccoyaEmailAgent
from agent.models import GenerationResult, GenerationTelemetry
from agent.normalization import normalize_lead
from agent.observability import finish_telemetry, monotonic_ms
from agent.prompts import PROMPT_VERSION
from agent.routing import get_routing_hints
from app.schemas.agent import (
    AgentGenerateRequest,
    AgentNormalizeResponse,
    AgentRoutingResponse,
    AgentTraceResponse,
)
from app.services.email_generator import get_accoya_email_agent


router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/generate", response_model=GenerationResult)
def generate_with_agent(
    payload: AgentGenerateRequest,
    email_agent: AccoyaEmailAgent = Depends(get_accoya_email_agent),
) -> GenerationResult:
    """Run the isolated agent workflow for one raw lead record."""
    try:
        return email_agent.generate(payload.lead)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Agent execution failed") from exc


@router.post("/normalize", response_model=AgentNormalizeResponse)
def normalize_agent_input(payload: AgentGenerateRequest) -> AgentNormalizeResponse:
    """Return deterministic lead normalization output without provider calls."""
    try:
        normalized = normalize_lead(payload.lead)
        return AgentNormalizeResponse(normalized_lead=normalized)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/routing-hints", response_model=AgentRoutingResponse)
def get_agent_routing_hints(payload: AgentGenerateRequest) -> AgentRoutingResponse:
    """Return normalized lead plus deterministic catalog routing hints."""
    try:
        normalized = normalize_lead(payload.lead)
        hints = get_routing_hints(normalized)
        return AgentRoutingResponse(normalized_lead=normalized, routing_hints=hints)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/trace", response_model=AgentTraceResponse)
def trace_agent_steps(
    payload: AgentGenerateRequest,
    email_agent: AccoyaEmailAgent = Depends(get_accoya_email_agent),
) -> AgentTraceResponse:
    """Run the workflow and return available stage output for debugging."""
    try:
        started_ms = monotonic_ms()

        initial_state = {
            "original_lead": deepcopy(dict(payload.lead)),
            "strategy_chunks": [],
            "warnings": [],
            "error": None,
            "telemetry": GenerationTelemetry(
                model_name=email_agent._model.model_name,
                prompt_version=PROMPT_VERSION,
            ),
        }
        state = email_agent.graph.invoke(initial_state)

        result = state.get("result")
        if result is None:
            raise HTTPException(status_code=500, detail="Agent trace did not produce a result")

        telemetry = finish_telemetry(result.telemetry, run_started_ms=started_ms)
        result = result.model_copy(update={"telemetry": telemetry})

        return AgentTraceResponse(
            normalized_lead=state["normalized_lead"],
            routing_hints=state["routing_hints"],
            selection=state["selection"],
            nurturing_route=state.get("nurturing_route"),
            strategy_chunks=state.get("strategy_chunks", []),
            nurturing_chunks=state.get("nurturing_chunks", []),
            warnings=state.get("warnings", []),
            error=state.get("error"),
            result=result,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Agent trace execution failed") from exc
