"""AI integration routes — embedding, similarity search, and LLM analysis."""

import asyncio
import hashlib
import json
import math
import re
import sqlite3
import statistics
import struct

from collections.abc import Callable

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

# Cancellation tokens. Each embedding task takes the next number at handler entry;
# POST /ai/cancel marks the newest task (and thereby every older one) as cancelled,
# so a cancel can neither be erased by a later task nor spill over onto it.
_latest_task = 0
_cancelled_up_to = 0


def _begin_cancellable_task() -> int:
    global _latest_task
    _latest_task += 1
    return _latest_task


def _check_cancelled(task: int) -> None:
    if task <= _cancelled_up_to:
        raise HTTPException(409, "Cancelled")

# Source types whose text AQDA embeds for search. text/pdf embed `content`;
# audio embeds its (non-empty) transcript, so offsets index into the transcript.
_EMBEDDABLE_SOURCE_SQL = (
    "source_type IN ('text', 'pdf') "
    "OR (source_type='audio' AND transcript IS NOT NULL AND TRIM(transcript) <> '')"
)


def _embed_text_for(row) -> str:
    """The text AQDA embeds for a document row: transcript for audio, else content."""
    text = row["transcript"] if row["source_type"] == "audio" else row["content"]
    return text or ""


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
    expected = 1 if isinstance(text, str) else len(text)
    if len(data["embeddings"]) != expected:
        raise ValueError("Ollama returned an incomplete embedding batch")
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


def _chunk_text(
    text: str, chunk_size: int = 500, overlap: int = 50, mode: str = "fixed"
) -> list[dict]:
    """Split text into overlapping chunks, returning offset info."""
    if mode == "paragraph":
        return _chunk_paragraphs(text, chunk_size, overlap)
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
        # Always advance. When overlap is large relative to a short (sentence-snapped)
        # chunk, `end - overlap` can land at or before `start`; force forward progress
        # so the loop can never spin forever.
        start = max(end - overlap, start + 1)
    return chunks


def _chunk_paragraphs(text: str, chunk_size: int, overlap: int) -> list[dict]:
    """One chunk per non-empty line (a paragraph or speaker turn); long lines use fixed windows.

    Suited to transcripts with one turn per line. Text with a hard line break at
    every line (some PDFs) produces many tiny chunks, which the Settings page warns about.
    """
    chunks = []
    for match in re.finditer(r"[^\n]+", text):
        line = match.group()
        stripped = line.strip()
        if not stripped:
            continue
        line_start = match.start() + (len(line) - len(line.lstrip()))
        if len(stripped) <= chunk_size:
            chunks.append({"text": stripped, "start": line_start, "end": line_start + len(stripped)})
            continue
        for chunk in _chunk_text(stripped, chunk_size, overlap):
            chunks.append({
                "text": chunk["text"],
                "start": line_start + chunk["start"],
                "end": line_start + chunk["end"],
            })
    return chunks


def _chunk_id(doc_id: int, start: int, end: int, model: str, text: str) -> str:
    """Deterministic ID for a chunk embedding."""
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    raw = f"{doc_id}:{start}:{end}:{model}:{content_hash}"
    return hashlib.md5(raw.encode()).hexdigest()


_SENTENCE_END = (".", "!", "?", "\n")


def _snap_to_sentences(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand a [start, end) span outward to the nearest sentence boundaries.

    Keeps suggested codings from beginning mid-sentence, so 'accepting' a
    suggestion applies a clean, readable span.
    """
    n = len(text)
    start = max(0, min(start, n))
    end = max(start, min(end, n))
    # Walk the start back to just after the previous sentence terminator.
    s = start
    while s > 0 and text[s - 1] not in _SENTENCE_END:
        s -= 1
    while s < end and text[s] in " \t\r\n":
        s += 1
    # Walk the end forward to finish the current sentence.
    e = end
    while e < n and text[e - 1] not in _SENTENCE_END:
        e += 1
    while e > s and text[e - 1] in " \t\r\n":
        e -= 1
    if e <= s:
        return start, end
    return s, e


def _finalize_suggestions(
    results: list[dict],
    get_text: Callable[[int], str | None],
    existing: dict[int, list[tuple[int, int]]],
    top_k: int,
) -> list[dict]:
    """Snap raw chunks to sentences, then drop duplicates and already-coded spans.

    Snapping can widen a chunk into a passage that already carries this code, and
    two overlapping chunks can snap to the same sentences, so both checks must run
    on the final spans: otherwise 'Apply' would create overlapping duplicate codings.
    Document text is fetched through ``get_text`` only for documents actually
    reached before ``top_k`` suggestions survive.
    """
    seen: set[tuple[int, int, int]] = set()
    final: list[dict] = []
    texts: dict[int, str] = {}
    for result in results:
        doc_id = result["document_id"]
        start, end, text = result["start_pos"], result["end_pos"], result["text"]
        if doc_id not in texts:
            texts[doc_id] = get_text(doc_id) or ""
        content = texts[doc_id]
        if content:
            start, end = _snap_to_sentences(content, start, end)
            text = content[start:end]
        if not text.strip() or (doc_id, start, end) in seen:
            continue
        if any(start < c_end and end > c_start for c_start, c_end in existing.get(doc_id, [])):
            continue
        seen.add((doc_id, start, end))
        final.append({**result, "start_pos": start, "end_pos": end, "text": text})
        if len(final) >= top_k:
            break
    return final


EMBED_BATCH_SIZE = 10  # embed this many chunks per Ollama call


async def _ensure_doc_embedded(
    doc_id: int, doc_content: str, project_id: int,
    embed_model: str, ollama_url: str,
    chunk_size: int, chunk_overlap: int, chunk_mode: str = "fixed",
    task: int = 0,
):
    """Embed a document's chunks into SQLite cache if not already stored."""
    chunks = _chunk_text(doc_content, chunk_size, chunk_overlap, chunk_mode)
    if not chunks:
        conn = _sync_db()
        try:
            conn.execute(
                "DELETE FROM embedding_cache WHERE document_id=? AND model=?",
                (doc_id, embed_model),
            )
            conn.commit()
        finally:
            conn.close()
        return

    chunk_ids = [
        _chunk_id(doc_id, c["start"], c["end"], embed_model, c["text"])
        for c in chunks
    ]

    # Step 1: Compare with cached chunks in Python; paragraph mode can produce far
    # more chunks than SQLite accepts as bound parameters in a single statement.
    wanted_ids = set(chunk_ids)
    conn = _sync_db()
    try:
        cursor = conn.execute(
            "SELECT id FROM embedding_cache WHERE document_id=? AND model=?",
            (doc_id, embed_model),
        )
        cached_ids = {row["id"] for row in cursor.fetchall()}
        stale_ids = sorted(cached_ids - wanted_ids)
        for batch_start in range(0, len(stale_ids), 500):
            batch = stale_ids[batch_start:batch_start + 500]
            conn.execute(
                f"DELETE FROM embedding_cache WHERE id IN ({','.join('?' * len(batch))})",
                batch,
            )
        conn.commit()
        existing_ids = cached_ids & wanted_ids
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
        _check_cancelled(task)
        batch = new_chunks[batch_start:batch_start + EMBED_BATCH_SIZE]
        texts = [chunk["text"] for _, chunk in batch]
        try:
            embeddings = await _ollama_embed(texts, embed_model, ollama_url)
            for (cid, chunk), emb in zip(batch, embeddings):
                # Pack immediately to free the float list
                embedded.append((cid, chunk, _pack_embedding(emb)))
        except httpx.ConnectError:
            raise HTTPException(503, "Cannot connect to Ollama. Make sure it is running.")
        except Exception as exc:
            # Never report a search over only the batches that happened to succeed.
            raise HTTPException(502, f"Ollama embedding failed: {exc}")

    # A cancel that arrived during the last Ollama call must not be followed by writes.
    _check_cancelled(task)
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


@router.post("/cancel")
async def cancel_ai_task():
    """Stop the running embedding task after its current batch; cached chunks are kept."""
    global _cancelled_up_to
    _cancelled_up_to = _latest_task
    return {"cancelled": True}


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
    task = _begin_cancellable_task()  # registered before the first await
    settings = await _get_settings()
    ollama_url = settings.get("ollama_url", "http://localhost:11434")
    embed_model = req.embedding_model or settings.get("embedding_model", "nomic-embed-text")
    chunk_size = int(settings.get("chunk_size", "500"))
    chunk_overlap = int(settings.get("chunk_overlap", "50"))
    chunk_mode = settings.get("chunk_mode", "fixed")

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
                f"WHERE project_id=? AND id IN ({placeholders}) "
                f"AND ({_EMBEDDABLE_SOURCE_SQL}) "
                f"AND COALESCE(exclude_from_ai, 0)=0",
                [req.project_id] + req.document_ids,
            )
        else:
            cursor = await db.execute(
                f"SELECT id, name, source_type FROM document "
                f"WHERE project_id=? AND ({_EMBEDDABLE_SOURCE_SQL}) "
                f"AND COALESCE(exclude_from_ai, 0)=0",
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
            _check_cancelled(task)
            _embedding_progress["current"] = i + 1
            _embedding_progress["doc_name"] = doc["name"]
            # Load content with sync sqlite3 (avoids aiosqlite memory leak)
            conn = _sync_db()
            try:
                cursor = conn.execute(
                    "SELECT content, transcript, source_type FROM document WHERE id=?",
                    (doc["id"],),
                )
                row = cursor.fetchone()
                content = _embed_text_for(row) if row else ""
            finally:
                conn.close()
            await _ensure_doc_embedded(
                doc["id"], content, req.project_id,
                embed_model, ollama_url, chunk_size, chunk_overlap, chunk_mode, task,
            )
    finally:
        _embedding_progress["active"] = False

    # Embed query and search
    _check_cancelled(task)
    try:
        query_embedding = await _ollama_embed(query_text, embed_model, ollama_url)
    except httpx.ConnectError:
        raise HTTPException(503, "Cannot connect to Ollama. Make sure it is running.")
    except Exception as e:
        raise HTTPException(502, f"Ollama embedding failed: {e}")

    # Scope the search to the resolved (non-excluded, still-existing) documents so
    # cached chunks from reference/deleted docs never surface in results.
    _check_cancelled(task)
    search_doc_ids = [doc["id"] for doc in docs]
    results = await _search_embeddings(
        query_embedding, req.project_id, embed_model,
        top_k=req.top_k, document_ids=search_doc_ids,
    )

    # Add document names
    doc_names = {doc["id"]: doc["name"] for doc in docs}
    for r in results:
        r["document_name"] = doc_names.get(r["document_id"], "Unknown")
        r["similarity"] = round(max(0, r["similarity"]), 4)

    return results


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

    # Fetch extra results so we still have enough after filtering. The query
    # already carries the code's name and definition, so no code_id here.
    fetch_k = req.top_k * 3 + len(existing_codings)
    similar_results = await find_similar(SimilarSearchRequest(
        project_id=req.project_id,
        query=query,
        top_k=fetch_k,
        embedding_model=req.embedding_model,
    ))
    if not similar_results:
        return []

    # Document text is needed to snap suggestions to sentence boundaries so that
    # they don't start mid-sentence and 'Apply' records a clean span. Only the
    # codeable text is read (never base64 audio), and only in a worker thread.
    # Existing spans are re-read here, after the (possibly long) embedding work,
    # so a coding made in the meantime is respected too.
    def finalize() -> list[dict]:
        conn = _sync_db()
        try:
            codings_by_doc: dict[int, list[tuple[int, int]]] = {}
            for row in conn.execute(
                "SELECT c.document_id, c.start_pos, c.end_pos FROM coding c "
                "JOIN document d ON c.document_id = d.id "
                "WHERE c.code_id=? AND d.project_id=? AND c.deleted_at IS NULL",
                (req.code_id, req.project_id),
            ):
                codings_by_doc.setdefault(row["document_id"], []).append(
                    (row["start_pos"], row["end_pos"])
                )

            def get_text(doc_id: int) -> str | None:
                row = conn.execute(
                    "SELECT CASE WHEN source_type='audio' THEN transcript ELSE content END "
                    "AS text FROM document WHERE id=?",
                    (doc_id,),
                ).fetchone()
                return row["text"] if row else None

            return _finalize_suggestions(similar_results, get_text, codings_by_doc, req.top_k)
        finally:
            conn.close()

    return await asyncio.to_thread(finalize)


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

MIN_OUTLIER_SEGMENTS = 4  # below this, a code's similarity spread is not meaningful


def _outlier_cutoff(similarities: list[float]) -> float | None:
    """Adaptive cutoff below which a segment counts as an outlier for its code.

    Cosine similarities to a code centroid depend on the embedding model (with
    nomic-embed-text even unrelated text scores around 0.6), so a fixed absolute
    threshold never fires. Instead flag segments more than 1.5 standard deviations
    below the code's own mean, requiring a gap of at least 0.05 so that a tightly
    consistent code does not produce false alarms.
    """
    if len(similarities) < MIN_OUTLIER_SEGMENTS:
        return None
    mean = statistics.fmean(similarities)
    spread = statistics.pstdev(similarities)
    return mean - max(1.5 * spread, 0.05)


class ConsistencyCheckRequest(BaseModel):
    project_id: int
    code_id: int | None = None
    # Explicit absolute cutoff; None selects the adaptive per-code rule.
    similarity_threshold: float | None = None
    embedding_model: str | None = None


@router.post("/consistency-check")
async def consistency_check(req: ConsistencyCheckRequest):
    """Check coding consistency by flagging segments that are outliers for their code."""
    task = _begin_cancellable_task()
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

            # Embed segments in batches (one Ollama call per batch, not per segment)
            embeddings = []
            for batch_start in range(0, len(segments), EMBED_BATCH_SIZE):
                _check_cancelled(task)
                batch = segments[batch_start:batch_start + EMBED_BATCH_SIZE]
                try:
                    batch_embs = await _ollama_embed(
                        [s["selected_text"] for s in batch], embed_model, ollama_url
                    )
                    embeddings.extend(batch_embs)
                except httpx.ConnectError:
                    raise HTTPException(
                        503, "Cannot connect to Ollama. Make sure it is running."
                    )
                except Exception as exc:
                    # Missing segments would change the centroid and outlier scores.
                    raise HTTPException(502, f"Ollama embedding failed: {exc}")

            _check_cancelled(task)

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
            cutoff = (
                req.similarity_threshold
                if req.similarity_threshold is not None
                else _outlier_cutoff(similarities)
            )

            outliers = []
            for i, sim in enumerate(similarities):
                if cutoff is not None and sim < cutoff:
                    seg = segments[i]
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
                "segment_count": len(segments),
                "avg_similarity": round(avg_similarity, 4),
                "outlier_cutoff": round(cutoff, 4) if cutoff is not None else None,
                "outliers": outliers,
            })

    finally:
        await db.close()

    return {"results": results}


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
