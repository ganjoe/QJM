"""
WorkItem submission, job streaming, and OpenAI-compatible endpoints.

Core API surface for agents interacting with the LLM Router.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from engine import pool_manager, sse_manager
from engine.models import WorkItem, Job, SubmitResponse, JobStatusResponse, RoutingConfig, WorkItemPayload

router = APIRouter(tags=["workitems"])


# ------------------------------------------------------------------
# FR-1.1 / FR-1.2 — WorkItem submission
# ------------------------------------------------------------------

@router.post("/v1/submit", response_model=SubmitResponse)
async def submit_workitem(workitem: WorkItem):
    """
    Submit a WorkItem for asynchronous processing.

    The router immediately returns a ``job_id`` and a ``stream_url``.
    The actual LLM inference is queued and processed in the background.
    """
    job = Job(workitem=workitem)
    # TODO: Persist to Supabase

    await pool_manager.submit(job)

    return SubmitResponse(
        job_id=job.job_id,
        stream_url=f"/v1/jobs/{job.job_id}/stream",
        status="queued",
    )


# ------------------------------------------------------------------
# FR-1.3 — SSE streaming
# ------------------------------------------------------------------

@router.get("/v1/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    """
    Server-Sent Events stream for a job's results.

    Event types emitted:
      - ``status``  — lifecycle changes (queued → running → waiting)
      - ``chunk``   — token delta from the LLM
      - ``error``   — error description
      - ``done``    — stream complete, no more events
    """
    job = pool_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return StreamingResponse(
        sse_manager.stream(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # Disable nginx buffering
        },
    )


# ------------------------------------------------------------------
# Polling alternative
# ------------------------------------------------------------------

@router.get("/v1/jobs/{job_id}/status", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    """Get current status of a job (polling alternative to SSE)."""
    job = pool_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        assigned_endpoint=job.assigned_endpoint,
        error=job.error,
    )


# ------------------------------------------------------------------
# FR-4.1 — OpenAI-compatible proxy endpoint
# ------------------------------------------------------------------

@router.post("/v1/chat/completions")
async def openai_compatible_completions(request_body: dict):
    """
    OpenAI-compatible ``/v1/chat/completions`` endpoint.

    Maps ``model`` → ``capability_class`` and transparently creates a
    WorkItem + Job internally.  Supports ``stream: true`` for SSE delivery.
    This enables drop-in compatibility with OpenAI SDKs.
    """
    messages = request_body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="'messages' field is required")

    capability = request_body.get("model", "fast")

    workitem = WorkItem(
        routing=RoutingConfig(capability_class=capability),
        payload=WorkItemPayload(
            messages=messages,
            temperature=request_body.get("temperature", 0.7),
            max_tokens=request_body.get("max_tokens", 2048),
            tools=request_body.get("tools", []),
            stream=request_body.get("stream", True),
            model=request_body.get("model"),
        ),
    )

    job = Job(workitem=workitem)
    await pool_manager.submit(job)

    if request_body.get("stream", True):
        return StreamingResponse(
            sse_manager.stream(job.job_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    # Non-streaming: collect full response
    # TODO: Implement non-streaming response aggregation
    return {
        "error": "Non-streaming mode not yet implemented. Use stream: true.",
        "job_id": job.job_id,
        "stream_url": f"/v1/jobs/{job.job_id}/stream",
    }


# ------------------------------------------------------------------
# Pool stats (for dashboard)
# ------------------------------------------------------------------

@router.get("/v1/pools")
async def get_pool_stats():
    """Return queue depths and worker status for all capability pools."""
    return {
        "pools": pool_manager.get_pool_stats(),
        "total_jobs": pool_manager.total_jobs,
        "active_streams": sse_manager.active_streams,
    }
