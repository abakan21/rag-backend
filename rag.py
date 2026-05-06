import os
from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import MarkdownTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_qdrant import QdrantVectorStore

QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_LOCAL_PATH = os.getenv("QDRANT_LOCAL_PATH", "")

embeddings = HuggingFaceEmbeddings(model_name="paraphrase-multilingual-MiniLM-L12-v2")
collection_name = "firecrawl_docs_v3"

if QDRANT_LOCAL_PATH:
    qdrant_client = QdrantClient(path=QDRANT_LOCAL_PATH)
else:
    qdrant_client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

try:
    qdrant_client.get_collection(collection_name)
except Exception:
    from qdrant_client.http.models import Distance, VectorParams

    qdrant_client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=384, distance=Distance.COSINE),
    )


def index_markdown_file(file_path: str, url: str, job_id: int):
    print(f"Indexing {file_path} into VectorDB...")
    loader = TextLoader(file_path, encoding="utf-8")
    docs = loader.load()

    splitter = MarkdownTextSplitter(chunk_size=1000, chunk_overlap=150)
    chunks = []

    split_docs = splitter.split_documents(docs)
    for chunk in split_docs:
        chunk.metadata["url"] = url
        chunk.metadata["job_id"] = str(job_id)
        chunk.metadata["filename"] = os.path.basename(file_path)
        chunks.append(chunk)

    if not chunks:
        print("No chunks extracted from file.")
        return

    vector_store = QdrantVectorStore(
        client=qdrant_client, collection_name=collection_name, embedding=embeddings
    )
    vector_store.add_documents(chunks)
    print(f"Indexed {len(chunks)} chunks successfully.")


def delete_job_vectors(job_id: int):
    qdrant_client.delete(
        collection_name=collection_name,
        points_selector=qdrant_models.FilterSelector(
            filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="metadata.job_id",
                        match=qdrant_models.MatchValue(value=str(job_id)),
                    ),
                ]
            )
        ),
    )
    print(f"Deleted vectors for job_id: {job_id}")


def search_documents(query: str, k: int = 3):
    vector_store = QdrantVectorStore(
        client=qdrant_client, collection_name=collection_name, embedding=embeddings
    )

    results = vector_store.similarity_search_with_score(query, k=k)

    formatted_results = []
    for doc, score in results:
        formatted_results.append(
            {
                "content": doc.page_content,
                "metadata": doc.metadata,
                "similarity_score": score,
            }
        )

    return formatted_results


def query_rag(query: str, k: int = 5):
    from langchain_community.llms import Ollama
    from langchain_core.prompts import PromptTemplate
    import warnings

    warnings.filterwarnings("ignore")

    context_chunks = search_documents(query, k=k)

    if not context_chunks:
        return {
            "answer": "Nebyly nalezeny žádné relevantní informace v databázi.",
            "sources": [],
        }

    context_text = "\n\n---\n\n".join([chunk["content"] for chunk in context_chunks])

    template = """
    Jsi AI asistent pro RAG (Retrieval-Augmented Generation) systém. 
    Tvým úkolem je odpovídat na otázky uživatele VÝHRADNĚ na základě poskytnutého kontextu.

    Pravidla:
    1. Odpovídej v jazyce, ve kterém je položena otázka (typicky česky).
    2. Pokud odpověď v kontextu NENÍ, řekni slušně, že ji neznáš. Nevymýšlej si fakta.
    3. Hledej informace pečlivě i v popisech obrázků (alt texty), odkazech nebo seznamech.
    4. Buď věcný a stručný.

    Kontext:
    {context}

    Otázka: {question}

    Odpověď:"""

    prompt = PromptTemplate(template=template, input_variables=["context", "question"])

    from langchain_openai import ChatOpenAI

    api_base = os.getenv("OLLAMA_URL", "https://llm.ai.e-infra.cz/v1" )
    api_key = os.getenv("OLLAMA_API_KEY", "")
    model_name = os.getenv("LLM_MODEL_NAME", "llama3.3:latest")

    print(f"Initializing LLM at {api_base}. Generating response...")
    try:
        llm = ChatOpenAI(
            model=model_name,
            openai_api_base=api_base,
            openai_api_key=api_key,
            temperature=0.1
        )
        response = llm.invoke(prompt.format(context=context_text, question=query))
        answer = response.content
    except Exception as e:
        print(f"LLM generation failed: {e}")
        answer = "Bohužel se nepodařilo spojit s LLM modelem nebo generování selhalo."

    sources = []
    seen_urls = set()
    for r in context_chunks:
        url = r["metadata"].get("url", "unknown")
        filename = r["metadata"].get("filename", "")
        fragment = r["content"][:300] if r["content"] else ""
        job_id = r["metadata"].get("job_id", "")
        if url not in seen_urls:
            sources.append(
                {
                    "path": url,
                    "score": r["similarity_score"],
                    "filename": filename,
                    "fragment": fragment,
                    "job_id": job_id,
                }
            )
            seen_urls.add(url)

    return {"answer": answer, "sources": sources}
