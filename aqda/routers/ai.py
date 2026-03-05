"""AI integration routes — embedding, similarity search, and LLM analysis."""

import hashlib
import json
import math
import re
import sqlite3
import struct

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx

from aqda.db import get_db, _db_path

router = APIRouter()

# Shared HTTP client for Ollama calls (avoids creating new connections per request)
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=120.0)
    return _http_client


# Track embedding progress for the UI
_embedding_progress: dict = {"active": False, "current": 0, "total": 0, "doc_name": ""}


def _sync_db() -> sqlite3.Connection:
    """Synchronous SQLite connection for embedding operations (avoids aiosqlite memory leak)."""
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_settings() -> dict:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT key, value FROM setting")
        rows = await cursor.fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        await db.close()


async def _ollama_embed(
    text: str | list[str], model: str, ollama_url: str
) -> list[float] | list[list[float]]:
    """Get embeddings from Ollama. Accepts single string or batch of strings."""
    client = _get_http_client()
    resp = await client.post(
        f"{ollama_url}/api/embed",
        json={"model": model, "input": text},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(text, str):
        return data["embeddings"][0]
    return data["embeddings"]


async def _ollama_generate(
    prompt: str, model: str, ollama_url: str, system: str = "", think: bool = False
) -> str:
    """Generate text from Ollama."""
    client = _get_http_client()
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "think": think,
    }
    resp = await client.post(f"{ollama_url}/api/generate", json=payload)
    resp.raise_for_status()
    return resp.json()["response"]


def _pack_embedding(embedding: list[float]) -> bytes:
    """Pack a float list into a compact binary blob."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _unpack_embedding(blob: bytes) -> list[float]:
    """Unpack a binary blob back into a float list."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[dict]:
    """Split text into overlapping chunks, returning offset info."""
    if chunk_size <= overlap:
        overlap = 0
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            for sep in [". ", ".\n", "\n\n", "\n", " "]:
                last_sep = text[start:end].rfind(sep)
                if last_sep > chunk_size // 2:
                    end = start + last_sep + len(sep)
                    break
        chunk_text = text[start:end].strip()
        if chunk_text:
            chunks.append({
                "text": chunk_text,
                "start": start,
                "end": end,
            })
        # Last chunk reached — no overlap needed
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def _chunk_id(doc_id: int, start: int, end: int, model: str) -> str:
    """Deterministic ID for a chunk embedding."""
    raw = f"{doc_id}:{start}:{end}:{model}"
    return hashlib.md5(raw.encode()).hexdigest()


EMBED_BATCH_SIZE = 10  # embed this many chunks per Ollama call


async def _ensure_doc_embedded(
    doc_id: int, doc_content: str, project_id: int,
    embed_model: str, ollama_url: str,
    chunk_size: int, chunk_overlap: int,
):
    """Embed a document's chunks into SQLite cache if not already stored."""
    chunks = _chunk_text(doc_content, chunk_size, chunk_overlap)
    if not chunks:
        return

    chunk_ids = [_chunk_id(doc_id, c["start"], c["end"], embed_model) for c in chunks]

    # Step 1: Check existing chunks (sync sqlite3 — avoids aiosqlite memory leak)
    conn = _sync_db()
    try:
        placeholders = ",".join("?" * len(chunk_ids))
        cursor = conn.execute(
            f"SELECT id FROM embedding_cache WHERE id IN ({placeholders})", chunk_ids
        )
        existing_ids = {row["id"] for row in cursor.fetchall()}
    finally:
        conn.close()

    new_chunks = [
        (cid, chunk) for cid, chunk in zip(chunk_ids, chunks)
        if cid not in existing_ids
    ]
    if not new_chunks:
        return

    # Step 2: Embed via Ollama — no DB connection held open.
    embedded: list[tuple[str, dict, bytes]] = []
    for batch_start in range(0, len(new_chunks), EMBED_BATCH_SIZE):
        batch = new_chunks[batch_start:batch_start + EMBED_BATCH_SIZE]
        texts = [chunk["text"] for _, chunk in batch]
        try:
            embeddings = await _ollama_embed(texts, embed_model, ollama_url)
            for (cid, chunk), emb in zip(batch, embeddings):
                # Pack immediately to free the float list
                embedded.append((cid, chunk, _pack_embedding(emb)))
        except Exception:
            continue

    if not embedded:
        return

    # Step 3: Write to DB (sync sqlite3 — avoids aiosqlite memory leak)
    conn = _sync_db()
    try:
        for cid, chunk, emb_blob in embedded:
            conn.execute(
                "INSERT OR IGNORE INTO embedding_cache "
                "(id, document_id, project_id, model, start_pos, end_pos, "
                "chunk_text, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, doc_id, project_id, embed_model, chunk["start"],
                 chunk["end"], chunk["text"], emb_blob),
            )
        conn.commit()
    finally:
        conn.close()
    embedded.clear()


async def _search_embeddings(
    query_embedding: list[float], project_id: int, embed_model: str,
    top_k: int = 10, document_ids: list[int] | None = None,
) -> list[dict]:
    """Search the embedding cache by cosine similarity."""
    conn = _sync_db()
    try:
        if document_ids:
            placeholders = ",".join("?" * len(document_ids))
            cursor = conn.execute(
                f"SELECT document_id, start_pos, end_pos, chunk_text, embedding "
                f"FROM embedding_cache "
                f"WHERE project_id=? AND model=? AND document_id IN ({placeholders})",
                [project_id, embed_model] + document_ids,
            )
        else:
            cursor = conn.execute(
                "SELECT document_id, start_pos, end_pos, chunk_text, embedding "
                "FROM embedding_cache WHERE project_id=? AND model=?",
                (project_id, embed_model),
            )
        rows = cursor.fetchall()
    finally:
        conn.close()

    # Compute similarities
    scored = []
    for row in rows:
        emb = _unpack_embedding(row["embedding"])
        sim = _cosine_similarity(query_embedding, emb)
        scored.append({
            "document_id": row["document_id"],
            "start_pos": row["start_pos"],
            "end_pos": row["end_pos"],
            "text": row["chunk_text"],
            "similarity": sim,
        })

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:top_k]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/embedding-progress")
async def embedding_progress():
    """Check current embedding progress (polled by the frontend)."""
    return _embedding_progress


@router.get("/embedding-status")
async def embedding_status(project_id: int):
    """Check which documents have cached embeddings."""
    conn = _sync_db()
    try:
        cursor = conn.execute(
            "SELECT id, name FROM document "
            "WHERE project_id=? AND source_type IN ('text', 'pdf')",
            (project_id,),
        )
        docs = cursor.fetchall()

        cursor = conn.execute(
            "SELECT DISTINCT document_id FROM embedding_cache WHERE project_id=?",
            (project_id,),
        )
        embedded_ids = {row["document_id"] for row in cursor.fetchall()}
    finally:
        conn.close()

    return {
        "documents": [
            {"id": d["id"], "name": d["name"], "embedded": d["id"] in embedded_ids}
            for d in docs
        ],
        "embedded_count": len(embedded_ids),
        "total_count": len(docs),
    }


class SimilarSearchRequest(BaseModel):
    project_id: int
    query: str
    code_id: int | None = None
    document_ids: list[int] | None = None
    top_k: int = 10
    embedding_model: str | None = None
    llm_model: str | None = None


@router.post("/similar")
async def find_similar(req: SimilarSearchRequest):
    """Find passages similar to a query or code description using embeddings."""
    settings = await _get_settings()
    ollama_url = settings.get("ollama_url", "http://localhost:11434")
    embed_model = req.embedding_model or settings.get("embedding_model", "nomic-embed-text")
    chunk_size = int(settings.get("chunk_size", "500"))
    chunk_overlap = int(settings.get("chunk_overlap", "50"))

    if not embed_model:
        raise HTTPException(
            400, "No embedding model configured. Set one in Settings or select in the AI panel."
        )

    # Build query text
    query_text = req.query
    if req.code_id:
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT name, description FROM code WHERE id=?", (req.code_id,)
            )
            code = await cursor.fetchone()
            if code:
                query_text = f"{code['name']}: {code['description']}\n{query_text}"
        finally:
            await db.close()

    # Get document list (without content — content loaded one at a time during embedding)
    db = await get_db()
    try:
        if req.document_ids:
            placeholders = ",".join("?" * len(req.document_ids))
            cursor = await db.execute(
                f"SELECT id, name, source_type FROM document "
                f"WHERE project_id=? AND id IN ({placeholders}) AND source_type='text'",
                [req.project_id] + req.document_ids,
            )
        else:
            cursor = await db.execute(
                "SELECT id, name, source_type FROM document "
                "WHERE project_id=? AND source_type IN ('text', 'pdf')",
                (req.project_id,),
            )
        docs = await cursor.fetchall()
    finally:
        await db.close()

    if not docs:
        return []

    # Ensure all documents are embedded (load content one at a time to save memory)
    _embedding_progress["active"] = True
    _embedding_progress["total"] = len(docs)
    try:
        for i, doc in enumerate(docs):
            _embedding_progress["current"] = i + 1
            _embedding_progress["doc_name"] = doc["name"]
            # Load content with sync sqlite3 (avoids aiosqlite memory leak)
            conn = _sync_db()
            try:
                cursor = conn.execute(
                    "SELECT content FROM document WHERE id=?", (doc["id"],)
                )
                row = cursor.fetchone()
                content = row["content"] if row else ""
            finally:
                conn.close()
            await _ensure_doc_embedded(
                doc["id"], content, req.project_id,
                embed_model, ollama_url, chunk_size, chunk_overlap,
            )
    finally:
        _embedding_progress["active"] = False

    # Embed query and search
    try:
        query_embedding = await _ollama_embed(query_text, embed_model, ollama_url)
    except httpx.ConnectError:
        raise HTTPException(503, "Cannot connect to Ollama. Make sure it is running.")
    except Exception as e:
        raise HTTPException(503, f"Ollama embedding failed: {e}")

    results = await _search_embeddings(
        query_embedding, req.project_id, embed_model,
        top_k=req.top_k, document_ids=req.document_ids,
    )

    # Add document names
    doc_names = {doc["id"]: doc["name"] for doc in docs}
    for r in results:
        r["document_name"] = doc_names.get(r["document_id"], "Unknown")
        r["similarity"] = round(max(0, r["similarity"]), 4)

    return results


class AnalyzeRequest(BaseModel):
    text: str
    instruction: str = (
        "Analyze this passage from a qualitative research perspective. "
        "Identify key themes, patterns, and notable aspects."
    )
    llm_model: str | None = None


@router.post("/analyze")
async def analyze_text(req: AnalyzeRequest):
    """Use an LLM to analyze a text passage."""
    settings = await _get_settings()
    ollama_url = settings.get("ollama_url", "http://localhost:11434")
    llm_model = req.llm_model or settings.get("llm_model", "")

    if not llm_model:
        raise HTTPException(
            400, "No LLM model configured. Set one in Settings or select in the AI panel."
        )

    system = (
        "You are a qualitative research assistant. You help researchers analyze text data "
        "by identifying themes, patterns, and notable aspects. Be concise and analytical. "
        "Focus on what the text reveals, not on summarizing it."
    )
    prompt = f"{req.instruction}\n\nText passage:\n\"\"\"\n{req.text}\n\"\"\""

    try:
        think = settings.get("think_mode", "off") == "on"
        response = await _ollama_generate(prompt, llm_model, ollama_url, system, think=think)
        return {"analysis": response}
    except httpx.ConnectError:
        raise HTTPException(503, "Cannot connect to Ollama. Make sure it is running.")
    except Exception as e:
        raise HTTPException(503, f"Ollama generation failed: {e}")


class AutoCodeRequest(BaseModel):
    project_id: int
    code_id: int
    top_k: int = 20
    embedding_model: str | None = None


@router.post("/autocode")
async def suggest_codings(req: AutoCodeRequest):
    """Find uncoded passages that might belong to a specific code."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT name, description FROM code WHERE id=?", (req.code_id,)
        )
        code = await cursor.fetchone()
        if not code:
            raise HTTPException(404, "Code not found")

        cursor = await db.execute(
            "SELECT selected_text, document_id, start_pos, end_pos "
            "FROM coding WHERE code_id=? AND deleted_at IS NULL LIMIT 5",
            (req.code_id,),
        )
        examples = await cursor.fetchall()

        # Get all existing codings for this code (to filter out already-coded passages)
        cursor = await db.execute(
            "SELECT c.document_id, c.start_pos, c.end_pos "
            "FROM coding c JOIN document d ON c.document_id = d.id "
            "WHERE c.code_id=? AND d.project_id=? AND c.deleted_at IS NULL",
            (req.code_id, req.project_id),
        )
        existing_codings = await cursor.fetchall()
    finally:
        await db.close()

    query = f"Code: {code['name']}"
    if code["description"]:
        query += f"\nDescription: {code['description']}"
    if examples:
        query += "\nExamples of coded passages:\n"
        for ex in examples:
            query += f"- {ex['selected_text'][:200]}\n"

    # Fetch extra results so we still have enough after filtering
    fetch_k = req.top_k * 3 + len(existing_codings)
    similar_results = await find_similar(SimilarSearchRequest(
        project_id=req.project_id,
        query=query,
        code_id=req.code_id,
        top_k=fetch_k,
        embedding_model=req.embedding_model,
    ))

    # Filter out passages overlapping existing codings for this code
    codings_by_doc: dict[int, list[tuple[int, int]]] = {}
    for ec in existing_codings:
        doc_id = ec["document_id"]
        if doc_id not in codings_by_doc:
            codings_by_doc[doc_id] = []
        codings_by_doc[doc_id].append((ec["start_pos"], ec["end_pos"]))

    filtered = []
    for result in similar_results:
        doc_id = result["document_id"]
        r_start = result["start_pos"]
        r_end = result["end_pos"]
        overlaps = False
        if doc_id in codings_by_doc:
            for c_start, c_end in codings_by_doc[doc_id]:
                if r_start < c_end and r_end > c_start:
                    overlaps = True
                    break
        if not overlaps:
            filtered.append(result)
            if len(filtered) >= req.top_k:
                break

    return filtered


class SummarizeCodeRequest(BaseModel):
    project_id: int
    code_id: int
    llm_model: str | None = None


@router.post("/summarize-code")
async def summarize_code(req: SummarizeCodeRequest):
    """Generate an LLM summary of all passages coded under a specific code."""
    settings = await _get_settings()
    ollama_url = settings.get("ollama_url", "http://localhost:11434")
    llm_model = req.llm_model or settings.get("llm_model", "")

    if not llm_model:
        raise HTTPException(
            400, "No LLM model configured. Set one in Settings or select in the AI panel."
        )

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT name, description FROM code WHERE id=? AND project_id=?",
            (req.code_id, req.project_id),
        )
        code = await cursor.fetchone()
        if not code:
            raise HTTPException(404, "Code not found")

        cursor = await db.execute(
            "SELECT c.selected_text FROM coding c "
            "JOIN document d ON c.document_id = d.id "
            "WHERE c.code_id=? AND d.project_id=? AND c.deleted_at IS NULL",
            (req.code_id, req.project_id),
        )
        segments = await cursor.fetchall()
    finally:
        await db.close()

    if not segments:
        return {"summary": "No coded segments found for this code.", "segment_count": 0}

    code_name = code["name"]
    code_description = code["description"] or "No description provided"
    segment_texts = "\n\n---\n\n".join(
        f'Passage {i+1}: "{seg["selected_text"]}"' for i, seg in enumerate(segments)
    )

    system = (
        "You are a qualitative research assistant. Below are all text passages that have been "
        f"coded under the code '{code_name}'. The code is defined as: '{code_description}'. "
        "Synthesize these passages into a thematic summary. Identify key patterns, variations, "
        "and notable aspects across all the passages. Be analytical, not just descriptive."
    )
    prompt = f"Here are {len(segments)} coded passages:\n\n{segment_texts}"

    try:
        think = settings.get("think_mode", "off") == "on"
        response = await _ollama_generate(prompt, llm_model, ollama_url, system, think=think)
        return {"summary": response, "segment_count": len(segments)}
    except httpx.ConnectError:
        raise HTTPException(503, "Cannot connect to Ollama. Make sure it is running.")
    except Exception as e:
        raise HTTPException(503, f"Ollama generation failed: {e}")


# ---------------------------------------------------------------------------
# 1. POST /ai/consistency-check — Code Consistency Checker
# ---------------------------------------------------------------------------

class ConsistencyCheckRequest(BaseModel):
    project_id: int
    code_id: int | None = None
    similarity_threshold: float = 0.3
    embedding_model: str | None = None


@router.post("/consistency-check")
async def consistency_check(req: ConsistencyCheckRequest):
    """Check coding consistency by flagging segments that are outliers for their code."""
    settings = await _get_settings()
    ollama_url = settings.get("ollama_url", "http://localhost:11434")
    embed_model = req.embedding_model or settings.get("embedding_model", "nomic-embed-text")

    if not embed_model:
        raise HTTPException(
            400, "No embedding model configured. Set one in Settings or select in the AI panel."
        )

    db = await get_db()
    try:
        if req.code_id is not None:
            cursor = await db.execute(
                "SELECT id, name FROM code "
                "WHERE id=? AND project_id=? AND deleted_at IS NULL",
                (req.code_id, req.project_id),
            )
        else:
            cursor = await db.execute(
                "SELECT id, name FROM code WHERE project_id=? AND deleted_at IS NULL",
                (req.project_id,),
            )
        codes = await cursor.fetchall()

        if not codes:
            return {"results": []}

        results = []
        for code in codes:
            cursor = await db.execute(
                "SELECT c.id AS coding_id, c.document_id, c.selected_text, "
                "c.start_pos, c.end_pos, d.name AS document_name "
                "FROM coding c "
                "JOIN document d ON c.document_id = d.id "
                "WHERE c.code_id=? AND d.project_id=? AND c.deleted_at IS NULL",
                (code["id"], req.project_id),
            )
            segments = await cursor.fetchall()

            if len(segments) < 2:
                continue

            # Embed each segment
            embeddings = []
            valid_segments = []
            for seg in segments:
                try:
                    emb = await _ollama_embed(
                        seg["selected_text"], embed_model, ollama_url
                    )
                    embeddings.append(emb)
                    valid_segments.append(seg)
                except httpx.ConnectError:
                    raise HTTPException(
                        503, "Cannot connect to Ollama. Make sure it is running."
                    )
                except Exception:
                    continue

            if len(valid_segments) < 2:
                continue

            # Compute centroid
            dim = len(embeddings[0])
            centroid = [0.0] * dim
            for emb in embeddings:
                for j in range(dim):
                    centroid[j] += emb[j]
            for j in range(dim):
                centroid[j] /= len(embeddings)

            similarities = [_cosine_similarity(emb, centroid) for emb in embeddings]
            avg_similarity = sum(similarities) / len(similarities)

            outliers = []
            for i, sim in enumerate(similarities):
                if sim < req.similarity_threshold:
                    seg = valid_segments[i]
                    outliers.append({
                        "coding_id": seg["coding_id"],
                        "document_id": seg["document_id"],
                        "document_name": seg["document_name"],
                        "selected_text": seg["selected_text"],
                        "start_pos": seg["start_pos"],
                        "end_pos": seg["end_pos"],
                        "similarity": round(sim, 4),
                    })

            results.append({
                "code_id": code["id"],
                "code_name": code["name"],
                "segment_count": len(valid_segments),
                "avg_similarity": round(avg_similarity, 4),
                "outliers": outliers,
            })

    finally:
        await db.close()

    return {"results": results}


# ---------------------------------------------------------------------------
# 2. POST /ai/negative-cases — Negative Case Finder
# ---------------------------------------------------------------------------

class NegativeCaseRequest(BaseModel):
    project_id: int
    code_id: int
    top_k: int = 10
    embedding_model: str | None = None


@router.post("/negative-cases")
async def find_negative_cases(req: NegativeCaseRequest):
    """Find uncoded passages semantically similar to a code — potential negative cases."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT name, description FROM code WHERE id=?", (req.code_id,)
        )
        code = await cursor.fetchone()
        if not code:
            raise HTTPException(404, "Code not found")

        cursor = await db.execute(
            "SELECT c.selected_text, c.document_id, c.start_pos, c.end_pos "
            "FROM coding c "
            "JOIN document d ON c.document_id = d.id "
            "WHERE c.code_id=? AND d.project_id=? AND c.deleted_at IS NULL",
            (req.code_id, req.project_id),
        )
        existing_codings = await cursor.fetchall()
    finally:
        await db.close()

    query = f"Code: {code['name']}"
    if code["description"]:
        query += f"\nDescription: {code['description']}"
    if existing_codings:
        query += "\nExamples of coded passages:\n"
        for ex in existing_codings[:5]:
            query += f"- {ex['selected_text'][:200]}\n"

    fetch_k = req.top_k * 3 + len(existing_codings)
    similar_results = await find_similar(SimilarSearchRequest(
        project_id=req.project_id,
        query=query,
        code_id=req.code_id,
        top_k=fetch_k,
        embedding_model=req.embedding_model,
    ))

    # Filter out passages overlapping existing codings for this code
    codings_by_doc: dict[int, list[tuple[int, int]]] = {}
    for ec in existing_codings:
        doc_id = ec["document_id"]
        if doc_id not in codings_by_doc:
            codings_by_doc[doc_id] = []
        codings_by_doc[doc_id].append((ec["start_pos"], ec["end_pos"]))

    filtered = []
    for result in similar_results:
        doc_id = result["document_id"]
        r_start = result["start_pos"]
        r_end = result["end_pos"]

        overlaps = False
        if doc_id in codings_by_doc:
            for c_start, c_end in codings_by_doc[doc_id]:
                if r_start < c_end and r_end > c_start:
                    overlaps = True
                    break

        if not overlaps:
            filtered.append(result)
            if len(filtered) >= req.top_k:
                break

    return filtered


# ---------------------------------------------------------------------------
# 3. POST /ai/suggest-hierarchy — Codebook Hierarchy Suggester
# ---------------------------------------------------------------------------

class HierarchySuggestRequest(BaseModel):
    project_id: int
    llm_model: str | None = None


@router.post("/suggest-hierarchy")
async def suggest_hierarchy(req: HierarchySuggestRequest):
    """Ask an LLM to suggest a hierarchical grouping for the project's codebook."""
    settings = await _get_settings()
    ollama_url = settings.get("ollama_url", "http://localhost:11434")
    llm_model = req.llm_model or settings.get("llm_model", "")

    if not llm_model:
        raise HTTPException(
            400, "No LLM model configured. Set one in Settings or select in the AI panel."
        )

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT c.id, c.name, c.description, "
            "(SELECT COUNT(*) FROM coding cg "
            " WHERE cg.code_id = c.id AND cg.deleted_at IS NULL) AS coding_count "
            "FROM code c WHERE c.project_id=? AND c.deleted_at IS NULL "
            "ORDER BY c.name",
            (req.project_id,),
        )
        codes = await cursor.fetchall()

        if not codes:
            return {"groups": [], "standalone": []}

        code_examples: dict[int, list[str]] = {}
        for code in codes:
            cursor = await db.execute(
                "SELECT selected_text FROM coding "
                "WHERE code_id=? AND deleted_at IS NULL LIMIT 3",
                (code["id"],),
            )
            examples = await cursor.fetchall()
            code_examples[code["id"]] = [ex["selected_text"][:150] for ex in examples]
    finally:
        await db.close()

    code_lines = []
    for i, code in enumerate(codes, 1):
        line = f'{i}. "{code["name"]}" ({code["coding_count"]} segments)'
        if code["description"]:
            line += f" - Description: {code['description']}"
        examples = code_examples.get(code["id"], [])
        if examples:
            example_strs = ", ".join(f'"{ex}"' for ex in examples)
            line += f" Examples: {example_strs}"
        code_lines.append(line)

    codes_text = "\n".join(code_lines)

    system = "You are a qualitative research methods expert."
    prompt = (
        "Below is a codebook with codes used in a qualitative analysis project. "
        "Suggest a hierarchical organization by grouping related codes under "
        "parent categories.\n\n"
        "Rules:\n"
        "- Suggest new parent categories where helpful\n"
        "- A code can only belong to one parent\n"
        "- Not every code needs a parent — leave standalone codes as-is\n"
        "- Return ONLY valid JSON\n\n"
        f"Codes:\n{codes_text}\n\n"
        'Return JSON format:\n'
        '{\n'
        '  "groups": [\n'
        '    {\n'
        '      "suggested_parent": "Category Name",\n'
        '      "description": "What this group captures",\n'
        '      "children": ["Code1", "Code2"]\n'
        '    }\n'
        '  ],\n'
        '  "standalone": ["CodeX", "CodeY"]\n'
        '}'
    )

    try:
        think = settings.get("think_mode", "off") == "on"
        response = await _ollama_generate(prompt, llm_model, ollama_url, system, think=think)
    except httpx.ConnectError:
        raise HTTPException(503, "Cannot connect to Ollama. Make sure it is running.")
    except Exception as e:
        raise HTTPException(503, f"Ollama generation failed: {e}")

    # Try to parse JSON from the response
    try:
        parsed = json.loads(response)
        return parsed
    except json.JSONDecodeError:
        pass

    json_match = re.search(r'\{[\s\S]*\}', response)
    if json_match:
        try:
            parsed = json.loads(json_match.group())
            return parsed
        except json.JSONDecodeError:
            pass

    return {"error": "Could not parse JSON from LLM response", "raw_response": response}


# ---------------------------------------------------------------------------
# 4. POST /ai/generate-definition — Code Definition Generator
# ---------------------------------------------------------------------------

class GenerateDefinitionRequest(BaseModel):
    project_id: int
    code_id: int
    llm_model: str | None = None


@router.post("/generate-definition")
async def generate_definition(req: GenerateDefinitionRequest):
    """Generate a code definition from its coded passages using an LLM."""
    settings = await _get_settings()
    ollama_url = settings.get("ollama_url", "http://localhost:11434")
    llm_model = req.llm_model or settings.get("llm_model", "")

    if not llm_model:
        raise HTTPException(
            400, "No LLM model configured. Set one in Settings or select in the AI panel."
        )

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT name, description FROM code "
            "WHERE id=? AND project_id=? AND deleted_at IS NULL",
            (req.code_id, req.project_id),
        )
        code = await cursor.fetchone()
        if not code:
            raise HTTPException(404, "Code not found")

        cursor = await db.execute(
            "SELECT c.selected_text FROM coding c "
            "JOIN document d ON c.document_id = d.id "
            "WHERE c.code_id=? AND d.project_id=? AND c.deleted_at IS NULL "
            "LIMIT 50",
            (req.code_id, req.project_id),
        )
        segments = await cursor.fetchall()
    finally:
        await db.close()

    if not segments:
        return {"definition": "No coded segments found for this code.", "segment_count": 0}

    code_name = code["name"]
    current_desc = code["description"]

    passage_lines = "\n".join(
        f'{i+1}. "{seg["selected_text"]}"' for i, seg in enumerate(segments)
    )

    desc_line = ""
    if current_desc:
        desc_line = f"Current description: {current_desc}\n\n"

    system = "You are a qualitative research methods expert."
    prompt = (
        f'A researcher has applied the code "{code_name}" to the following text passages. '
        "Based on these passages, write a concise, precise definition for this code that "
        "captures what it means in this research context.\n\n"
        f"{desc_line}"
        f"Coded passages:\n{passage_lines}\n\n"
        "Write a definition that:\n"
        "- Captures the core meaning across all passages\n"
        "- Is specific enough to guide future coding decisions\n"
        "- Notes any important variations or sub-themes\n"
        "- Is 2-4 sentences long"
    )

    try:
        think = settings.get("think_mode", "off") == "on"
        response = await _ollama_generate(prompt, llm_model, ollama_url, system, think=think)
        return {"definition": response, "segment_count": len(segments)}
    except httpx.ConnectError:
        raise HTTPException(503, "Cannot connect to Ollama. Make sure it is running.")
    except Exception as e:
        raise HTTPException(503, f"Ollama generation failed: {e}")
