"""
backend/agent.py — LangGraph agent with MCP tools.

Responsibilities:
  - spawn MCP server subprocesses via stdio
  - bind their tools to the Gemini model
  - compile the LangGraph ReAct graph with MemorySaver
  - expose get_app() for FastAPI to consume

FastAPI does not know what MCP is.
This file does not know what FastAPI is.
"""

import os
from pathlib import Path
from typing import Annotated, AsyncIterator, Sequence, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

load_dotenv()

# Offline mode — prevents HuggingFace network checks on every startup
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

SERVERS_DIR = Path(__file__).resolve().parent / "servers"

# ── MCP server config ─────────────────────────────────────────────────────────
# Each entry is a stdio subprocess. MultiServerMCPClient spawns them
# automatically when the context manager opens. No ports. No terminals.
# To add a new tool: add one line here and create the server file.
MCP_CONFIG = {
    "rag": {
        "command": "python",
        "args":    [str(SERVERS_DIR / "rag_server.py")],
        "transport": "stdio",
    },
    # "search": {
    #     "command": "python",
    #     "args":    [str(SERVERS_DIR / "search_server.py")],
    #     "transport": "stdio",
    # },
}

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

#Agent state 
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]

#Build Functions
async def build_agent(client: MultiServerMCPClient):
    """
    Builds and compiles the LangGraph agent.

    Called once inside the FastAPI lifespan after the MCP client
    has opened its connections. Returns the compiled app and
    the MemorySaver checkpointer.

    The MCP client is passed in (not created here) so the caller
    (main.py lifespan) controls its lifetime — the async with block
    in main.py keeps the subprocesses alive for the whole server session.
    """
    google_api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not google_api_key:
        raise ValueError("GOOGLE_API_KEY not set in .env")

    all_tools = await client.get_tools()
    print(f"[agent] loaded {len(all_tools)} tool(s): {[t.name for t in all_tools]}")

    llm   = ChatGoogleGenerativeAI(model="gemini-2.5-flash", google_api_key=google_api_key)
    model = llm.bind_tools(all_tools)

    async def call_model(state: AgentState):
        prompt   = SystemMessage(content=SYSTEM_PROMPT)
        response = await model.ainvoke([prompt] + list(state["messages"]))
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(all_tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")

    checkpointer = MemorySaver()
    app          = graph.compile(checkpointer=checkpointer)

    print("[agent] LangGraph graph compiled and ready.")
    return app, checkpointer