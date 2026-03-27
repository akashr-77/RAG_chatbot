# loads the files from data_files folder 

import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Union

import chromadb
import numpy as np
from dotenv import load_dotenv
from langchain_community.document_loaders import (
    PyPDFLoader,
    TextLoader,
    UnstructuredWordDocumentLoader,
)
from langchain_core.documents import Document
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_text_splitters import RecursiveCharacterTextSplitter
from mcp.server.fastmcp import FastMCP
from sentence_transformers import SentenceTransformer

load_dotenv()

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data_files"
VECTOR_DB_DIR = BASE_DIR / "vector_database"


def log(message: str):
    print(message, file=sys.stderr)


@asynccontextmanager
async def lifespan(server):
    ensure_vector_store_populated()
    yield


mcp = FastMCP("RAGServer", lifespan=lifespan)

#load all the files from data_files folder

def _sanitize_metadata_value(value):
    if isinstance(value, (str, int, float, bool)):
        return value
    if value is None:
        return None
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, 'isoformat'):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, (list, tuple, set)):
        return ', '.join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, default=str, ensure_ascii=True)
    return str(value)

def sanitize_metadata(metadata):
    cleaned = {}
    for key, value in metadata.items():
        sanitized_value = _sanitize_metadata_value(value)
        if sanitized_value is not None:
            cleaned[str(key)] = sanitized_value
    return cleaned

def load_files(data_folder):
    """
    Loads all files(.pdf, .txt, .docx) from the specified data folder and returns a list of Document objects.
    """
    data_folder = Path(data_folder)
    all_docs = []
    loaded_files_count = 0

    if not data_folder.exists():
        log(f"Data folder not found: {data_folder}")
        return all_docs

    # Define loaders for each file type
    loaders = {
        '.pdf': PyPDFLoader,
        '.txt': lambda path: TextLoader(path, encoding='utf-8', autodetect_encoding=True),
        '.docx': UnstructuredWordDocumentLoader,
    }

    for file_path in data_folder.iterdir():
        if file_path.is_file():
            suffix = file_path.suffix.lower()
            if suffix in loaders:
                try:
                    loader = loaders[suffix](str(file_path))
                    docs = loader.load()
                    # Add metadata to documents
                    for i, doc in enumerate(docs):
                        extra_metadata = {
                            "source": str(file_path),
                            "file_name": file_path.name,
                            "file_type": suffix[1:],  # Remove the dot
                        }
                        if suffix == '.pdf':
                            extra_metadata["page_number"] = i + 1
                        doc.metadata.update(extra_metadata)
                        doc.metadata = sanitize_metadata(doc.metadata)
                    all_docs.extend(docs)
                    loaded_files_count += 1
                except Exception as e:
                    log(f"Error loading {file_path}: {e}")
            else:
                log(f"Unsupported file format: {file_path.name}")
    
    log(f"Loaded {loaded_files_count} files.")
    return all_docs


#split the documents into chunks of 1000 characters with an overlap of 200 characters
def split_documents(documents, chunk_size=1000, chunk_overlap=200):
    '''Splits documents into chunks'''
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    split_docs = text_splitter.split_documents(documents)  #returns a list of documents
    log(f"Total chunks created: {len(split_docs)}")
    return split_docs

#Embeddings and vector store

class EmbeddingManager:
    """
    Manages embedding generation.
    """
    def __init__(self, embedding_model_name: str = 'all-MiniLM-L6-v2'):
        self.embedding_model = embedding_model_name
        self.model = None
        self._load_model()

    def _load_model(self):
        """Loads the embedding model."""
        try:
            self.model = SentenceTransformer(self.embedding_model)
            log(f"Loaded embedding model: {self.embedding_model}")
        except Exception as e:
            log(f"Error loading embedding model: {e}")
            raise


    def generate_embeddings(self, texts: List[Union[str, Document]])->np.ndarray:
        """Generates embeddings for a list of texts or Document objects.
        Args:
            texts (List[Union[str, Document]]): List of strings or Document objects to embed.
            Returns: np.ndarray: Array of embeddings.
        """
        try:
            text_contents = [doc.page_content if isinstance(doc, Document) else doc for doc in texts]
            embeddings = self.model.encode(text_contents, show_progress_bar=True, batch_size=32, normalize_embeddings=True)
            return np.array(embeddings)
        except Exception as e:
            log(f"Error generating embeddings: {e}")
            raise
    

class VectorStore:
    """
    Manages storage and retrieval of embeddings. 
    """

    def __init__(self, collection_name: str = "documents_chunks", persist_directory: str = "../vector_database"):
        self.collection_name = collection_name
        self.persist_directory = persist_directory
        self.client = None
        self.collection = None
        self._initialize_chromadb()

    def _initialize_chromadb(self):
        """Initializes the ChromaDB client and collection."""
        try:
            os.makedirs(self.persist_directory, exist_ok=True)
            self.client = chromadb.PersistentClient(path = self.persist_directory)
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "Collection of document chunks and their embeddings"}
            )
            log(f"Collection '{self.collection_name}' is ready.")
        except Exception as e:
            log(f"Error initializing ChromaDB: {e}")
            raise
    
    def _sanitize_metadata(self, metadata: dict) -> dict:
        """Ensures all metadata values are ChromaDB-compatible types."""
        sanitized = {}
        for k, v in metadata.items():
            if isinstance(v, (str, int, float, bool)):
                sanitized[k] = v
            elif v is None:
                sanitized[k] = ""          # Replace None with empty string
            else:
                sanitized[k] = str(v)      # Coerce lists, dicts, etc. to string
        return sanitized

    def add_docs(self, docs: List[Union[str, Document]], embeddings: np.ndarray):
        """ 
        Adds documents and their embeddings to the collection.
        Args:
            docs (List[Union[str, Document]]): List of documents or strings to add.
            embeddings (np.ndarray): Corresponding array of embeddings.
        """
        log(f"Adding {len(docs)} documents to the vector store...")

        if len(docs) != len(embeddings):
            raise ValueError(f"Document count ({len(docs)}) does not match embedding count ({len(embeddings)}).")

        #prepare data for insertion
        ids = []
        metadatas = []
        document_texts = []
        embedding_lists = []

        for i, (doc, embedding) in enumerate(zip(docs, embeddings)):
            doc_id = str(uuid.uuid4()) # Generate a unique ID for each document chunk
            ids.append(doc_id)

            #prepare metadata for each chunk
            metadata = self._sanitize_metadata(dict(doc.metadata)) if doc.metadata else {}
            metadata['doc_index']=i
            metadata['content_len']=len(doc.page_content if isinstance(doc, Document) else str(doc))
            metadatas.append(metadata)

            document_texts.append(doc.page_content if isinstance(doc, Document) else str(doc))
            embedding_lists.append(embedding.tolist())

        # Insert data into the collection
        try:
            self.collection.add(
                ids=ids,
                metadatas=metadatas,
                documents=document_texts,
                embeddings=embedding_lists
            )
            log(f"Successfully added {len(docs)} documents to the vector store.")
        except Exception as e:
            log(f"Error adding documents to vector store: {e}")
            raise


@lru_cache(maxsize=1)
def get_embedding_manager() -> EmbeddingManager:
    return EmbeddingManager()


@lru_cache(maxsize=1)
def get_vector_store() -> VectorStore:
    return VectorStore(persist_directory=str(VECTOR_DB_DIR))


@lru_cache(maxsize=1)
def get_retriever() -> "RAGretriever":
    return RAGretriever(get_vector_store(), get_embedding_manager())


def ensure_vector_store_populated(force_rebuild: bool = False) -> Dict[str, Any]:
    vector_store = get_vector_store()
    collection_size = vector_store.collection.count()
    if collection_size > 0 and not force_rebuild:
        return {"status": "skipped", "message": f"Vector store already contains {collection_size} chunks"}

    documents = load_files(DATA_DIR)
    if not documents:
        return {"status": "empty", "message": f"No supported documents found in {DATA_DIR}"}

    split_docs = split_documents(documents)
    embeddings = get_embedding_manager().generate_embeddings(split_docs)

    if force_rebuild and collection_size > 0:
        vector_store.client.delete_collection(name=vector_store.collection_name)
        vector_store._initialize_chromadb()

    vector_store.add_docs(split_docs, embeddings)
    return {"status": "ingested", "chunks": len(split_docs), "documents": len(documents)}


#Retreval layer

class RAGretriever:
    """
    Handles querying the vector store and retrieving relevant document chunks based on similarity to the query. 
    """
    def __init__(self, vector_store_manager: VectorStore, embedding_manager: EmbeddingManager, threshold_dist: float = 0.7):
        self.vector_store_manager = vector_store_manager
        self.embedding_manager = embedding_manager
        self.threshold_dist = threshold_dist

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Retrieves relevant document chunks based on similarity to the query.
        Args:
            query (str): The input query for which to retrieve relevant documents.
            top_k (int): The number of top similar documents to retrieve.
        Returns:
            List[Dict[str, Any]]: A list of retrieved documents with their metadata and similarity scores.
        """
        # Generate embedding for the query
        query_embedding = self.embedding_manager.generate_embeddings([query])[0]

        # Retrieve all documents and their embeddings from the vector store
        try:
            results = self.vector_store_manager.collection.query(
                query_embeddings=[query_embedding.tolist()],
                n_results=top_k,
                include=["documents", "metadatas", "distances"]
            )

            retrieved_docs = []
            
            if results['documents'] and results['documents'][0]:  # Check if there are any retrieved documents
                for rank, (doc, metadata, distance, id) in enumerate(zip(results['documents'][0], results['metadatas'][0], results['distances'][0], results['ids'][0])):
                    # similarity_score = 1 - distance  # Convert distance to similarity score (assuming distance is between 0 and 1)
                    if distance> self.threshold_dist:
                        break  # Stop if distance exceeds the threshold

                    retrieved_docs.append({
                        "document": doc,
                        "metadata": metadata,
                        # "similarity_score": similarity_score,
                        "distance": distance,
                        "doc_id": id,
                        "rank": rank + 1  # Rank starts at 1
                    })
            else:
                log("No documents retrieved for the query.")
                return []
            return retrieved_docs
        except Exception as e:
            log(f"Error retrieving documents: {e}")
            raise

#Generation layer

def generate_answer(query: str, retrieved_docs: List[Dict[str, Any]], llm_model_name: str = "gemini-3-flash-preview") -> str:
    # Generate answer using LLM

    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GOOGLE_API_KEY is not set. Add it to your environment or .env file.")

    llm = ChatGoogleGenerativeAI(
        model=llm_model_name,
        temperature=0,
        google_api_key=api_key,
    )

    # Concatenate retrieved documents
    context = "\n".join([doc['document'] for doc in retrieved_docs])

    # Create prompt
    prompt = f"Answer the following question based on the provided context. If the context doesn't contain enough information, say so.\n\nQuestion: {query}\n\nContext:\n{context}"

    # Generate answer
    response = llm.invoke(prompt)
    if isinstance(response.content, list):
        answer = "\n".join(part.get("text", "") for part in response.content if isinstance(part, dict)).strip()
    else:
        answer = response.content

    return answer

@mcp.tool()
def ingest_documents(force_rebuild: bool = False) -> Dict[str, Any]:
    """Build or refresh the persistent Chroma index from local documents."""
    return ensure_vector_store_populated(force_rebuild=force_rebuild)


@mcp.tool()
def retrieve_relevant_chunks(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """Retrieve the most relevant chunks for a query."""
    return get_retriever().retrieve(query=query, top_k=top_k)


@mcp.tool()
def answer_question(query: str, top_k: int = 5, llm_model_name: str = "gemini-3-flash-preview") -> Dict[str, Any]:
    """Run retrieval-augmented generation end to end for a user question."""
    retrieved_docs = get_retriever().retrieve(query=query, top_k=top_k)
    answer = generate_answer(query=query, retrieved_docs=retrieved_docs, llm_model_name=llm_model_name)
    return {
        "query": query,
        "answer": answer,
        "retrieved_docs": retrieved_docs,
    }

if __name__ == "__main__":
    mcp.run()

