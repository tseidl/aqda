import io

import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

import aqda.db as db_module
from aqda.routers import ai, codes, codings, documents, projects

from aqda.routers.ai import MIN_OUTLIER_SEGMENTS, _finalize_suggestions, _outlier_cutoff

TEXT = "First sentence here. Second sentence follows. Third one closes. Fourth is last."


def test_finalize_suggestions_snaps_then_filters_duplicates_and_coded_spans():
    results = [
        # Both chunks cut through the second sentence and snap to the same span.
        {"document_id": 1, "start_pos": 27, "end_pos": 40, "text": "sentence foll", "similarity": 0.9},
        {"document_id": 1, "start_pos": 21, "end_pos": 44, "text": "Second sentence follows", "similarity": 0.8},
        # Snaps to the third sentence, which already carries this code.
        {"document_id": 1, "start_pos": 47, "end_pos": 60, "text": "hird one clos", "similarity": 0.7},
        {"document_id": 1, "start_pos": 66, "end_pos": 70, "text": "urth", "similarity": 0.6},
        # No document text available: returned unchanged.
        {"document_id": 2, "start_pos": 0, "end_pos": 5, "text": "hello", "similarity": 0.5},
    ]
    existing = {1: [(50, 55)]}

    texts = {1: TEXT}
    final = _finalize_suggestions(results, texts.get, existing, top_k=10)
    assert [(r["start_pos"], r["end_pos"], r["text"]) for r in final] == [
        (21, 45, "Second sentence follows."),
        (64, 79, "Fourth is last."),
        (0, 5, "hello"),
    ]
    assert [r["similarity"] for r in final] == [0.9, 0.6, 0.5]
    assert len(_finalize_suggestions(results, texts.get, existing, top_k=1)) == 1


def test_outlier_cutoff_adapts_to_each_code():
    assert _outlier_cutoff([0.8] * (MIN_OUTLIER_SEGMENTS - 1)) is None

    consistent = [0.79, 0.80, 0.75, 0.81, 0.75, 0.78]
    cutoff = _outlier_cutoff(consistent)
    assert cutoff is not None
    assert not [s for s in consistent if s < cutoff]

    with_outlier = consistent + [0.56]
    cutoff = _outlier_cutoff(with_outlier)
    assert [s for s in with_outlier if s < cutoff] == [0.56]


def test_paragraph_chunking_keeps_lines_intact():
    from aqda.routers.ai import _chunk_text

    text = "Interviewer: Hi.\n\n  Respondent: " + "word " * 150 + "\nShort.\n"
    chunks = _chunk_text(text, chunk_size=500, overlap=50, mode="paragraph")
    assert chunks[0] == {"text": "Interviewer: Hi.", "start": 0, "end": 16}
    assert chunks[-1]["text"] == "Short."
    assert len(chunks) > 3  # the long turn still falls back to fixed windows
    for chunk in chunks:
        assert "\n" not in chunk["text"]
        assert text[chunk["start"]:chunk["end"]].strip() == chunk["text"]


# Topic Search shows chunks as they are: none may start or end inside a word.
def test_chunks_start_and_end_at_word_or_sentence_boundaries():
    from aqda.routers.ai import _chunk_text

    sentences = [
        "The plant has a future if the investment actually comes.",
        "I am not sure it will, though!",
        "Do you think the owners care?",
        "Nobody in the works council believes the announcements any more, because we have "
        "heard them three times before and each time the money went somewhere else instead.",
        "Über die Zukunft 🙂 spricht hier niemand mehr.",
    ]
    text = " ".join(sentences * 8)
    words = set(text.split())
    for chunk_size, overlap in [(500, 50), (200, 50), (200, 150), (120, 100), (100, 0)]:
        chunks = _chunk_text(text, chunk_size, overlap)
        assert len(chunks) > 3
        for chunk in chunks:
            # Offsets are code points (Python string indices) and enclose the text exactly.
            assert text[chunk["start"]:chunk["end"]] == chunk["text"]
            assert len(chunk["text"]) <= chunk_size
            pieces = chunk["text"].split()
            assert pieces[0] in words and pieces[-1] in words, chunk["text"]
        # Nothing is left out between two chunks, and every chunk moves forward.
        for before, after in zip(chunks, chunks[1:]):
            assert before["start"] < after["start"] <= before["end"] + 1
        assert chunks[0]["start"] == 0 and chunks[-1]["end"] == len(text)

    # With sentences shorter than the chunk, every chunk starts with a sentence.
    starts = tuple(sentences)
    assert all(chunk["text"].startswith(starts) for chunk in _chunk_text(text, 500, 50))
    # An overlap that holds a whole sentence repeats that sentence, not a fragment of one.
    overlapping = _chunk_text(text, 500, 200)
    assert any(after["start"] < before["end"] for before, after in zip(overlapping, overlapping[1:]))
    assert all(chunk["text"].startswith(starts) for chunk in overlapping)


# The chunker once looped forever when the overlap reached back to the chunk's own start.
@pytest.mark.parametrize("chunk_size, overlap", [(10, 9), (5, 50), (3, 2), (1, 0), (0, 0), (50, 49)])
def test_chunker_always_moves_forward(chunk_size, overlap):
    from aqda.routers.ai import _chunk_text

    for text in ["word " * 40, "Short. " * 30, "a" * 120, "字" * 120, "x\n\n\n" * 20, " " * 50, ""]:
        chunks = _chunk_text(text, chunk_size, overlap)
        assert len(chunks) <= len(text)
        starts = [chunk["start"] for chunk in chunks]
        assert starts == sorted(set(starts))
        for chunk in chunks:
            assert text[chunk["start"]:chunk["end"]] == chunk["text"]


# Models write Markdown whatever the prompt says; the researcher reads plain text.
def test_markdown_is_removed_from_model_answers():
    from aqda.routers.ai import LENGTH_LIMIT_NOTE, _plain_text

    answer = (
        "### **Key patterns**\n\n"
        "Workers show **ritualistic engagement** with *formal* consultation and "
        "__distrust__ of _management_.\n\n"
        "* **Hollow promises:** announcements are `empty`.\n"
        "- The variable works_council_role and 2 * 3 * 4 stay as they are.\n\n"
        "---\n\n"
        "1. A numbered line stays.\n\n\n\n"
        "The answer was cut **short"
    )
    assert _plain_text(answer) == (
        "Key patterns\n\n"
        "Workers show ritualistic engagement with formal consultation and "
        "distrust of management.\n\n"
        "- Hollow promises: announcements are empty.\n"
        "- The variable works_council_role and 2 * 3 * 4 stay as they are.\n\n"
        "1. A numbered line stays.\n\n"
        "The answer was cut short"
    )
    assert _plain_text("Plain answer." + LENGTH_LIMIT_NOTE) == "Plain answer." + LENGTH_LIMIT_NOTE


@pytest.mark.asyncio
async def test_cancel_flag_stops_the_next_embedding_batch():
    from fastapi import HTTPException

    from aqda.routers.ai import _begin_cancellable_task, _check_cancelled, cancel_ai_task

    first = _begin_cancellable_task()
    _check_cancelled(first)  # a fresh task is not cancelled
    await cancel_ai_task()
    with pytest.raises(HTTPException) as stopped:
        _check_cancelled(first)
    assert stopped.value.status_code == 409
    second = _begin_cancellable_task()
    _check_cancelled(second)  # a task started after the cancel is unaffected
    with pytest.raises(HTTPException):
        _check_cancelled(first)  # ... and the cancelled one stays cancelled


# A failed batch must not turn a partial search or consistency check into success.
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "consistency"])
@pytest.mark.parametrize("failed_batch", [1, 2, None])
async def test_embedding_failure_is_reported(
    tmp_path, use_data_dir, monkeypatch, operation, failed_batch,
):
    use_data_dir(tmp_path / "embedding-failure")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Embeddings"))
    code = await codes.create_code(codes.CodeCreate(project_id=project["id"], name="Theme"))
    document = await documents.upload_document(
        project_id=project["id"],
        file=UploadFile(filename="interview.txt", file=io.BytesIO(b"x" * 6000)),
    )
    for start in range(11):
        await codings.create_coding(codings.CodingCreate(
            document_id=document["id"], code_id=code["id"],
            start_pos=start, end_pos=start + 1, selected_text="x",
        ))

    calls = 0

    # Simulate a model failure after zero or one successful embedding batches.
    async def embed(text, model, ollama_url):
        nonlocal calls
        if isinstance(text, str):
            return [1.0, 0.0]
        calls += 1
        if calls == failed_batch:
            raise httpx.ReadTimeout("Model timed out")
        return [[1.0, 0.0] for _ in text]

    monkeypatch.setattr(ai, "_ollama_embed", embed)

    # Exercise both public handlers with the same corpus and simulated model.
    async def run():
        if operation == "search":
            return await ai.find_similar(ai.SimilarSearchRequest(
                project_id=project["id"], query="theme", embedding_model="test-model",
            ))
        return await ai.consistency_check(ai.ConsistencyCheckRequest(
            project_id=project["id"], embedding_model="test-model",
        ))

    if failed_batch is None:
        result = await run()
        if operation == "search":
            assert len(result) == 10
            assert {row["document_id"] for row in result} == {document["id"]}
        else:
            assert result["results"][0]["segment_count"] == 11
            assert result["results"][0]["outliers"] == []
        assert calls == 2
        return

    with pytest.raises(HTTPException) as failure:
        await run()
    assert failure.value.status_code == 502
    assert "embedding failed" in failure.value.detail
    assert calls == failed_batch
    assert not ai._embedding_progress["active"]


# A successful HTTP response must still contain one embedding per requested input.
@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [0, 1])
async def test_incomplete_embedding_response_is_rejected(monkeypatch, returned):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"embeddings": [[1.0, 0.0]] * returned})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        monkeypatch.setattr(ai, "_get_http_client", lambda: client)
        with pytest.raises(ValueError, match="incomplete embedding batch"):
            await ai._ollama_embed(["first", "second"], "test-model", "http://ollama.test")


def test_fit_passages_spreads_a_deterministic_subset():
    from aqda.routers.ai import _estimate_tokens, _fit_passages

    passages = ["passage " + "word " * 30 for _ in range(40)]
    assert _fit_passages(passages[:3], 10_000) == [0, 1, 2]

    each = _estimate_tokens(passages[0])
    chosen = _fit_passages(passages, budget_tokens=each * 10)
    assert len(chosen) == 10
    assert chosen[0] < 5 and chosen[-1] > 34  # spread over the whole list, not its start
    assert _fit_passages(passages, budget_tokens=each * 10) == chosen

    # A long passage in the middle is skipped instead of blocking the short ones around it.
    short, long = "tiny", "long " * 100
    cost = _estimate_tokens(short)
    assert _fit_passages([short, short, long, short, short], budget_tokens=2 * cost) == [1, 4]

    with pytest.raises(HTTPException) as too_long:
        _fit_passages(["x" * 3000], budget_tokens=10)
    assert too_long.value.status_code == 422


def test_token_estimate_is_generous_for_digits_and_other_scripts():
    from aqda.routers.ai import _estimate_tokens

    assert _estimate_tokens("interview") == 9 // 3 + 1
    assert _estimate_tokens("2024-09-22") == 10 + 1
    assert _estimate_tokens("访谈记录") == 8 + 1


# Ollama cuts prompts beyond its window silently, so the window must be set explicitly.
@pytest.mark.asyncio
async def test_generation_sets_the_context_window(monkeypatch):
    import json

    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"response": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(ai, "_get_http_client", lambda: client)
        assert await ai._ollama_generate("prompt", "model", "http://ollama.test") == "ok"
    assert seen["options"] == {
        "num_ctx": ai.LLM_CONTEXT_TOKENS, "num_predict": ai.LLM_RESPONSE_TOKENS,
    }
    # ... and the model is kept in memory beyond Ollama's five minutes.
    assert seen["keep_alive"] == ai.OLLAMA_KEEP_ALIVE


# An answer cut off at the length limit says so instead of looking complete.
@pytest.mark.asyncio
async def test_answer_at_the_length_limit_is_marked(monkeypatch):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": "Partial", "done_reason": "length"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        monkeypatch.setattr(ai, "_get_http_client", lambda: client)
        answer = await ai._ollama_generate("prompt", "model", "http://ollama.test", think=True)
    assert answer == "Partial" + ai.LENGTH_LIMIT_NOTE


async def _project_with_excluded_guide(tmp_path, use_data_dir):
    """A code applied in an interview and in an excluded document of example quotes."""
    use_data_dir(tmp_path / "exclusion")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Exclusion"))
    code = await codes.create_code(codes.CodeCreate(project_id=project["id"], name="Trust"))
    interview = await documents.upload_document(
        project_id=project["id"],
        file=UploadFile(filename="interview.txt", file=io.BytesIO(
            b"Interview passage one. Interview passage two. Uncoded remainder here."
        )),
    )
    guide = await documents.upload_document(
        project_id=project["id"],
        file=UploadFile(filename="examples.txt", file=io.BytesIO(b"Guide example quote.")),
    )
    for start, text in [(0, "Interview passage one."), (23, "Interview passage two.")]:
        await codings.create_coding(codings.CodingCreate(
            document_id=interview["id"], code_id=code["id"],
            start_pos=start, end_pos=start + len(text), selected_text=text,
        ))
    await codings.create_coding(codings.CodingCreate(
        document_id=guide["id"], code_id=code["id"],
        start_pos=0, end_pos=20, selected_text="Guide example quote.",
    ))
    await documents.set_ai_exclusions(documents.AiExclusionUpdate(
        project_id=project["id"], excluded_ids=[guide["id"]],
        listed_ids=[interview["id"], guide["id"]],
    ))
    return project, code


# Excluded documents are ignored by every AI feature, including as coded examples.
@pytest.mark.asyncio
async def test_excluded_documents_are_ignored_by_every_ai_feature(
    tmp_path, use_data_dir, monkeypatch,
):
    project, code = await _project_with_excluded_guide(tmp_path, use_data_dir)
    prompts = []

    async def generate(prompt, model, ollama_url, system="", think=False, task=None):
        prompts.append(system + prompt)
        if "Return ONLY valid JSON" in prompt:
            return '{"groups": [], "standalone": ["Trust"]}'
        return "**Trust** is *earned*, as Passage 1 shows."

    embedded = []

    async def embed(text, model, ollama_url):
        embedded.extend([text] if isinstance(text, str) else text)
        return [1.0, 0.0] if isinstance(text, str) else [[1.0, 0.0] for _ in text]

    monkeypatch.setattr(ai, "_ollama_generate", generate)
    monkeypatch.setattr(ai, "_ollama_embed", embed)
    ids = {"project_id": project["id"], "code_id": code["id"]}

    summary = await ai.summarize_code(ai.SummarizeCodeRequest(**ids, llm_model="m"))
    definition = await ai.generate_definition(ai.GenerateDefinitionRequest(**ids, llm_model="m"))
    hierarchy = await ai.suggest_hierarchy(
        ai.HierarchySuggestRequest(project_id=project["id"], llm_model="m")
    )
    consistency = await ai.consistency_check(
        ai.ConsistencyCheckRequest(**ids, embedding_model="m")
    )
    suggestions = await ai.suggest_codings(ai.AutoCodeRequest(**ids, embedding_model="m"))

    assert len(prompts) == 3
    for prompt in prompts:
        assert "Interview passage one." in prompt
        assert "Guide example" not in prompt
    assert '"Trust" (2 segments)' in prompts[2]
    # A summary without a length limit runs to a thousand words and takes minutes.
    assert f"Write at most {ai.SUMMARY_MAX_WORDS} words." in prompts[0]
    # Passages are labelled with their document, not with a number the researcher never
    # sees, and Markdown the model writes anyway is removed from what the researcher reads.
    assert 'From the document "interview.txt": "Interview passage one."' in prompts[0]
    for prompt in prompts:
        assert "Passage 1" not in prompt and '1. "' not in prompt
    assert ai.PLAIN_TEXT_INSTRUCTION in prompts[0] and ai.PLAIN_TEXT_INSTRUCTION in prompts[1]
    assert summary["summary"] == "Trust is earned, as Passage 1 shows."
    assert definition["definition"] == "Trust is earned, as Passage 1 shows."
    assert all("Guide example" not in text for text in embedded)
    assert any("Interview passage one." in text for text in embedded)
    assert (summary["segment_count"], summary["total_segments"]) == (2, 2)
    assert (definition["segment_count"], definition["total_segments"]) == (2, 2)
    assert (hierarchy["codes_included"], hierarchy["code_count"]) == (1, 1)
    assert hierarchy["examples_included"] is True
    assert hierarchy["answer_truncated"] is False
    assert hierarchy["standalone"] == ["Trust"]
    assert consistency["results"][0]["segment_count"] == 2
    assert all("Guide" not in s["text"] for s in suggestions)


# A summary over more passages than fit reports how many the model actually read.
@pytest.mark.asyncio
async def test_summary_reports_the_passages_it_used(tmp_path, use_data_dir, monkeypatch):
    use_data_dir(tmp_path / "coverage")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Coverage"))
    code = await codes.create_code(codes.CodeCreate(project_id=project["id"], name="Theme"))
    sentence = "This participant talks at length about the theme in question. " * 8
    document = await documents.upload_document(
        project_id=project["id"],
        file=UploadFile(filename="long.txt", file=io.BytesIO((sentence * 60).encode())),
    )
    for index in range(60):
        start = index * len(sentence)
        await codings.create_coding(codings.CodingCreate(
            document_id=document["id"], code_id=code["id"],
            start_pos=start, end_pos=start + len(sentence), selected_text=sentence,
        ))
    prompts = []

    async def generate(prompt, model, ollama_url, system="", think=False, task=None):
        prompts.append(system + prompt)
        return "summary"

    monkeypatch.setattr(ai, "_ollama_generate", generate)
    result = await ai.summarize_code(ai.SummarizeCodeRequest(
        project_id=project["id"], code_id=code["id"], llm_model="m",
    ))
    used = result["segment_count"]
    assert result["total_segments"] == 60
    assert 0 < used < 60
    assert f"Here are {used} of 60 coded passages" in prompts[0]
    assert ai._estimate_tokens(prompts[0]) <= ai.LLM_CONTEXT_TOKENS - ai.LLM_RESPONSE_TOKENS


def _stream_client(lines, status=200):
    """An HTTP client whose Ollama answers with the given newline-delimited JSON objects."""
    import json

    body = "".join(json.dumps(line) + "\n" for line in lines).encode()
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, content=body)
    ))


# The answer is read as a stream, so long answers are not cut off by a total time limit.
@pytest.mark.asyncio
async def test_generation_joins_a_streamed_answer(monkeypatch):
    lines = [
        {"thinking": "Let me see", "done": False},
        {"response": "Two wor", "done": False},
        {"response": "ds here.", "done": False},
        {"response": "", "done": True, "done_reason": "stop", "load_duration": 2_000_000_000,
         "prompt_eval_count": 40, "prompt_eval_duration": 500_000_000, "eval_count": 9,
         "eval_duration": 1_500_000_000, "total_duration": 4_000_000_000},
    ]
    async with _stream_client(lines) as client:
        monkeypatch.setattr(ai, "_get_http_client", lambda: client)
        answer = await ai._ollama_generate("prompt", "model", "http://ollama.test", think=True)
    assert answer == "Two words here."
    progress = await ai.generation_progress()
    assert progress["active"] is False and progress["words"] == 3
    assert ai._count_words("ds here.", *ai._count_words("Two wor", 0, False)) == (3, True)
    # Where the time went, for the line under the answer.
    assert ai._last_generation == {
        "load_seconds": 2.0, "prompt_tokens": 40, "prompt_seconds": 0.5,
        "answer_tokens": 9, "answer_seconds": 1.5, "total_seconds": 4.0, "reasoned": True,
    }


# Failures reach the researcher with a reason: Ollama's own message, or a named timeout.
@pytest.mark.asyncio
async def test_generation_failures_carry_a_reason(monkeypatch):
    async with _stream_client([{"error": "model 'missing' not found"}], status=404) as client:
        monkeypatch.setattr(ai, "_get_http_client", lambda: client)
        with pytest.raises(HTTPException) as unknown_model:
            await ai._generate_or_fail("prompt", "missing", "http://ollama.test", "", False, 10**9)
    assert "model 'missing' not found" in unknown_model.value.detail

    def silent(request):
        raise httpx.ReadTimeout("")

    async with httpx.AsyncClient(transport=httpx.MockTransport(silent)) as client:
        monkeypatch.setattr(ai, "_get_http_client", lambda: client)
        with pytest.raises(HTTPException) as timed_out:
            await ai._generate_or_fail("prompt", "model", "http://ollama.test", "", False, 10**9)
    assert timed_out.value.status_code == 504
    assert "did not respond" in timed_out.value.detail
    assert (await ai.generation_progress())["active"] is False


# Cancel stops a generation at the next word instead of letting the model finish unseen.
@pytest.mark.asyncio
async def test_cancel_stops_a_running_generation(monkeypatch):
    task = ai._begin_cancellable_task()

    async def answer():
        yield b'{"response": "First", "done": false}\n'
        await ai.cancel_ai_task()
        yield b'{"response": " second", "done": false}\n'
        yield b'{"response": "", "done": true}\n'

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=answer()))
    async with httpx.AsyncClient(transport=transport) as client:
        monkeypatch.setattr(ai, "_get_http_client", lambda: client)
        with pytest.raises(HTTPException) as cancelled:
            await ai._generate_or_fail("prompt", "model", "http://ollama.test", "", False, task)
    assert cancelled.value.status_code == 409


# Valid JSON in a slightly different shape must not reach the interface as it is.
def test_hierarchy_answer_is_brought_into_shape():
    from aqda.routers.ai import _normalise_hierarchy

    answer = {
        "groups": [
            {"suggested_parent": "**Institutions**", "description": "d", "codes": ["Trust", 7, ""]},
            {"suggested_parent": "No codes"},
            {"children": ["Orphan"]},
            "not a group",
        ],
        "standalone": "Trust",
    }
    assert _normalise_hierarchy(answer) == {
        "groups": [
            {"suggested_parent": "Institutions", "description": "d", "children": ["Trust", "7"]}
        ],
        "standalone": [],
    }
    assert _normalise_hierarchy({"groups": {"a": 1}}) is None
    assert _normalise_hierarchy(["Trust"]) is None


# Many short documents (spreadsheet rows) share embedding requests instead of one each.
@pytest.mark.asyncio
async def test_short_documents_are_embedded_in_shared_batches(tmp_path, use_data_dir, monkeypatch):
    use_data_dir(tmp_path / "rows")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Rows"))
    for number in range(25):
        await documents.upload_document(
            project_id=project["id"],
            file=UploadFile(filename=f"row-{number:02d}.txt", file=io.BytesIO(
                f"Answer number {number}.".encode()
            )),
        )
    batches = []

    async def embed(text, model, ollama_url):
        if isinstance(text, str):
            return [1.0, 0.0]
        batches.append(len(text))
        return [[1.0, 0.0] for _ in text]

    monkeypatch.setattr(ai, "_ollama_embed", embed)
    request = ai.SimilarSearchRequest(
        project_id=project["id"], query="answer", embedding_model="m", top_k=50,
    )
    assert len(await ai.find_similar(request)) == 25
    assert batches == [10, 10, 5]
    assert len(await ai.find_similar(request)) == 25
    assert batches == [10, 10, 5]  # the second search finds every chunk in the cache


# A model split between graphics memory and processor is reported, so the panel can warn.
@pytest.mark.asyncio
async def test_loaded_models_report_their_processor_share(tmp_path, use_data_dir, monkeypatch):
    from aqda.routers import settings as settings_router

    use_data_dir(tmp_path / "loaded")
    await db_module.init_db()
    ps = {"models": [
        {"name": "fits:9b", "size": 6_000, "size_vram": 6_000, "context_length": 8192},
        {"name": "split:27b", "size": 20_000, "size_vram": 12_000, "context_length": 8192},
    ]}
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=ps))
    monkeypatch.setattr(
        settings_router.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport)
    )
    loaded = (await settings_router.loaded_ollama_models())["models"]
    assert [(m["name"], m["cpu_share"]) for m in loaded] == [("fits:9b", 0.0), ("split:27b", 0.4)]


# Reasoning counts towards the answer length; when it uses all of it, say so instead of
# returning an empty answer with a note.
@pytest.mark.asyncio
async def test_reasoning_that_uses_the_whole_answer_length_is_reported(monkeypatch):
    lines = [
        {"thinking": "Still thinking", "done": False},
        {"response": "", "done": True, "done_reason": "length"},
    ]
    async with _stream_client(lines) as client:
        monkeypatch.setattr(ai, "_get_http_client", lambda: client)
        with pytest.raises(HTTPException) as no_answer:
            await ai._generate_or_fail("prompt", "model", "http://ollama.test", "", True, 10**9)
    assert no_answer.value.detail == ai.REASONING_ONLY_MESSAGE


# Before its first word the model sends nothing; a cancel must not wait for that word.
@pytest.mark.asyncio
async def test_cancel_does_not_wait_for_the_first_word(monkeypatch):
    import asyncio

    closed = []

    async def silent_model(prompt, model, ollama_url, system="", think=False, task=None):
        try:
            await asyncio.sleep(60)
        finally:
            closed.append(True)

    monkeypatch.setattr(ai, "_ollama_generate", silent_model)
    task = ai._begin_cancellable_task()
    running = asyncio.ensure_future(
        ai._generate_or_fail("prompt", "model", "http://ollama.test", "", False, task)
    )
    await asyncio.sleep(0.1)
    await ai.cancel_ai_task()
    with pytest.raises(HTTPException) as cancelled:
        await asyncio.wait_for(running, timeout=3)
    assert cancelled.value.status_code == 409
    assert closed == [True]
