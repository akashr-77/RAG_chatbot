# RAG Chatbot — Complete System Design

---

## Table of Contents

1. [What this system does](#1-what-this-system-does)
2. [Directory structure](#2-directory-structure)
3. [Architecture decisions and reasons](#3-architecture-decisions-and-reasons)
4. [How the pieces connect](#4-how-the-pieces-connect)
5. [Embedding model and memory lifecycle](#5-embedding-model-and-memory-lifecycle)
6. [Complete code — all files](#6-complete-code--all-files)
   - [preload.py](#61-preloadpy)
   - [start.py](#62-startpy)
   - [backend/agent.py](#63-backendagentpy)
   - [backend/main.py](#64-backendmainpy)
   - [backend/servers/rag_server.py](#65-backendserversrag_serverpy)
   - [frontend/vite.config.js](#66-frontendviteconfigjs)
   - [frontend/index.html](#67-frontendindexhtml)
   - [frontend/src/main.jsx](#68-frontendsrcmainjsx)
   - [frontend/src/App.jsx](#69-frontendsrcappjsx)
   - [frontend/src/App.css](#610-frontendsrcappcss)
7. [How to run](#7-how-to-run)
8. [Adding new MCP tools in the future](#8-adding-new-mcp-tools-in-the-future)
9. [What each design choice protects against](#9-what-each-design-choice-protects-against)

---

## 1. What this system does

A local Retrieval-Augmented Generation chatbot. You put documents in a folder.
The system embeds them once into a vector database. From then on, every user
question goes through a LangGraph agent that decides to search the knowledge
base and returns a grounded answer — streamed live to a React frontend.

```
User types question
    → React frontend
    → FastAPI backend (streams SSE)
    → LangGraph agent
    → MCP tool call (answer_question)
    → rag_server.py subprocess (stdio, no port)
    → ChromaDB similarity search
    → Gemini generates answer
    → streamed back to browser
```

---

## 2. Directory structure

```
RAG_chatbot/
│
├── data_files/                  ← put your .pdf / .txt / .docx here
│
├── vector_database/             ← ChromaDB writes here (auto-created by preload.py)
│
├── preload.py                   ← run ONCE manually to build the vector store
├── start.py                     ← run every time: starts backend + frontend together
├── .env                         ← GOOGLE_API_KEY and other secrets
│
├── backend/
│   ├── main.py                  ← FastAPI only: HTTP routes, CORS, SSE streaming
│   ├── agent.py                 ← LangGraph agent: MCP client, graph, MemorySaver
│   └── servers/
│       └── rag_server.py        ← MCP stdio server: retrieval only, no ingestion
│
└── frontend/
    ├── vite.config.js
    ├── index.html
    └── src/
        ├── main.jsx
        ├── App.jsx
        └── App.css
```

---

## 3. Architecture decisions and reasons

### Decision 1 — stdio transport for MCP, not SSE

**What was wrong with SSE transport:**

SSE (Server-Sent Events as MCP transport) requires each MCP server to run as
its own HTTP server on its own port:

```
rag_server.py    → port 8000
search_server.py → port 8001   ← conflicts with FastAPI
calc_server.py   → port 8002
```

You would need a separate terminal for each server, pick ports manually, and
make sure nothing conflicts. As you add more tools this becomes unmanageable.

**Why stdio is better:**

With stdio, `MultiServerMCPClient` spawns each server as a subprocess using
stdin/stdout pipes. No ports. No terminals. The servers are hidden child
processes owned by the backend.

```python
mcp_config = {
    "rag":    {"command": "python", "args": ["backend/servers/rag_server.py"], "transport": "stdio"},
    "search": {"command": "python", "args": ["backend/servers/search_server.py"], "transport": "stdio"},
    "calc":   {"command": "python", "args": ["backend/servers/calc_server.py"], "transport": "stdio"},
}
```

Adding a new tool is one line in this dict and a new file in `servers/`.
No port management. No extra terminal.

**The embedding model loads exactly once either way.** The subprocess stays
alive for the entire session. It does not restart per query.

---

### Decision 2 — preload.py runs ingestion once, forever

**What was wrong:**

Previously, `rag_server.py` ran ingestion at startup (inside the MCP
lifespan). This meant every time the backend restarted it would check the
database, find it populated, and skip — but that check still touched disk.
More importantly, ingestion logic (loading files, splitting, embedding) was
permanently tangled into the retrieval server.

**Why preload is better:**

`preload.py` is a standalone script you run one time. It builds `vector_database/`
on disk. After that, `rag_server.py` is a pure retrieval server — it connects
to an already-built database and queries it. Startup becomes instant because
no file loading, splitting, or embedding ever happens at runtime.

```
FIRST TIME (once, ever):
  python preload.py
      loads data_files/ → splits → embeds → writes to vector_database/

EVERY RUN AFTER (as many times as you want):
  python start.py
      rag_server.py starts → connects to existing vector_database/ → ready
      zero file loading, zero embedding, zero ingestion
```

If you ever add new documents to `data_files/`, run `python preload.py --rebuild`
and it wipes and rebuilds the index. Your choice, explicit, not automatic.

---

### Decision 3 — backend split into main.py and agent.py

**What was wrong:**

`api_server.py` contained everything: FastAPI routes, MCP client setup,
LangGraph graph, MemorySaver, Gemini model, CORS config, SSE streaming.
Editing a route meant opening the same file that configures tools. Editing
tool logic meant touching HTTP concerns.

**Why the split is better:**

`main.py` has one job: HTTP. It knows about routes, CORS, SSE streaming,
request/response shapes. It imports a single `get_agent_app()` function
from `agent.py` and calls it. It has no idea what MCP is.

`agent.py` has one job: the agent. It knows about MCP servers, LangGraph,
MemorySaver, Gemini model binding. It has no idea what FastAPI is.

When you add a new tool: edit `agent.py` only.
When you change an API route: edit `main.py` only.
The two files never need to change together.

---

### Decision 4 — one start.py, one command

**What was wrong:**

Three terminals. The user had to remember the order (rag_server first, then
api_server, then frontend), which one was which, and Ctrl+C all three
separately.

**Why start.py is better:**

`start.py` launches the backend and the Vite dev server as subprocesses.
The backend itself spawns the MCP servers internally. From the user's
perspective: one command starts everything, one Ctrl+C stops everything.

```
python start.py
    ├── subprocess: python backend/main.py    (which spawns MCP servers internally)
    └── subprocess: npm run dev               (frontend)
```

---

### Decision 5 — rag_server.py is retrieval only

`rag_server.py` exposes exactly one MCP tool: `answer_question`. It does
no file loading, no splitting, no embedding at runtime. It connects to the
pre-built ChromaDB collection on disk and queries it. Its startup cost is:

- Python interpreter: ~0.3s
- SentenceTransformer model load from disk: ~1s (with TRANSFORMERS_OFFLINE=1)
- ChromaDB PersistentClient connect: ~0.2s

Total: ~1.5s per session. Then every query uses the already-loaded model in RAM.

---

## 4. How the pieces connect

```
start.py
    │
    ├─→ subprocess: backend/main.py
    │       │
    │       │  FastAPI lifespan starts
    │       │
    │       └─→ agent.py: build_agent()
    │               │
    │               └─→ MultiServerMCPClient(mcp_config)
    │                       │
    │                       ├─→ subprocess: backend/servers/rag_server.py
    │                       │       stdio pipe (no port)
    │                       │       SentenceTransformer loads → stays in RAM
    │                       │       ChromaDB connects → stays connected
    │                       │       MCP handshake complete
    │                       │
    │                       └─→ (future) subprocess: backend/servers/search_server.py
    │                               stdio pipe
    │
    │       LangGraph graph compiled with MemorySaver
    │       FastAPI opens on :8001
    │
    └─→ subprocess: npm run dev (frontend on :5173)
            Vite proxies /api/* → :8001

USER opens http://localhost:5173
    types question
    POST /api/chat → main.py → agent.py → MCP call → rag_server.py → answer
    SSE stream → browser renders response
```

---

## 5. Embedding model and memory lifecycle

```
SESSION START (python start.py):
    rag_server.py subprocess spawns
    SentenceTransformer('all-MiniLM-L6-v2') called ONCE
        → loads ~90MB weights from disk cache into RAM
        → stays in RAM for the entire session

QUERY 1:
    agent calls answer_question("what are Akash's skills")
    rag_server already has model in RAM
    model.encode(query) → ~0.5s
    chroma.query()      → ~0.1s
    Gemini API call     → ~2-3s
    answer streamed back

QUERY 2, 3, 4... N (same session):
    model.encode() again, model already in RAM
    no reload, no startup cost
    same ~3s per query (dominated by Gemini)

SESSION END (Ctrl+C in start.py):
    all subprocesses killed
    RAM freed
    model gone

NEXT SESSION (python start.py again):
    model reloads from disk (~1s with TRANSFORMERS_OFFLINE=1)
    everything ready in ~2s total
```

The key insight: stdio subprocess lifetime = backend lifetime. The model
is not per-query. It is per-session. Exactly the same as when rag_server.py
was a separate SSE server — just without the port.

---

## 6. Complete code — all files

---

### 6.1 preload.py

Run this once before the first session. Re-run with `--rebuild` if you add
new documents to `data_files/`.

```python
"""
preload.py — run once to build the vector database from data_files/.

Usage:
    python preload.py              # builds if empty
    python preload.py --rebuild    # wipes and rebuilds from scratch
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import chromadb
import numpy as np
from dotenv import load_dotenv
from langchain_community.document_loaders import (
    PyPDFLoader,
    TextLoader,
    UnstructuredWordDocumentLoader,
)
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer

load_dotenv()

# Offline mode — no HuggingFace network checks after first download
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

BASE_DIR   = Path(__file__).resolve().parent
DATA_DIR   = BASE_DIR / "data_files"
VECTOR_DIR = BASE_DIR / "vector_database"
COLLECTION = "documents_chunks"
MODEL_NAME = "all-MiniLM-L6-v2"


# ── Metadata helpers ──────────────────────────────────────────────────────────

def _sanitize_value(v):
    if isinstance(v, (str, int, float, bool)):
        return v
    if v is None:
        return None
    if isinstance(v, Path):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (list, tuple, set)):
        return ", ".join(str(i) for i in v)
    if isinstance(v, dict):
        return json.dumps(v, default=str)
    return str(v)


def sanitize_metadata(meta: dict) -> dict:
    return {
        str(k): _sanitize_value(v)
        for k, v in meta.items()
        if _sanitize_value(v) is not None
    }


# ── File loading ──────────────────────────────────────────────────────────────

def load_files(data_dir: Path) -> list[Document]:
    if not data_dir.exists():
        print(f"[preload] ERROR: data_files/ not found at {data_dir}")
        sys.exit(1)

    loaders = {
        ".pdf":  PyPDFLoader,
        ".txt":  lambda p: TextLoader(p, encoding="utf-8", autodetect_encoding=True),
        ".docx": UnstructuredWordDocumentLoader,
    }

    all_docs = []
    for fp in data_dir.iterdir():
        if not fp.is_file():
            continue
        suffix = fp.suffix.lower()
        if suffix not in loaders:
            print(f"[preload] skipping unsupported file: {fp.name}")
            continue
        try:
            docs = loaders[suffix](str(fp)).load()
            for i, doc in enumerate(docs):
                extra = {
                    "source":    str(fp),
                    "file_name": fp.name,
                    "file_type": suffix[1:],
                }
                if suffix == ".pdf":
                    extra["page_number"] = i + 1
                doc.metadata.update(extra)
                doc.metadata = sanitize_metadata(doc.metadata)
            all_docs.extend(docs)
            print(f"[preload] loaded: {fp.name} ({len(docs)} pages/sections)")
        except Exception as e:
            print(f"[preload] ERROR loading {fp.name}: {e}")

    print(f"[preload] total documents loaded: {len(all_docs)}")
    return all_docs


# ── Split ─────────────────────────────────────────────────────────────────────

def split_documents(docs: list[Document]) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    chunks = splitter.split_documents(docs)
    print(f"[preload] total chunks created: {len(chunks)}")
    return chunks


# ── Embed ─────────────────────────────────────────────────────────────────────

def embed_chunks(chunks: list[Document]) -> np.ndarray:
    print(f"[preload] loading embedding model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)
    texts = [c.page_content for c in chunks]
    print(f"[preload] embedding {len(texts)} chunks...")
    embeddings = model.encode(
        texts,
        show_progress_bar=True,
        batch_size=32,
        normalize_embeddings=True,
    )
    return np.array(embeddings)


# ── Store ─────────────────────────────────────────────────────────────────────

def store(chunks: list[Document], embeddings: np.ndarray, rebuild: bool):
    VECTOR_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(VECTOR_DIR))

    if rebuild:
        try:
            client.delete_collection(COLLECTION)
            print("[preload] existing collection deleted for rebuild")
        except Exception:
            pass

    collection = client.get_or_create_collection(
        name=COLLECTION,
        metadata={"description": "RAG document chunks"},
    )

    if collection.count() > 0 and not rebuild:
        print(f"[preload] collection already has {collection.count()} chunks. "
              "Run with --rebuild to overwrite.")
        return

    ids, metadatas, texts, vecs = [], [], [], []
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        meta = dict(chunk.metadata) if chunk.metadata else {}
        meta["doc_index"]   = i
        meta["content_len"] = len(chunk.page_content)
        # final sanitize pass for ChromaDB
        for k, v in meta.items():
            if not isinstance(v, (str, int, float, bool)):
                meta[k] = str(v)
        ids.append(str(uuid.uuid4()))
        metadatas.append(meta)
        texts.append(chunk.page_content)
        vecs.append(emb.tolist())

    collection.add(ids=ids, metadatas=metadatas, documents=texts, embeddings=vecs)
    print(f"[preload] stored {len(chunks)} chunks into vector_database/")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true",
                        help="wipe existing index and rebuild from scratch")
    args = parser.parse_args()

    print("=" * 50)
    print("RAG Chatbot — Preload Script")
    print("=" * 50)

    docs   = load_files(DATA_DIR)
    chunks = split_documents(docs)
    embs   = embed_chunks(chunks)
    store(chunks, embs, rebuild=args.rebuild)

    print("=" * 50)
    print("Preload complete. You can now run: python start.py")
    print("=" * 50)


if __name__ == "__main__":
    main()
```

---

### 6.2 start.py

Single entry point. Starts the backend (which spawns MCP servers internally)
and the Vite frontend. One Ctrl+C kills everything cleanly.

```python
"""
start.py — starts the full application with one command.

    python start.py

Starts:
  1. backend/main.py       (FastAPI on :8001, spawns MCP servers internally)
  2. npm run dev           (React frontend on :5173)

Ctrl+C cleanly terminates both.
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND  = ROOT / "backend" / "main.py"
FRONTEND = ROOT / "frontend"


def main():
    print("[start] Starting RAG Chatbot...")
    print("[start] Backend:  python backend/main.py")
    print("[start] Frontend: npm run dev (frontend/)")
    print("[start] Press Ctrl+C to stop everything.\n")

    backend  = subprocess.Popen([sys.executable, str(BACKEND)])
    # Small delay so FastAPI is up before Vite tries to proxy
    time.sleep(2)
    frontend = subprocess.Popen(["npm", "run", "dev"], cwd=str(FRONTEND))

    try:
        backend.wait()
    except KeyboardInterrupt:
        print("\n[start] Shutting down...")
    finally:
        backend.terminate()
        frontend.terminate()
        backend.wait()
        frontend.wait()
        print("[start] All processes stopped.")


if __name__ == "__main__":
    main()
```

---

### 6.3 backend/agent.py

Owns everything about the agent: MCP client, tool binding, LangGraph graph,
MemorySaver. FastAPI never touches any of this directly — it just calls
`get_app()` to get the compiled graph and uses it.

```python
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


# ── Agent state ───────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


# ── Build function ────────────────────────────────────────────────────────────

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
```

---

### 6.4 backend/main.py

Owns everything about HTTP: FastAPI app, CORS, SSE streaming, route
definitions. Imports `build_agent` and `MCP_CONFIG` from `agent.py`.
Does not know what LangGraph or MCP is beyond calling those functions.

```python
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


# ── Lifespan ──────────────────────────────────────────────────────────────────

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

    print("[main] Starting up — spawning MCP servers and building agent...")

    async with MultiServerMCPClient(MCP_CONFIG) as client:
        _mcp_client = client
        _agent_app, _ = await build_agent(client)
        print("[main] Ready. Accepting requests.")
        yield

    print("[main] Shutdown complete.")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],   # Vite dev server only
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request model ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message:   str
    thread_id: str


# ── Helper: extract clean text from Gemini AIMessage ─────────────────────────

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


# ── SSE streaming endpoint ────────────────────────────────────────────────────

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


# ── New thread endpoint ───────────────────────────────────────────────────────

@app.get("/api/new_thread")
async def new_thread():
    """
    Returns a fresh UUID for a new conversation.
    The frontend passes this thread_id with every message.
    MemorySaver uses it to isolate conversation history.
    """
    return {"thread_id": str(uuid.uuid4())}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=False)
```

---

### 6.5 backend/servers/rag_server.py

Pure retrieval server. No ingestion. No file loading. No splitting.
Connects to the pre-built ChromaDB collection on disk and queries it.
Startup is fast because the expensive work was done by `preload.py`.

```python
"""
backend/servers/rag_server.py — MCP stdio retrieval server.

Responsibilities:
  - connect to the pre-built ChromaDB vector store
  - load the SentenceTransformer embedding model (once, at startup)
  - expose one MCP tool: answer_question(query)
  - never ingest, never load files, never split documents

The vector store is pre-built by preload.py.
This server is spawned as a subprocess by MultiServerMCPClient in agent.py.
It communicates over stdin/stdout (stdio transport). No port. No HTTP.
"""

import os
import sys
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import chromadb
import numpy as np
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from mcp.server.fastmcp import FastMCP
from sentence_transformers import SentenceTransformer

load_dotenv()

# Offline mode — prevents HuggingFace HTTP checks after first model download
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

BASE_DIR    = Path(__file__).resolve().parents[2]   # RAG_chatbot/
VECTOR_DIR  = BASE_DIR / "vector_database"
COLLECTION  = "documents_chunks"
MODEL_NAME  = "all-MiniLM-L6-v2"
THRESHOLD   = 1.5    # max L2 distance (0-2 range for normalized vectors)
TOP_K       = 10


def log(msg: str):
    # MCP server must use stderr only — stdout is the stdio transport pipe
    print(msg, file=sys.stderr)


# ── Lifespan — validates the vector store exists before accepting tool calls ──

@asynccontextmanager
async def lifespan(server):
    store = get_vector_store()
    count = store.count()
    if count == 0:
        raise RuntimeError(
            "Vector store is empty. Run `python preload.py` first to build the index."
        )
    log(f"[rag_server] Vector store ready — {count} chunks loaded.")
    log(f"[rag_server] Embedding model: {MODEL_NAME}")
    yield
    log("[rag_server] Shutdown.")


mcp = FastMCP("RAGServer", lifespan=lifespan)


# ── Singletons — created once, reused for every tool call ────────────────────

@lru_cache(maxsize=1)
def get_model() -> SentenceTransformer:
    """
    Loads the embedding model once into RAM.
    lru_cache ensures this is never called twice in the same process.
    """
    log(f"[rag_server] Loading embedding model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)
    log("[rag_server] Embedding model loaded.")
    return model


@lru_cache(maxsize=1)
def get_vector_store() -> chromadb.Collection:
    """
    Connects to the persistent ChromaDB collection.
    Returns the collection object — used directly for querying.
    """
    client = chromadb.PersistentClient(path=str(VECTOR_DIR))
    collection = client.get_or_create_collection(
        name=COLLECTION,
        metadata={"description": "RAG document chunks"},
    )
    return collection


# ── Retrieval ─────────────────────────────────────────────────────────────────

def retrieve(query: str, top_k: int = TOP_K) -> list[dict[str, Any]]:
    """
    Embeds the query and returns the top_k most similar chunks
    within the distance threshold.
    """
    model      = get_model()
    collection = get_vector_store()

    query_vec = model.encode([query], normalize_embeddings=True)[0].tolist()

    results = collection.query(
        query_embeddings=[query_vec],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )

    docs = []
    if results["documents"] and results["documents"][0]:
        for rank, (doc, meta, dist, doc_id) in enumerate(zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
            results["ids"][0],
        )):
            if dist > THRESHOLD:
                break    # results are sorted by distance, stop early
            docs.append({
                "document": doc,
                "metadata": meta,
                "distance": dist,
                "doc_id":   doc_id,
                "rank":     rank + 1,
            })

    log(f"[rag_server] retrieved {len(docs)} chunks for query: {query[:60]}")
    return docs


# ── Generation ────────────────────────────────────────────────────────────────

def generate(query: str, chunks: list[dict[str, Any]]) -> str:
    """
    Sends the retrieved chunks as context to Gemini and returns the answer.
    """
    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not set.")

    llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0,
        google_api_key=api_key,
    )

    context = "\n\n".join(c["document"] for c in chunks)
    prompt  = (
        "Answer the following question using only the provided context. "
        "If the context does not contain enough information, say so clearly.\n\n"
        f"Question: {query}\n\nContext:\n{context}"
    )

    response = llm.invoke(prompt)

    if isinstance(response.content, list):
        return "\n".join(
            p.get("text", "") for p in response.content
            if isinstance(p, dict)
        ).strip()
    return str(response.content)


# ── MCP tool — the only thing the agent can call ──────────────────────────────

@mcp.tool()
def answer_question(query: str, top_k: int = TOP_K) -> dict[str, Any]:
    """
    Retrieve relevant document chunks and generate a grounded answer.
    This is the only tool exposed to the agent.
    """
    chunks = retrieve(query, top_k=top_k)
    answer = generate(query, chunks)
    return {
        "query":         query,
        "answer":        answer,
        "retrieved_docs": chunks,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # stdio transport — communicates over stdin/stdout pipe
    # spawned automatically by MultiServerMCPClient in agent.py
    mcp.run(transport="stdio")
```

---

### 6.6 frontend/vite.config.js

Proxies all `/api/*` requests from the React dev server to FastAPI.
The browser never needs to know port 8001 exists.

```javascript
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8001',
    },
  },
})
```

---

### 6.7 frontend/index.html

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>RAG Chat</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
```

---

### 6.8 frontend/src/main.jsx

```jsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './App.css'
import App from './App.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
```

---

### 6.9 frontend/src/App.jsx

```jsx
import { useState, useRef, useEffect, useCallback } from 'react'
import './App.css'

// ── Inline SVG icons ──────────────────────────────────────────────────────────
const SendIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M22 2L11 13M22 2L15 22l-4-9-9-4 20-7z" />
  </svg>
)
const PlusIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M12 5v14M5 12h14" />
  </svg>
)
const BotIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
    <rect x="3" y="11" width="18" height="10" rx="2" />
    <path d="M12 11V7M9 7h6M7 15h.01M12 15h.01M17 15h.01" />
  </svg>
)
const UserIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
    <circle cx="12" cy="8" r="4" />
    <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" />
  </svg>
)
const TrashIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
    <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6" />
  </svg>
)

// ── Typing / status indicator ─────────────────────────────────────────────────
function TypingIndicator({ status }) {
  return (
    <div className="message assistant">
      <div className="avatar"><BotIcon /></div>
      <div className="bubble typing-bubble">
        {status
          ? <span className="status-text">{status}</span>
          : <span className="dots"><span /><span /><span /></span>
        }
      </div>
    </div>
  )
}

// ── Single message bubble ─────────────────────────────────────────────────────
function Message({ msg }) {
  const isUser = msg.role === 'user'
  return (
    <div className={`message ${isUser ? 'user' : 'assistant'}`}>
      <div className="avatar">{isUser ? <UserIcon /> : <BotIcon />}</div>
      <div className="bubble">
        {msg.content.split('\n').map((line, i, arr) => (
          <span key={i}>{line}{i < arr.length - 1 && <br />}</span>
        ))}
      </div>
    </div>
  )
}

// ── Sidebar conversation item ─────────────────────────────────────────────────
function ConvItem({ conv, isActive, onSelect, onDelete }) {
  return (
    <div
      className={`conv-item ${isActive ? 'active' : ''}`}
      onClick={() => onSelect(conv.id)}
    >
      <span className="conv-title">{conv.title}</span>
      <button
        className="conv-delete"
        onClick={e => { e.stopPropagation(); onDelete(conv.id) }}
      >
        <TrashIcon />
      </button>
    </div>
  )
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  const [conversations, setConversations] = useState([])
  const [activeId,      setActiveId]      = useState(null)
  const [input,         setInput]         = useState('')
  const [isStreaming,   setIsStreaming]   = useState(false)
  const [streamStatus,  setStreamStatus]  = useState('')
  const bottomRef   = useRef(null)
  const textareaRef = useRef(null)

  const activeConv = conversations.find(c => c.id === activeId)
  const messages   = activeConv?.messages ?? []

  // Scroll to bottom on new content
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isStreaming])

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 160) + 'px'
  }, [input])

  // New conversation — fetches a thread_id from the backend
  const newConversation = useCallback(async () => {
    const res = await fetch('/api/new_thread')
    const { thread_id } = await res.json()
    const conv = { id: thread_id, title: 'New conversation', messages: [] }
    setConversations(prev => [conv, ...prev])
    setActiveId(thread_id)
    setInput('')
  }, [])

  useEffect(() => { newConversation() }, [])

  const deleteConversation = useCallback((id) => {
    setConversations(prev => {
      const next = prev.filter(c => c.id !== id)
      if (activeId === id) setActiveId(next[0]?.id ?? null)
      return next
    })
  }, [activeId])

  const updateMessages = useCallback((convId, updater) => {
    setConversations(prev =>
      prev.map(c => c.id === convId ? { ...c, messages: updater(c.messages) } : c)
    )
  }, [])

  const setTitle = useCallback((convId, title) => {
    setConversations(prev =>
      prev.map(c => c.id === convId ? { ...c, title: title.slice(0, 40) } : c)
    )
  }, [])

  // Send message and read SSE stream
  const sendMessage = useCallback(async () => {
    const text = input.trim()
    if (!text || isStreaming || !activeId) return

    setInput('')
    setIsStreaming(true)
    setStreamStatus('')

    updateMessages(activeId, msgs => {
      if (msgs.length === 0) setTitle(activeId, text)
      return [...msgs, { role: 'user', content: text }]
    })

    try {
      const response = await fetch('/api/chat', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ message: text, thread_id: activeId }),
      })

      if (!response.ok) throw new Error(`HTTP ${response.status}`)

      const reader  = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop()   // keep incomplete last line

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const raw = line.slice(6).trim()
          if (!raw) continue
          let data
          try { data = JSON.parse(raw) } catch { continue }

          if (data.type === 'status') {
            setStreamStatus(data.text)
          } else if (data.type === 'token') {
            setStreamStatus('')
            updateMessages(activeId, msgs => [
              ...msgs,
              { role: 'assistant', content: data.text },
            ])
          } else if (data.type === 'error') {
            updateMessages(activeId, msgs => [
              ...msgs,
              { role: 'assistant', content: `Error: ${data.text}` },
            ])
          } else if (data.type === 'done') {
            setStreamStatus('')
          }
        }
      }
    } catch (err) {
      updateMessages(activeId, msgs => [
        ...msgs,
        { role: 'assistant', content: `Connection error: ${err.message}` },
      ])
    } finally {
      setIsStreaming(false)
      setStreamStatus('')
    }
  }, [input, isStreaming, activeId, updateMessages, setTitle])

  const handleKeyDown = e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  return (
    <div className="app">
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <span className="logo">⬡ RAG Chat</span>
          <button className="new-chat-btn" onClick={newConversation}>
            <PlusIcon />
          </button>
        </div>
        <div className="conv-list">
          {conversations.length === 0 && (
            <p className="conv-empty">No conversations yet</p>
          )}
          {conversations.map(conv => (
            <ConvItem
              key={conv.id}
              conv={conv}
              isActive={conv.id === activeId}
              onSelect={setActiveId}
              onDelete={deleteConversation}
            />
          ))}
        </div>
        <div className="sidebar-footer">
          <span>Powered by Gemini + LangGraph</span>
        </div>
      </aside>

      {/* Chat area */}
      <main className="chat-area">
        {messages.length === 0 && !isStreaming ? (
          <div className="welcome">
            <div className="welcome-icon"><BotIcon /></div>
            <h2>How can I help you?</h2>
            <p>Ask me anything about the documents in your knowledge base.</p>
          </div>
        ) : (
          <div className="messages">
            {messages.map((msg, i) => <Message key={i} msg={msg} />)}
            {isStreaming && <TypingIndicator status={streamStatus} />}
            <div ref={bottomRef} />
          </div>
        )}

        {/* Input bar */}
        <div className="input-bar">
          <div className="input-wrapper">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask a question… (Enter to send, Shift+Enter for new line)"
              rows={1}
              disabled={isStreaming || !activeId}
            />
            <button
              className="send-btn"
              onClick={sendMessage}
              disabled={!input.trim() || isStreaming || !activeId}
            >
              <SendIcon />
            </button>
          </div>
          <p className="input-hint">Responses come from your knowledge base only.</p>
        </div>
      </main>
    </div>
  )
}
```

---

### 6.10 frontend/src/App.css

```css
@import url('https://fonts.googleapis.com/css2?family=Sora:wght@300;400;500;600&family=Lora:ital,wght@0,400;0,500;1,400&display=swap');

:root {
  --bg:          #0d1117;
  --surface:     #161b22;
  --surface2:    #1c2128;
  --border:      #30363d;
  --accent:      #d4a853;
  --accent-dim:  #9a7a3d;
  --text:        #e6edf3;
  --text-muted:  #8b949e;
  --user-bg:     #1f2d1f;
  --user-border: #2d4a2d;
  --bot-bg:      #1c2128;
  --radius:      14px;
  --sidebar-w:   260px;
  --font-ui:     'Sora', sans-serif;
  --font-prose:  'Lora', Georgia, serif;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body, #root { height: 100%; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-ui);
  font-size: 15px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}

/* Layout */
.app { display: flex; height: 100vh; overflow: hidden; }

/* Sidebar */
.sidebar {
  width: var(--sidebar-w);
  min-width: var(--sidebar-w);
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.sidebar-header {
  padding: 18px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border);
}
.logo { font-size: 16px; font-weight: 600; color: var(--accent); letter-spacing: .03em; }
.new-chat-btn {
  width: 32px; height: 32px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: all .15s;
}
.new-chat-btn:hover { border-color: var(--accent); color: var(--accent); background: rgba(212,168,83,.08); }
.new-chat-btn svg { width: 16px; height: 16px; }

.conv-list { flex: 1; overflow-y: auto; padding: 8px; scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
.conv-empty { color: var(--text-muted); font-size: 13px; text-align: center; padding: 24px 0; }
.conv-item {
  display: flex; align-items: center; justify-content: space-between;
  padding: 9px 12px; border-radius: 8px; cursor: pointer; gap: 8px;
  transition: background .12s; border: 1px solid transparent;
}
.conv-item:hover { background: var(--surface2); }
.conv-item.active { background: var(--surface2); border-color: var(--border); }
.conv-title { font-size: 13px; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
.conv-item.active .conv-title { color: var(--text); }
.conv-delete {
  width: 24px; height: 24px; border-radius: 5px; border: none; background: transparent;
  color: var(--text-muted); cursor: pointer; display: flex; align-items: center; justify-content: center;
  opacity: 0; transition: opacity .12s, color .12s; flex-shrink: 0;
}
.conv-item:hover .conv-delete { opacity: 1; }
.conv-delete:hover { color: #f85149; }
.conv-delete svg { width: 13px; height: 13px; }
.sidebar-footer { padding: 12px 16px; border-top: 1px solid var(--border); font-size: 11px; color: var(--text-muted); text-align: center; }

/* Chat area */
.chat-area { flex: 1; display: flex; flex-direction: column; overflow: hidden; background: var(--bg); }

/* Welcome */
.welcome {
  flex: 1; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 16px; padding: 40px; text-align: center;
  animation: fadeIn .4s ease;
}
.welcome-icon {
  width: 56px; height: 56px; border-radius: 16px; background: var(--surface);
  border: 1px solid var(--border); display: flex; align-items: center; justify-content: center; color: var(--accent);
}
.welcome-icon svg { width: 28px; height: 28px; }
.welcome h2 { font-size: 24px; font-weight: 500; color: var(--text); }
.welcome p  { font-size: 14px; color: var(--text-muted); max-width: 360px; }

/* Messages */
.messages { flex: 1; overflow-y: auto; padding: 32px 0; display: flex; flex-direction: column; gap: 8px; scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
.message { display: flex; align-items: flex-start; gap: 12px; padding: 6px 32px; animation: slideUp .2s ease; max-width: 860px; width: 100%; margin: 0 auto; }
.message.user { flex-direction: row-reverse; }
.avatar { width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; border: 1px solid var(--border); }
.avatar svg { width: 16px; height: 16px; }
.message.user      .avatar { background: rgba(212,168,83,.12); color: var(--accent); border-color: var(--accent-dim); }
.message.assistant .avatar { background: var(--surface); color: var(--text-muted); }
.bubble { padding: 12px 16px; border-radius: var(--radius); max-width: calc(100% - 56px); line-height: 1.7; word-break: break-word; }
.message.user      .bubble { background: var(--user-bg); border: 1px solid var(--user-border); color: var(--text); font-family: var(--font-ui); font-size: 14px; border-bottom-right-radius: 4px; }
.message.assistant .bubble { background: var(--bot-bg); border: 1px solid var(--border); color: var(--text); font-family: var(--font-prose); font-size: 15px; border-bottom-left-radius: 4px; }

/* Typing indicator */
.typing-bubble { min-height: 42px; display: flex; align-items: center; }
.status-text { font-family: var(--font-ui); font-size: 13px; color: var(--accent); font-style: italic; animation: pulse 1.5s ease-in-out infinite; }
.dots { display: flex; gap: 5px; align-items: center; }
.dots span { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); animation: bounce 1.2s ease-in-out infinite; }
.dots span:nth-child(2) { animation-delay: .2s; }
.dots span:nth-child(3) { animation-delay: .4s; }

/* Input bar */
.input-bar { padding: 16px 32px 20px; display: flex; flex-direction: column; align-items: center; gap: 8px; background: var(--bg); border-top: 1px solid var(--border); }
.input-wrapper { display: flex; align-items: flex-end; gap: 10px; width: 100%; max-width: 760px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px 12px; transition: border-color .15s; }
.input-wrapper:focus-within { border-color: var(--accent-dim); }
textarea { flex: 1; background: transparent; border: none; outline: none; color: var(--text); font-family: var(--font-ui); font-size: 14px; line-height: 1.6; resize: none; max-height: 160px; scrollbar-width: thin; }
textarea::placeholder { color: var(--text-muted); }
textarea:disabled { opacity: .5; cursor: not-allowed; }
.send-btn { width: 34px; height: 34px; border-radius: 8px; border: none; background: var(--accent); color: #0d1117; cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: all .15s; align-self: flex-end; }
.send-btn:hover:not(:disabled) { background: #e8bb6a; transform: scale(1.05); }
.send-btn:disabled { background: var(--surface2); color: var(--text-muted); cursor: not-allowed; }
.send-btn svg { width: 16px; height: 16px; }
.input-hint { font-size: 11px; color: var(--text-muted); text-align: center; }

/* Animations */
@keyframes fadeIn  { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
@keyframes slideUp { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
@keyframes bounce  { 0%,80%,100% { transform: translateY(0); opacity: .4; } 40% { transform: translateY(-6px); opacity: 1; } }
@keyframes pulse   { 0%,100% { opacity: 1; } 50% { opacity: .5; } }

/* Scrollbar */
::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 10px; }
```

---

## 7. How to run

### First time only

```bash
# 1. Install Python dependencies
pip install fastapi uvicorn python-multipart langchain langgraph
pip install langchain-google-genai langchain-mcp-adapters
pip install langchain-community chromadb sentence-transformers
pip install pypdf unstructured python-dotenv

# 2. Install frontend dependencies
cd frontend && npm install && cd ..

# 3. Create .env at project root
echo "GOOGLE_API_KEY=your_key_here"        > .env
echo "TRANSFORMERS_OFFLINE=0"             >> .env   # set to 1 after first model download

# 4. Put your documents in data_files/

# 5. Build the vector database (run once, or when you add new files)
python preload.py
```

### Every run after

```bash
python start.py
```

Open `http://localhost:5173` in your browser.

### If you add new documents

```bash
python preload.py --rebuild
python start.py
```

---

## 8. Adding new MCP tools in the future

**Step 1** — create `backend/servers/search_server.py`:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("SearchServer")

@mcp.tool()
def search_web(query: str) -> dict:
    """Search the internet for recent information."""
    # ... your implementation
    return {"results": [...]}

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

**Step 2** — add one line to `MCP_CONFIG` in `backend/agent.py`:

```python
MCP_CONFIG = {
    "rag": {
        "command": "python",
        "args":    [str(SERVERS_DIR / "rag_server.py")],
        "transport": "stdio",
    },
    "search": {                                          # ← add this
        "command": "python",
        "args":    [str(SERVERS_DIR / "search_server.py")],
        "transport": "stdio",
    },
}
```

That is the entire change. No ports. No terminals. No changes to `main.py`.
The agent automatically gets the new tool bound at startup.

---

## 9. What each design choice protects against

| Design choice | Problem it prevents |
|---|---|
| `preload.py` separate from `rag_server.py` | Server never unexpectedly re-ingests during a session |
| stdio transport | Port conflicts when adding more MCP servers |
| `agent.py` separate from `main.py` | Editing HTTP routes doesn't risk breaking tool logic |
| `start.py` single entry point | Forgetting to start a process, or starting in wrong order |
| `TRANSFORMERS_OFFLINE=1` | Slow HuggingFace network checks on every startup |
| `lru_cache` on singletons in rag_server | Model and DB connection never created twice in same process |
| MemorySaver with thread_id | Multiple browser tabs get isolated conversation history |
| Vite proxy for `/api/*` | Browser never needs to know backend port; no CORS issues in dev |
| `extract_text()` in main.py | Gemini's cryptographic signature in response extras is discarded |
| RuntimeError in rag_server lifespan if DB empty | Clear error at startup rather than silent empty answers at query time |
