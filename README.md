# RAG_chatbot

A small Retrieval-Augmented Generation (RAG) demo that ingests local documents, builds embeddings, stores them in a Chroma vector database, and answers user queries with a Google Gemini LLM.

## Features
- Load and chunk local documents from `data_files/`.
- Generate embeddings with `sentence-transformers`.
- Store embeddings in a ChromaDB database located at `vector_database/`.
- Retrieve relevant chunks for a query and generate answers via Google Generative AI (Gemini).

## Prerequisites
- Python 3.9+
- A virtual environment (recommended)
- Google API key set in environment variable `GOOGLE_API_KEY` (if using Google Geminis)

## Installation
1. Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # PowerShell
# or
.\.venv\Scripts\activate.bat   # cmd.exe
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage
- Run the MCP RAG server:

```powershell
python pipelines/rag_server.py
```

- Available MCP tools:
	- `ingest_documents(force_rebuild=False)` to build or refresh the Chroma index from `data_files/`
	- `retrieve_relevant_chunks(query, top_k=5)` to return the best matching chunks
	- `answer_question(query, top_k=5)` to run retrieval plus Gemini answer generation
- The server ingests local documents during the MCP lifespan, so the vector store is ready when the server starts.

## Configuration
- Store keys in a `.env` file or your OS environment variables. Example `.env`:

```
GOOGLE_API_KEY=your_api_key_here
```

## Project structure
- `data_files/` — Raw text documents used for building the vector store.
- `pipelines/` — Loader, retrieval, and test notebooks/scripts.
- `vector_database/` — ChromaDB local DB files (ignored by `.gitignore`).
- `requirements.txt` — Python dependencies.

## Notes & Tips
- If you change embedding models, ensure `embedding_dim` matches ChromaDB collection settings.
- Keep sensitive data (API keys, large datasets) out of the repo — add them to `.gitignore` if needed.

## Contributing
Open an issue or submit a PR with improvements.

## License
Specify your license here (e.g., MIT).