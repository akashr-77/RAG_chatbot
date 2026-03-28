import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Sequence, TypedDict

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel

load_dotenv()

# ── Globals (set once at startup, reused for every request) ──────────────────
_app_graph = None   # compiled LangGraph app
_checkpointer = None  # MemorySaver — holds all conversation threads in RAM
_mcp_client = None  # MultiServerMCPClient kept alive for the app lifetime

SYSTEM_PROMPT = """
You are a helpful AI assistant backed by a private knowledge base
that contains documents, resumes, reports, and other files.

RULES — follow these strictly:
1. For ANY question that requires specific facts, names, skills,
   dates, or information of any kind — you MUST call the
   answer_question tool. No exceptions.
2. NEVER answer factual questions from your own training knowledge.
   The knowledge base is your only source of truth.
3. If the tool returns empty or insufficient context, say so honestly.
   Do not fill gaps with your own assumptions.
4. Only answer from your own knowledge for casual conversation
   (greetings, clarifications, meta questions about yourself).

When in doubt — use the tool.
"""


# ── Agent state ───────────────────────────────────────────────────────────────
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


# ── FastAPI lifespan — builds the agent once when the server starts ───────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once at startup. Connects to the MCP RAG server, builds the
    LangGraph agent, and compiles it with a MemorySaver checkpointer.
    The MCP client stays open for the entire server lifetime (SSE transport).
    """
    global _app_graph, _checkpointer, _mcp_client

    google_api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not google_api_key:
        raise ValueError("GOOGLE_API_KEY not found in .env")

    mcp_config = {
        "rag": {"url": "http://127.0.0.1:8000/sse", "transport": "sse"}
    }

    _mcp_client = MultiServerMCPClient(mcp_config)
    all_tools = await _mcp_client.get_tools()
    print(f"[api_server] Connected to RAG server. Tools: {[t.name for t in all_tools]}")

    llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        google_api_key=google_api_key,
    )
    model = llm.bind_tools(all_tools)

    async def call_model(state: AgentState):
        system_prompt = SystemMessage(content=SYSTEM_PROMPT)
        response = await model.ainvoke([system_prompt] + list(state["messages"]))
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(all_tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")

    _checkpointer = MemorySaver()
    _app_graph = graph.compile(checkpointer=_checkpointer)

    print("[api_server] LangGraph agent ready.")
    yield

    print("[api_server] Shutting down.")


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(lifespan=lifespan)

# Allow React dev server to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request model ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    thread_id: str  # each conversation has its own UUID


# ── Helper: extract clean text from an AIMessage ─────────────────────────────
def extract_text(message: AIMessage) -> str:
    """Strips Gemini's extras/signatures, returns only the text content."""
    content = message.content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ]
        return "\n".join(parts).strip()
    return str(content).strip()


# ── SSE streaming chat endpoint ───────────────────────────────────────────────
@app.post("/api/chat")
async def chat(request: ChatRequest):
    """
    Streams the agent's response as Server-Sent Events.
    The frontend reads these events to show status updates and the final answer.

    Event types:
      {"type": "status", "text": "..."}   — intermediate status (tool running)
      {"type": "token",  "text": "..."}   — final answer text
      {"type": "done"}                    — stream complete
      {"type": "error",  "text": "..."}   — something went wrong
    """
    async def generate():
        try:
            inputs = {"messages": [HumanMessage(content=request.message)]}
            config = {"configurable": {"thread_id": request.thread_id}}

            # stream_mode="updates" gives us one dict per node that ran
            async for event in _app_graph.astream(inputs, config=config, stream_mode="updates"):

                # tools node ran → RAG is working
                if "tools" in event:
                    yield f"data: {json.dumps({'type': 'status', 'text': 'Searching knowledge base...'})}\n\n"

                # agent node ran → check if it's the final response (no tool calls)
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
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disables nginx buffering if deployed
        },
    )


# ── New thread endpoint ────────────────────────────────────────────────────────
@app.get("/api/new_thread")
async def new_thread():
    """Returns a fresh UUID for a new conversation."""
    return {"thread_id": str(uuid.uuid4())}


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="127.0.0.1", port=8001, reload=False)