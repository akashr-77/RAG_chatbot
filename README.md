# RAG_chatbot

A Retrieval-Augmented Generation (RAG) chatbot with a React frontend, a FastAPI chat API, and an MCP-backed retrieval service. Local documents are embedded into ChromaDB and used to answer questions with Google Gemini.

## Features
- Load and chunk local documents from `data_files/`.
- Generate embeddings with `sentence-transformers`.
- Store embeddings in a persistent ChromaDB database at `vector_database/`.
- Expose retrieval as MCP tools from `pipelines/rag_server.py`.
- Orchestrate chat responses through LangGraph in `pipelines/api_server.py`.
- Serve a React + Vite frontend that streams answers over Server-Sent Events.

## Prerequisites
- Python 3.9+
- A virtual environment (recommended)
- Node.js 18+ for the frontend
- Google API key set in environment variable `GOOGLE_API_KEY` (if using Google Geminis)

## Installation
1. Create and activate a Python virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # PowerShell
# or
.\.venv\Scripts\activate.bat   # cmd.exe
```

2. Install Python dependencies:

```bash
pip install -r requirements.txt
```

3. Install frontend dependencies:

```powershell
cd frontend
npm install
```

## Usage
Start the backend services in separate terminals:

```powershell
python pipelines/rag_server.py
```

```powershell
python pipelines/api_server.py
```

Then start the frontend:

```powershell
cd frontend
npm run dev
```

### Runtime layout
- `pipelines/rag_server.py` runs an MCP SSE server on `http://127.0.0.1:8000/sse`.
- `pipelines/api_server.py` runs FastAPI on `http://127.0.0.1:8001`.
- `frontend/vite.config.js` proxies `/api` requests to `http://127.0.0.1:8001`.
- The frontend requests a fresh conversation id from `/api/new_thread` and streams replies from `/api/chat`.

## Configuration
- Store keys in a `.env` file or your OS environment variables. Example `.env`:

```
GOOGLE_API_KEY=your_api_key_here
```

## API Endpoints
- `GET /api/new_thread` returns a UUID for a new chat thread.
- `POST /api/chat` accepts `{ "message": string, "thread_id": string }` and streams SSE events.
- SSE event types are `status`, `token`, `done`, and `error`.

## Documentation
- Detailed architecture and design notes are in [system_design.md](system_design.md).

## Project structure
- `frontend/` - React + Vite app.
- `pipelines/rag_server.py` - MCP retrieval server and document ingestion.
- `pipelines/api_server.py` - FastAPI chat orchestrator.
- `data_files/` — Raw text documents used for building the vector store.
- `vector_database/` — ChromaDB local DB files (ignored by `.gitignore`).
- `requirements.txt` — Python dependencies.
- `frontend/package.json` - Frontend dependency manifest.

## Notes
- The RAG server ingests documents during startup, so the vector store is ready before chat requests arrive.
- If you change the embedding model, rebuild the Chroma collection so the embedding dimensions stay consistent.
- Keep sensitive data and large generated artifacts out of the repo.

## Contributing
Open an issue or submit a PR with improvements.

## License
Specify your license here (e.g., MIT).