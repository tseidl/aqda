import pytest

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
