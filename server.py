"""Web server for WallaBot: serves the chat UI and runs the agent over a websocket.

Run it with:

    .venv/bin/python -m uvicorn server:app --reload

Then open http://localhost:8000 . Each browser tab gets its own agent session.
The user types a request (and optional budget); the server streams the agent's
text, activity logs, requirements, and board updates back to the page in real time.
"""

import asyncio
import json
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import agent
import geo

app = FastAPI()
BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")


@app.get("/")
async def index():
    """Serve the single-page chat UI."""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/logo.png")
async def logo():
    """Serve the WallaBot logo used in the UI header."""
    return FileResponse(os.path.join(BASE_DIR, "logos", "wallabot.png"))


@app.get("/api/sessions")
async def sessions():
    """Return the list of past sessions (newest first) for the history sidebar."""
    return agent.list_sessions()


@app.get("/api/provinces")
async def provinces():
    """Return the list of Spanish provinces for the "Provincia" autocomplete."""
    return geo.list_provinces()


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """Handle one chat session over a websocket.

    Protocol:
        Client -> server: {"text": "...", "budget": 1000 | null, "model": "..."}
                           or {"type": "stop"} to cancel the running turn.
        Server -> client: event objects with a "type" field
            (session, status, text_delta, message_done, log, requirements, board).

    The turn runs as a background task so the receive loop can keep reading — that's
    what lets a "stop" message cancel an in-progress search.
    """
    await websocket.accept()

    async def emit(event):
        """Forward an agent event to the browser."""
        await websocket.send_json(event)

    # Resume an existing session if the client asked for one (?session=<id>),
    # otherwise start fresh.
    requested = websocket.query_params.get("session")
    session = agent.load_session(requested) if requested else None
    if session is None:
        session = agent.new_session()

    await emit({"type": "session", "id": session.id, "title": session.title,
                "place": session.user_place})

    # On resume, rebuild the page: prior chat, requirements and board (each part
    # independently, so a session with a board but no saved messages still shows).
    if requested:
        chat = agent.chat_turns(session.messages, session.models)
        if chat:
            await emit({"type": "restore", "chat": chat})
        if session.requirements:
            await emit({
                "type": "requirements",
                "requirements": session.requirements,
                "budget": session.budget,
                "summary": "",
            })
        if session.board.get("options"):
            await emit({"type": "board", "board": session.board})

    run_task = None  # the currently running turn, if any

    async def run(text, model):
        """Run one turn; on user cancellation, report it and re-enable the UI."""
        try:
            await agent.run_turn(session, text, emit, model=model)
        except asyncio.CancelledError:
            try:
                await emit({"type": "log", "text": "⏹ Búsqueda detenida por el usuario."})
                await emit({"type": "status", "state": "idle"})
            except Exception:
                pass
        except Exception:
            pass  # run_turn handles its own errors; this is just a safety net

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue  # malformed client message; ignore and keep the socket open

            if msg.get("type") == "stop":
                if run_task and not run_task.done():
                    run_task.cancel()
                continue

            text = (msg.get("text") or "").strip()
            if not text:
                continue
            if run_task and not run_task.done():
                continue  # a turn is already running (the UI blocks this too)

            # Location from the "Provincia" field: geocode it onto the session so
            # searches are centered/filtered near the user.
            location = (msg.get("location") or "").strip()
            if location and location != session.user_place:
                session.set_location(location)

            budget = msg.get("budget")
            if budget:
                text += f"\n\n(My budget is €{budget}.)"
            # Resolve the model chosen in the UI dropdown (defaults to sonnet).
            model = agent.MODELS.get(msg.get("model"), agent.MODEL)
            # Run in the background so we can keep reading (e.g. a "stop" message).
            run_task = asyncio.create_task(run(text, model))
    except WebSocketDisconnect:
        if run_task and not run_task.done():
            run_task.cancel()
