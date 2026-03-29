# RAG Chatbot

Local RAG chatbot with a React frontend, a FastAPI chat API, and a LangGraph agent that calls an MCP-backed retrieval tool. The architecture and runtime flow follow [system_design.md](system_design.md).

## Overview
User questions go from the frontend to FastAPI, then through the LangGraph agent to the MCP retrieval server. The retrieval server queries a persistent ChromaDB index built from documents in `data_files/`, and the final answer is streamed back to the browser over SSE.

## Architecture
- `preload.py` builds `vector_database/` from the source documents.
- `backend/main.py` serves `/api/chat` and `/api/new_thread`.
- `backend/agent.py` builds the LangGraph agent and connects MCP tools.
- `backend/servers/rag_server.py` exposes the retrieval tool.
- `frontend/` contains the Vite + React UI.

## Project Structure
- `data_files/` - source documents to embed.
- `vector_database/` - generated ChromaDB persistence directory.
- `backend/` - FastAPI app, LangGraph agent, and MCP server.
- `frontend/` - Vite + React app.
- `preload.py` - one-time index builder.
- `start.py` - launches backend and frontend together.

## Setup
1. Create a Python virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install Python dependencies:

```powershell
pip install -r requirements.txt
```

3. Install frontend dependencies:

```powershell
cd frontend
npm install
```

4. Add your Google API key to a `.env` file at the repo root:

```env
GOOGLE_API_KEY=your_api_key_here
```

## Build the Vector Store
Run this once before the first chat session, and again with `--rebuild` if you change documents:

```powershell
python preload.py
```

## Run the App
Start everything with one command:

```powershell
python start.py
```

If you prefer manual startup:

```powershell
python backend/main.py
```

```powershell
cd frontend
npm run dev
```

## API
- `GET /api/new_thread` returns a new conversation UUID.
- `POST /api/chat` accepts `{ "message": string, "thread_id": string }` and streams SSE events.
- SSE events include `status`, `token`, `done`, and `error`.

## Notes
- `backend/servers/rag_server.py` is a subprocess managed by the backend; it does not need its own terminal.
- `vector_database/`, `frontend/node_modules/`, `.venv/`, and other generated files should not be committed.
- If you change the embedding model, rebuild the vector store so dimensions stay consistent.