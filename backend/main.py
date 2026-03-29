"""
backend/main.py — FastAPI HTTP server.

Responsibilities:
  - define /api/chat (SSE streaming) and /api/new_thread
  - manage CORS for the frontend origin
  - own the lifespan: open MCP client, build agent, clean up on shutdown
  - extract clean text from Gemini responses
  - stream SSE events to the browser

This file does not know about ChromaDB, embeddings, or LangGraph internals.
"""

import json
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel

from agent import MCP_CONFIG, build_agent

# ── Globals set once at startup ───────────────────────────────────────────────
_agent_app   = None   # compiled LangGraph app
_mcp_client  = None   # kept alive so subprocesses stay alive

# ── FastAPI lifespan — builds the agent once when the server starts ───────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once when FastAPI starts.

    Opens the MCP client as a context manager — this spawns all stdio
    subprocesses (rag_server.py etc.) and keeps them alive.

    Builds the LangGraph agent once. From this point every request
    uses the already-compiled graph and already-loaded embedding model.

    On shutdown (Ctrl+C): the async with block exits, MCP client closes,
    subprocesses are killed cleanly.
    """
    global _agent_app, _mcp_client

    print("Starting up FastAPI server and building agent...")

    # Open MCP client (spawns subprocesses, keeps them alive)
    client = MultiServerMCPClient(MCP_CONFIG)
    _mcp_client = client  # keep alive for the lifespan of the app
    _agent_app, _ = await build_agent(client)
    print("Agent built successfully! Ready to accept requests.")
    yield  # this is where the server actually starts

    print("Shutting down FastAPI server and cleaning up resources...")


    #FastAPI APP
app = FastAPI(lifespan=lifespan)

# CORS middleware to allow requests from the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # frontend origin
    allow_methods=["*"],
    allow_headers=["*"],
)

#Request model 
class ChatRequest(BaseModel):
    thread_id: str
    message: str

#Extrating clean text from Gemini responses
def extract_text(message: AIMessage) -> str:
    """
    Gemini wraps content in a list of blocks with type/text/extras.
    The 'extras' field contains a cryptographic signature we discard.
    This returns plain text only.
    """
    content = message.content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and block.get("text")
        ).strip()
    return str(content).strip()

#SSE streaming endpoint
@app.post("/api/chat")
async def chat(request: ChatRequest):
    """
    Streams the agent response as Server-Sent Events.

    Event types the frontend receives:
      {"type": "status", "text": "..."}  — tool is running (e.g. "Searching knowledge base...")
      {"type": "token",  "text": "..."}  — final answer text
      {"type": "done"}                   — stream complete, frontend re-enables input
      {"type": "error",  "text": "..."}  — something failed
    """
    async def generate():
        try:
            inputs = {"messages": [HumanMessage(content=request.message)]}
            config = {"configurable": {"thread_id": request.thread_id}}

            # stream_mode="updates" gives one dict per node that ran.
            # We check which node produced each update and act accordingly.
            async for event in _agent_app.astream(
                inputs, config=config, stream_mode="updates"
            ):
                # tools node ran → emit a status so the UI shows activity
                if "tools" in event:
                    yield f"data: {json.dumps({'type': 'status', 'text': 'Searching knowledge base...'})}\n\n"

                # agent node ran → check if this is the final answer
                # (final = AIMessage with no tool_calls)
                if "agent" in event:
                    msgs = event["agent"]["messages"]
                    last = msgs[-1]
                    if isinstance(last, AIMessage) and not last.tool_calls:
                        text = extract_text(last)
                        if text:
                            yield f"data: {json.dumps({'type': 'token', 'text': text})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # disables nginx buffering if ever deployed
        },
    )

#New thread endpoint
@app.get("/api/new_thread")
async def new_thread():
    """
    Returns a fresh UUID for a new conversation.
    The frontend passes this thread_id with every message.
    MemorySaver uses it to isolate conversation history.
    """
    return {"thread_id": str(uuid.uuid4())}

#Entry point 
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=False)