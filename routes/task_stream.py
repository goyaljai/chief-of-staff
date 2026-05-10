"""GET /task/{id}/stream — Server-Sent Events live event tail.

Subscribes to the task's STORE-internal asyncio.Queue. Streams every log
entry as it lands, plus a 15s heartbeat so reverse proxies don't kill idle
connections, plus a final 'terminal' event when the task lands in done/
failed/abandoned/cancelled so clients can disconnect cleanly.
"""
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from persistence import STORE


task_stream_router = APIRouter(tags=["task"])


@task_stream_router.get("/task/{task_id}/stream")
async def stream_task(task_id: str, request: Request):
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")

    async def event_gen():
        q = STORE.subscribe(task_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield {"event": "log", "data": json.dumps(entry, default=str)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"ts": time.time()})}
                cur = STORE.get(task_id)
                if cur and cur.status in ("done", "failed", "abandoned", "cancelled"):
                    yield {"event": "terminal", "data": json.dumps({"status": cur.status})}
                    break
        finally:
            STORE.unsubscribe(task_id, q)

    return EventSourceResponse(event_gen())
