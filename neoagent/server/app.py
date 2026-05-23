"""FastAPI server for NeoAgent — REST API + WebSocket streaming.

CyberClaw is CLI-only. This server enables:
- REST API for programmatic access
- WebSocket for real-time token streaming
- Session management (create, list, delete)
- OpenAPI docs auto-generated
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from dotenv import load_dotenv
from pathlib import Path

# Ensure .env is loaded from project root before config is cached
_project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_project_root / ".env", override=True)

from ..core.agent import AgentEvent, AgentEventKind, NeoAgent, create_agent
from ..core.config import get_config
from ..core.observability import EventType, Observability, get_observability
from ..core.tools.scheduler import get_scheduler


# ── Request/Response Models ────────────────────────


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    stream: bool = True


class ChatResponse(BaseModel):
    session_id: str
    response: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class SessionInfo(BaseModel):
    id: str
    message_count: int = 0
    created_at: str = ""


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None


# ── Session Store ──────────────────────────────────


class SessionStore:
    """In-memory session store with agent instance management."""

    def __init__(self) -> None:
        self._agents: dict[str, NeoAgent] = {}
        self._metadata: dict[str, dict] = {}

    def create(self, session_id: str | None = None, provider: str | None = None, model: str | None = None) -> str:
        sid = session_id or str(uuid.uuid4())[:12]
        if sid in self._agents:
            return sid  # Already exists

        import time
        agent = NeoAgent(provider=provider, model=model, session_id=sid)
        self._agents[sid] = agent
        self._metadata[sid] = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "message_count": 0,
        }
        return sid

    def get(self, session_id: str) -> NeoAgent:
        if session_id not in self._agents:
            raise KeyError(f"Session '{session_id}' not found")
        return self._agents[session_id]

    def delete(self, session_id: str) -> bool:
        if session_id in self._agents:
            del self._agents[session_id]
            self._metadata.pop(session_id, None)
            return True
        return False

    def list_sessions(self) -> list[SessionInfo]:
        return [
            SessionInfo(id=sid, **meta)
            for sid, meta in self._metadata.items()
        ]

    def increment_messages(self, session_id: str) -> None:
        if session_id in self._metadata:
            self._metadata[session_id]["message_count"] += 1


# ── App Factory ────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    cfg = get_config()
    obs = get_observability()
    scheduler = get_scheduler()
    scheduler.start()
    obs.log(EventType.SESSION_START, session_id="server", server_port=cfg.server.port)
    yield
    obs.log(EventType.SESSION_END, session_id="server")
    scheduler.shutdown(wait=False)
    obs.shutdown()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    cfg = get_config()
    app = FastAPI(
        title="NeoAgent API",
        description="Next-gen AI agent framework — async streaming, multi-agent, sandboxed execution",
        version="2.0.0",
        lifespan=lifespan,
    )

    origins = [o.strip() for o in cfg.server.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Optional API key middleware
    @app.middleware("http")
    async def api_key_middleware(request: Request, call_next):
        if cfg.server.api_key:
            public_paths = {"/docs", "/openapi.json", "/health"}
            if request.url.path not in public_paths:
                auth = request.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] != cfg.server.api_key:
                    return JSONResponse(
                        {"error": "Unauthorized"}, status_code=401
                    )
        return await call_next(request)

    store = SessionStore()
    obs = get_observability()

    # ── REST Endpoints ─────────────────────────────

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest):
        """Send a message and get a response (streaming or non-streaming)."""
        sid = store.create(req.session_id, req.provider, req.model)
        agent = store.get(sid)
        store.increment_messages(sid)

        obs.log(EventType.LLM_INPUT, session_id=sid, message=req.message[:200])

        if req.stream:
            # Return as server-sent events
            async def event_stream():
                full_response = []
                async for event in agent.stream(req.message, sid):
                    if event.kind == AgentEventKind.TOKEN:
                        full_response.append(str(event.data))
                        yield f"data: {json.dumps({'type': 'token', 'content': event.data})}\n\n"
                    elif event.kind == AgentEventKind.TOOL_CALL_START:
                        yield f"data: {json.dumps({'type': 'tool_start', 'data': event.data})}\n\n"
                    elif event.kind == AgentEventKind.TOOL_CALL_END:
                        yield f"data: {json.dumps({'type': 'tool_end', 'data': event.data})}\n\n"
                    elif event.kind == AgentEventKind.ERROR:
                        yield f"data: {json.dumps({'type': 'error', 'content': str(event.data)})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'content': ''.join(full_response)})}\n\n"

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={"X-Session-Id": sid},
            )

        # Non-streaming
        response_text = await agent.invoke(req.message, sid)
        return ChatResponse(session_id=sid, response=response_text)

    @app.get("/sessions", response_model=list[SessionInfo])
    async def list_sessions():
        """List all active sessions."""
        return store.list_sessions()

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str):
        """Delete a session and its history."""
        if store.delete(session_id):
            return {"status": "deleted", "session_id": session_id}
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    @app.get("/sessions/{session_id}/history")
    async def get_history(session_id: str):
        """Get conversation history for a session."""
        try:
            agent = store.get(session_id)
            messages = await agent.get_history(session_id)
            return {
                "session_id": session_id,
                "message_count": len(messages),
                "messages": [
                    {"type": m.type, "content": str(m.content)[:500]}
                    for m in messages
                ],
            }
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    @app.get("/metrics")
    async def get_metrics():
        """Get usage metrics and cost summary."""
        return obs.get_cost_summary()

    @app.get("/health")
    async def health():
        return {"status": "healthy", "version": "2.0.0"}

    # ── WebSocket ───────────────────────────────────

    @app.websocket("/ws/{session_id}")
    async def websocket_endpoint(ws: WebSocket, session_id: str):
        """WebSocket for real-time bidirectional streaming."""
        await ws.accept()

        try:
            agent = store.get(session_id)
        except KeyError:
            store.create(session_id)
            agent = store.get(session_id)

        try:
            while True:
                data = await ws.receive_text()
                request = json.loads(data)
                message = request.get("message", "")

                if not message:
                    await ws.send_json({"type": "error", "content": "Empty message"})
                    continue

                store.increment_messages(session_id)
                obs.log(EventType.LLM_INPUT, session_id=session_id, message=message[:200])

                async for event in agent.stream(message, session_id):
                    if event.kind == AgentEventKind.TOKEN:
                        await ws.send_json({"type": "token", "content": event.data})
                    elif event.kind == AgentEventKind.TOOL_CALL_START:
                        await ws.send_json({"type": "tool_start", "data": event.data})
                    elif event.kind == AgentEventKind.TOOL_CALL_END:
                        await ws.send_json({"type": "tool_end", "data": event.data})
                    elif event.kind == AgentEventKind.MESSAGE_COMPLETE:
                        await ws.send_json({"type": "done", "data": event.data})
                    elif event.kind == AgentEventKind.ERROR:
                        await ws.send_json({"type": "error", "content": event.data})

        except WebSocketDisconnect:
            pass
        except Exception as e:
            await ws.send_json({"type": "error", "content": str(e)})

    return app


def main():
    """Entry point: neoagent-server command."""
    import uvicorn

    cfg = get_config()
    app = create_app()

    print(f" NeoAgent Server v2.0.0")
    print(f"   Host: {cfg.server.host}:{cfg.server.port}")
    print(f"   Docs: http://{cfg.server.host}:{cfg.server.port}/docs")
    print(f"   WS:   ws://{cfg.server.host}:{cfg.server.port}/ws/{{session_id}}")

    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
