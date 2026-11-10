"""AI integration routes — embedding, similarity search, and LLM analysis."""

import asyncio
from collections import deque
import hashlib
import json
import math
import re
import sqlite3
import statistics
import struct
import time

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


# Ollama unloads a model five minutes after its last use unless told otherwise, and loading
# it again costs seconds to minutes. Each request asks to keep the model for this long, so
# reading and coding between two AI tasks does not mean waiting for the model again.
OLLAMA_KEEP_ALIVE = "30m"

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
        json={"model": model, "input": text, "keep_alive": OLLAMA_KEEP_ALIVE},
    )
    resp.raise_for_status()
    data = resp.json()
    expected = 1 if isinstance(text, str) else len(text)
    if len(data["embeddings"]) != expected:
        raise ValueError("Ollama returned an incomplete embedding batch")
    if isinstance(text, str):
        return data["embeddings"][0]
    return data["embeddings"]


# Ollama silently cuts prompts that exceed its context window, so AQDA sets the window
# explicitly and fits the passages it sends into it (see _fit_passages).
LLM_CONTEXT_TOKENS = 8192
# The answer's length limit, kept free in the window. The model's reasoning in think mode
# counts towards it, so that mode reserves more.
LLM_RESPONSE_TOKENS = 2048
LLM_THINKING_RESPONSE_TOKENS = 4096
# Appended when the model stops at the length limit, so a cut-off answer is never silent.
LENGTH_LIMIT_NOTE = "\n\n[The answer reached AQDA's length limit and may be incomplete.]"
REASONING_ONLY_MESSAGE = (
    "The model used the whole answer length for reasoning and did not get to its answer. "
    "Run it again, or turn off thinking mode in Settings."
)
EMPTY_ANSWER_MESSAGE = "The model reached AQDA's length limit without writing an answer."


def _response_tokens(think: bool) -> int:
    """Tokens reserved for the answer (and reasoning) in the context window."""
    return LLM_THINKING_RESPONSE_TOKENS if think else LLM_RESPONSE_TOKENS


# Longest silence AQDA accepts from Ollama while it generates. Before the first word the
# model may still be loading and reading the passages, which takes minutes on slow machines.
LLM_IDLE_TIMEOUT_SECONDS = 600

# Progress of the running generation, polled by the frontend. The answer is read as a stream,
# so AQDA can show that something is happening even though it cannot say how long it will take.
_generation_progress: dict = {"active": False, "phase": "", "words": 0, "started": 0.0}
# Where the time of the last finished generation went, as reported by Ollama (seconds and
# token counts): loading the model, reading the prompt, and writing (reasoning included).
_last_generation: dict = {}


def _generation_timing(final: dict) -> dict:
    """Summarize the timing fields of Ollama's final streamed object (durations are in ns)."""
    def seconds(key: str) -> float:
        return round((final.get(key) or 0) / 1e9, 2)

    return {
        "load_seconds": seconds("load_duration"),
        "prompt_tokens": final.get("prompt_eval_count") or 0,
        "prompt_seconds": seconds("prompt_eval_duration"),
        "answer_tokens": final.get("eval_count") or 0,
        "answer_seconds": seconds("eval_duration"),
        "total_seconds": seconds("total_duration"),
    }


def _count_words(piece: str, words: int, in_word: bool) -> tuple[int, bool]:
    """Continue a running word count over the next piece of streamed text."""
    for char in piece:
        if char.isspace():
            in_word = False
        elif not in_word:
            in_word = True
            words += 1
    return words, in_word


async def _ollama_generate(
    prompt: str, model: str, ollama_url: str, system: str = "", think: bool = False,
    task: int | None = None,
) -> str:
    """Generate text from Ollama within a known context window and answer length.

    The answer is streamed: the time limit applies to each silence rather than to the whole
    answer, progress is recorded in ``_generation_progress``, and a cancelled ``task`` closes
    the connection, which stops the model.
    """
    client = _get_http_client()
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": True,
        "think": think,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {"num_ctx": LLM_CONTEXT_TOKENS, "num_predict": _response_tokens(think)},
    }
    timeout = httpx.Timeout(LLM_IDLE_TIMEOUT_SECONDS, connect=10.0)
    parts: list[str] = []
    done_reason = None
    words, in_word = 0, False
    reasoned = False  # Ollama reports reasoning and writing as one duration
    _generation_progress.update(active=True, phase="waiting", words=0, started=time.monotonic())
    try:
        async with client.stream(
            "POST", f"{ollama_url}/api/generate", json=payload, timeout=timeout
        ) as resp:
            if resp.status_code >= 400:
                # Ollama explains failures (unknown model, no thinking support) in the body.
                body = (await resp.aread()).decode("utf-8", errors="replace")
                try:
                    reason = json.loads(body).get("error") or body
                except (ValueError, AttributeError):
                    reason = body
                raise RuntimeError(str(reason).strip() or f"HTTP status {resp.status_code}")
            async for line in resp.aiter_lines():
                if task is not None:
                    _check_cancelled(task)
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise RuntimeError(str(data["error"]))
                if data.get("thinking"):
                    reasoned = True
                    if not parts:
                        _generation_progress["phase"] = "thinking"
                piece = data.get("response") or ""
                if piece:
                    parts.append(piece)
                    words, in_word = _count_words(piece, words, in_word)
                    _generation_progress.update(phase="writing", words=words)
                done_reason = data.get("done_reason") or done_reason
                if data.get("done"):
                    _last_generation.clear()
                    _last_generation.update(_generation_timing(data), reasoned=reasoned)
                    break
    finally:
        _generation_progress["active"] = False
    response = "".join(parts)
    if done_reason == "length":
        if not response.strip():
            # Reasoning counts towards the answer length and can use all of it.
            raise HTTPException(502, REASONING_ONLY_MESSAGE if think else EMPTY_ANSWER_MESSAGE)
        return response + LENGTH_LIMIT_NOTE
    return response


async def _generate_or_fail(
    prompt: str, model: str, ollama_url: str, system: str, think: bool, task: int,
) -> str:
    """Run a generation and turn its failures into messages a researcher can act on."""
    _last_generation.clear()
    generation = asyncio.ensure_future(
        _ollama_generate(prompt, model, ollama_url, system, think=think, task=task)
    )
    try:
        # Watch for a cancel while waiting: before its first word the model sends nothing,
        # so the check inside the stream would only run once the answer starts.
        while not generation.done():
            await asyncio.wait({generation}, timeout=0.25)
            if not generation.done() and task <= _cancelled_up_to:
                generation.cancel()  # closes the connection, which stops the model
                await asyncio.wait({generation})
                raise HTTPException(409, "Cancelled")
        return generation.result()
    except HTTPException:
        raise  # cancelled, or the model wrote no answer
    except httpx.ConnectError:
        raise HTTPException(503, "Cannot connect to Ollama. Make sure it is running.")
    except httpx.TimeoutException:
        minutes = LLM_IDLE_TIMEOUT_SECONDS // 60
        raise HTTPException(
            504,
            f"The model did not respond for {minutes} minutes, so AQDA stopped waiting. "
            "Try a smaller or faster model, or check whether Ollama is busy with something else.",
        )
    except Exception as exc:
        raise HTTPException(503, f"Ollama generation failed: {exc or type(exc).__name__}")
    finally:
        if not generation.done():
            generation.cancel()


def _estimate_tokens(text: str) -> int:
    """Deliberately high token estimate for fitting passages into the model's window.

    Common tokenizers need about four characters of English prose per token; this counts
    three ASCII letters per token, every digit, punctuation mark, and line break as one,
    and every non-ASCII character (accented letters, other scripts, emoji) as two. It is
    a heuristic with a wide margin, not a guarantee for every model's tokenizer.
    """
    letters = digits_and_marks = other = 0
    for char in text:
        if not char.isascii():
            other += 1
        elif char.isalpha():
            letters += 1
        elif char != " ":
            digits_and_marks += 1
    return letters // 3 + digits_and_marks + 2 * other + 1


def _prompt_budget(think: bool, *fixed_parts: str) -> int:
    """Tokens left for passages once the fixed prompt text and the answer are accounted for."""
    fixed = sum(_estimate_tokens(part) for part in fixed_parts)
    return LLM_CONTEXT_TOKENS - _response_tokens(think) - fixed


def _spread_order(count: int) -> list[int]:
    """Indices 0..count-1 ordered so that every prefix spreads over the whole range.

    Midpoints come first (the middle, then the middles of both halves, and so on).
    """
    order: list[int] = []
    intervals = deque([(0, count)])
    while intervals:
        low, high = intervals.popleft()
        if low >= high:
            continue
        middle = (low + high) // 2
        order.append(middle)
        intervals.append((low, middle))
        intervals.append((middle + 1, high))
    return order


def _fit_passages(passages: list[str], budget_tokens: int, wrapper: str = "") -> list[int]:
    """Indices of the passages to send: all when they fit, else a subset spread over the list.

    ``wrapper`` is the text added around each passage (numbering, separators). Passages are
    taken in spread order (see _spread_order) and any passage that no longer fits the
    remaining budget is skipped, so the subset covers the whole (document-ordered) list
    rather than its beginning, and the same codings always give the same selection.
    """
    extra = _estimate_tokens(wrapper) if wrapper else 0
    costs = [_estimate_tokens(passage) + extra for passage in passages]
    if sum(costs) <= budget_tokens:
        return list(range(len(passages)))
    chosen = []
    remaining = budget_tokens
    for index in _spread_order(len(passages)):
        if costs[index] <= remaining:
            chosen.append(index)
            remaining -= costs[index]
    if not chosen:
        raise HTTPException(
            422, "The coded passages are too long to fit into the model's context window."
        )
    return sorted(chosen)


# Documents the researcher excluded from AI features in the AI panel.
_INCLUDED_DOCUMENT_SQL = "COALESCE(d.exclude_from_ai, 0)=0"


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


async def _embed_and_store(
    conn: sqlite3.Connection, batch: list[tuple[str, int, dict]], project_id: int,
    embed_model: str, ollama_url: str, task: int,
) -> None:
    """Embed one batch of (chunk id, document id, chunk) and store it in the cache."""
    _check_cancelled(task)
    try:
        embeddings = await _ollama_embed(
            [chunk["text"] for _, _, chunk in batch], embed_model, ollama_url
        )
    except httpx.ConnectError:
        raise HTTPException(503, "Cannot connect to Ollama. Make sure it is running.")
    except Exception as exc:
        # Never report a search over only the batches that happened to succeed.
        raise HTTPException(502, f"Ollama embedding failed: {exc}")
    # A cancel that arrived during the Ollama call must not be followed by writes.
    _check_cancelled(task)
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO embedding_cache "
            "(id, document_id, project_id, model, start_pos, end_pos, chunk_text, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (cid, doc_id, project_id, embed_model, chunk["start"], chunk["end"],
                 chunk["text"], _pack_embedding(embedding))
                for (cid, doc_id, chunk), embedding in zip(batch, embeddings)
            ],
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # A document deleted while it was being embedded; the search skips it anyway.
        conn.rollback()


async def _embed_documents(
    docs: list, project_id: int, embed_model: str, ollama_url: str,
    chunk_size: int, chunk_overlap: int, chunk_mode: str, task: int,
) -> None:
    """Embed every chunk of ``docs`` that is not in the SQLite cache yet.

    Chunks go to Ollama in batches that may span documents, so a project of many short
    documents (spreadsheet rows) needs one request per batch rather than one per document.
    Each batch is stored as soon as it is embedded: a cancelled or failed run keeps what it
    finished. Uses sync sqlite3 (avoids an aiosqlite memory leak) and never holds a
    transaction open across an Ollama call.
    """
    conn = _sync_db()
    try:
        cached: dict[int, set[str]] = {}
        for row in conn.execute(
            "SELECT id, document_id FROM embedding_cache WHERE project_id=? AND model=?",
            (project_id, embed_model),
        ):
            cached.setdefault(row["document_id"], set()).add(row["id"])

        pending: list[tuple[str, int, dict]] = []
        for i, doc in enumerate(docs):
            _check_cancelled(task)
            _embedding_progress["current"] = i + 1
            _embedding_progress["doc_name"] = doc["name"]
            # Content is loaded one document at a time to save memory.
            row = conn.execute(
                "SELECT content, transcript, source_type FROM document WHERE id=?", (doc["id"],)
            ).fetchone()
            chunks = _chunk_text(
                _embed_text_for(row) if row else "", chunk_size, chunk_overlap, chunk_mode
            )
            wanted = {
                _chunk_id(doc["id"], c["start"], c["end"], embed_model, c["text"]): c
                for c in chunks
            }
            have = cached.get(doc["id"], set())
            # Chunks cached for an earlier text or chunk setting. Deleted in batches:
            # paragraph mode can produce more chunks than SQLite accepts as parameters.
            stale = sorted(have - wanted.keys())
            for batch_start in range(0, len(stale), 500):
                batch = stale[batch_start:batch_start + 500]
                conn.execute(
                    f"DELETE FROM embedding_cache WHERE id IN ({','.join('?' * len(batch))})",
                    batch,
                )
            if stale:
                conn.commit()
            pending.extend((cid, doc["id"], c) for cid, c in wanted.items() if cid not in have)
            while len(pending) >= EMBED_BATCH_SIZE:
                await _embed_and_store(
                    conn, pending[:EMBED_BATCH_SIZE], project_id, embed_model, ollama_url, task
                )
                del pending[:EMBED_BATCH_SIZE]
            if i % 50 == 49:
                # Let other requests (the progress poll, a cancel) through on a cached project.
                await asyncio.sleep(0)
        if pending:
            await _embed_and_store(conn, pending, project_id, embed_model, ollama_url, task)
    finally:
        conn.close()


def _search_embeddings(
    query_embedding: list[float], project_id: int, embed_model: str,
    top_k: int = 10, document_ids: list[int] | None = None,
) -> list[dict]:
    """Search the embedding cache by cosine similarity (CPU-bound; run in a worker thread)."""
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


@router.get("/generation-progress")
async def generation_progress():
    """What the running generation is doing: waiting for the model, reasoning, or writing."""
    progress = _generation_progress
    seconds = int(time.monotonic() - progress["started"]) if progress["active"] else 0
    return {
        "active": progress["active"],
        "phase": progress["phase"] if progress["active"] else "",
        "words": progress["words"],
        "seconds": seconds,
    }


@router.post("/cancel")
async def cancel_ai_task():
    """Stop the running AI task: embedding after its current batch (cached chunks are kept),
    generation at the model's next word."""
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

    # Ensure all documents are embedded
    _embedding_progress["active"] = True
    _embedding_progress["total"] = len(docs)
    try:
        await _embed_documents(
            docs, req.project_id, embed_model, ollama_url,
            chunk_size, chunk_overlap, chunk_mode, task,
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
    results = await asyncio.to_thread(
        _search_embeddings,
        query_embedding, req.project_id, embed_model,
        req.top_k, search_doc_ids,
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
            "SELECT name, description FROM code "
            "WHERE id=? AND project_id=? AND deleted_at IS NULL",
            (req.code_id, req.project_id),
        )
        code = await cursor.fetchone()
        if not code:
            raise HTTPException(404, "Code not found")

        cursor = await db.execute(
            "SELECT c.selected_text FROM coding c JOIN document d ON d.id=c.document_id "
            f"WHERE c.code_id=? AND c.deleted_at IS NULL AND {_INCLUDED_DOCUMENT_SQL} "
            "ORDER BY c.id LIMIT 5",
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


# Writing is the slow part of a summary; without a limit models write 600-1,400 words.
SUMMARY_MAX_WORDS = 300


class SummarizeCodeRequest(BaseModel):
    project_id: int
    code_id: int
    llm_model: str | None = None


@router.post("/summarize-code")
async def summarize_code(req: SummarizeCodeRequest):
    """Generate an LLM summary of all passages coded under a specific code."""
    task = _begin_cancellable_task()  # registered before the first await
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
            f"AND {_INCLUDED_DOCUMENT_SQL} ORDER BY d.name, d.id, c.start_pos",
            (req.code_id, req.project_id),
        )
        segments = await cursor.fetchall()
    finally:
        await db.close()

    if not segments:
        return {
            "summary": "No coded segments found for this code.",
            "segment_count": 0,
            "total_segments": 0,
        }

    code_name = code["name"]
    code_description = code["description"] or "No description provided"
    system = (
        "You are a qualitative research assistant. Below are text passages that have been "
        f"coded under the code '{code_name}'. The code is defined as: '{code_description}'. "
        "Synthesize these passages into a thematic summary. Identify key patterns, variations, "
        "and notable aspects across all the passages. Be analytical, not just descriptive. "
        f"Write at most {SUMMARY_MAX_WORDS} words."
    )
    think = settings.get("think_mode", "off") == "on"
    passages = [seg["selected_text"] for seg in segments]
    header = f"Here are {len(segments)} of {len(segments)} coded passages:\n\n"
    wrapper = f'Passage {len(segments)}: ""\n\n---\n\n'
    chosen = _fit_passages(passages, _prompt_budget(think, system, header), wrapper)
    segment_texts = "\n\n---\n\n".join(
        f'Passage {number}: "{passages[index]}"' for number, index in enumerate(chosen, 1)
    )
    prompt = f"Here are {len(chosen)} of {len(segments)} coded passages:\n\n{segment_texts}"

    response = await _generate_or_fail(prompt, llm_model, ollama_url, system, think, task)
    return {
        "summary": response,
        "segment_count": len(chosen),
        "total_segments": len(segments),
        "timing": dict(_last_generation),
    }


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
                "WHERE c.code_id=? AND d.project_id=? AND c.deleted_at IS NULL "
                f"AND {_INCLUDED_DOCUMENT_SQL}",
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

def _normalise_hierarchy(parsed) -> dict | None:
    """Bring a model's hierarchy answer into the shape the interface shows, or None.

    Local models often return valid JSON that differs slightly from the requested format
    (a list named "codes" instead of "children", a missing key, a number among the names).
    Groups without a name or without codes are dropped; an answer with nothing usable
    counts as unparseable.
    """
    if not isinstance(parsed, dict):
        return None

    def names(value) -> list[str]:
        items = value if isinstance(value, list) else []
        texts = [str(item).strip() for item in items if isinstance(item, (str, int))]
        return [text for text in texts if text]

    groups = []
    raw_groups = parsed.get("groups")
    for group in raw_groups if isinstance(raw_groups, list) else []:
        if not isinstance(group, dict):
            continue
        parent = group.get("suggested_parent") or group.get("parent") or group.get("name")
        children = names(group.get("children")) or names(group.get("codes"))
        if not isinstance(parent, str) or not parent.strip() or not children:
            continue
        description = group.get("description")
        groups.append({
            "suggested_parent": parent.strip(),
            "description": description if isinstance(description, str) else "",
            "children": children,
        })
    standalone = names(parsed.get("standalone"))
    if not groups and not standalone:
        return None
    return {"groups": groups, "standalone": standalone}


class HierarchySuggestRequest(BaseModel):
    project_id: int
    llm_model: str | None = None


@router.post("/suggest-hierarchy")
async def suggest_hierarchy(req: HierarchySuggestRequest):
    """Ask an LLM to suggest a hierarchical grouping for the project's codebook."""
    task = _begin_cancellable_task()  # registered before the first await
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
            "(SELECT COUNT(*) FROM coding cg JOIN document d ON d.id=cg.document_id "
            " WHERE cg.code_id = c.id AND cg.deleted_at IS NULL "
            f" AND {_INCLUDED_DOCUMENT_SQL}) AS coding_count "
            "FROM code c WHERE c.project_id=? AND c.deleted_at IS NULL "
            "ORDER BY c.name",
            (req.project_id,),
        )
        codes = await cursor.fetchall()

        if not codes:
            return {
                "groups": [], "standalone": [], "code_count": 0, "codes_included": 0,
                "examples_included": True, "answer_truncated": False,
            }

        code_examples: dict[int, list[str]] = {}
        for code in codes:
            cursor = await db.execute(
                "SELECT cg.selected_text FROM coding cg JOIN document d ON d.id=cg.document_id "
                f"WHERE cg.code_id=? AND cg.deleted_at IS NULL AND {_INCLUDED_DOCUMENT_SQL} "
                "ORDER BY cg.id LIMIT 3",
                (code["id"],),
            )
            examples = await cursor.fetchall()
            code_examples[code["id"]] = [ex["selected_text"][:150] for ex in examples]
    finally:
        await db.close()

    # One line per code; example passages are dropped first when the codebook is too long.
    def code_line(code, with_examples: bool) -> str:
        line = f'"{code["name"]}" ({code["coding_count"]} segments)'
        if code["description"]:
            line += f" - Description: {code['description']}"
        examples = code_examples.get(code["id"], []) if with_examples else []
        if examples:
            example_strs = ", ".join(f'"{ex}"' for ex in examples)
            line += f" Examples: {example_strs}"
        return line

    system = "You are a qualitative research methods expert."
    instructions = (
        "Below is a codebook with codes used in a qualitative analysis project. "
        "Suggest a hierarchical organization by grouping related codes under "
        "parent categories.\n\n"
        "Rules:\n"
        "- Suggest new parent categories where helpful\n"
        "- A code can only belong to one parent\n"
        "- Not every code needs a parent — leave standalone codes as-is\n"
        "- Return ONLY valid JSON\n\n"
    )
    response_format = (
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
    think = settings.get("think_mode", "off") == "on"
    budget = _prompt_budget(think, system, instructions, "Codes:\n\n\n", response_format)
    wrapper = f"{len(codes)}. \n"
    lines = [code_line(code, with_examples=True) for code in codes]
    examples_included = sum(_estimate_tokens(line + wrapper) for line in lines) <= budget
    if not examples_included:
        lines = [code_line(code, with_examples=False) for code in codes]
    chosen = _fit_passages(lines, budget, wrapper)
    codes_text = "\n".join(
        f"{number}. {lines[index]}" for number, index in enumerate(chosen, 1)
    )
    prompt = f"{instructions}Codes:\n{codes_text}\n\n{response_format}"
    coverage = {
        "code_count": len(codes),
        "codes_included": len(chosen),
        "examples_included": examples_included,
    }

    response = await _generate_or_fail(prompt, llm_model, ollama_url, system, think, task)
    # A suggestion cut off at the length limit may lack groups even when its JSON parses.
    coverage["answer_truncated"] = response.endswith(LENGTH_LIMIT_NOTE)
    coverage["timing"] = dict(_last_generation)

    # Parse the whole response as JSON, else the outermost {...} block within it.
    json_match = re.search(r'\{[\s\S]*\}', response)
    for candidate in (response, json_match.group() if json_match else None):
        if candidate is None:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        suggestion = _normalise_hierarchy(parsed)
        if suggestion is not None:
            return {**suggestion, **coverage}

    return {
        "error": "Could not parse JSON from LLM response",
        "raw_response": response,
        **coverage,
    }


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
    task = _begin_cancellable_task()  # registered before the first await
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
            f"AND {_INCLUDED_DOCUMENT_SQL} ORDER BY d.name, d.id, c.start_pos",
            (req.code_id, req.project_id),
        )
        segments = await cursor.fetchall()
    finally:
        await db.close()

    if not segments:
        return {
            "definition": "No coded segments found for this code.",
            "segment_count": 0,
            "total_segments": 0,
        }

    code_name = code["name"]
    current_desc = code["description"]

    desc_line = ""
    if current_desc:
        desc_line = f"Current description: {current_desc}\n\n"

    system = "You are a qualitative research methods expert."
    intro = (
        f'A researcher has applied the code "{code_name}" to the following text passages. '
        "Based on these passages, write a concise, precise definition for this code that "
        "captures what it means in this research context.\n\n"
        f"{desc_line}"
    )
    guidance = (
        "Write a definition that:\n"
        "- Captures the core meaning across all passages\n"
        "- Is specific enough to guide future coding decisions\n"
        "- Notes any important variations or sub-themes\n"
        "- Is 2-4 sentences long"
    )
    think = settings.get("think_mode", "off") == "on"
    passages = [seg["selected_text"] for seg in segments]
    wrapper = f'{len(segments)}. ""\n'
    chosen = _fit_passages(
        passages,
        _prompt_budget(think, system, intro, "Coded passages:\n\n\n", guidance),
        wrapper,
    )
    passage_lines = "\n".join(
        f'{number}. "{passages[index]}"' for number, index in enumerate(chosen, 1)
    )
    prompt = f"{intro}Coded passages:\n{passage_lines}\n\n{guidance}"

    response = await _generate_or_fail(prompt, llm_model, ollama_url, system, think, task)
    return {
        "definition": response,
        "segment_count": len(chosen),
        "total_segments": len(segments),
        "timing": dict(_last_generation),
    }
