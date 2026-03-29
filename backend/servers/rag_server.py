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

#Retrival 
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

#generation 
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

#only mcp tool the agetn can call 
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

if __name__ == "__main__":
    # stdio transport — communicates over stdin/stdout pipe
    # spawned automatically by MultiServerMCPClient in agent.py
    mcp.run(transport="stdio")