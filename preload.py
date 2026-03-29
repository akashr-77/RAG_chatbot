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