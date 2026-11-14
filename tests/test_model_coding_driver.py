"""Tests of the example driver's pure functions (no network)."""

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "aqda" / "examples" / "model_coding.py"
_SPEC = importlib.util.spec_from_file_location("model_coding_example", _PATH)
driver = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(driver)

CODEBOOK = [
    {"path": ["Economy"], "name": "Economy", "description": "About the\n economy"},
    {"path": ["Economy", "Jobs"], "name": "Jobs", "description": ""},
]


def test_codebook_from_codes_builds_paths_in_tree_order():
    codes = [
        {"id": 3, "parent_id": 1, "name": "Jobs", "description": ""},
        {"id": 1, "parent_id": None, "name": "Economy", "description": "About"},
        {"id": 2, "parent_id": None, "name": "Policy", "description": None},
    ]
    assert driver.codebook_from_codes(codes) == [
        {"path": ["Economy"], "name": "Economy", "description": "About"},
        {"path": ["Economy", "Jobs"], "name": "Jobs", "description": ""},
        {"path": ["Policy"], "name": "Policy", "description": ""},
    ]


def test_prompt_carries_instructions_codebook_fields_and_text():
    fields = [{"name": "Actor", "kind": "text"}, {"name": "Agrees", "kind": "yes_no"}]
    messages = driver.build_messages(
        "Code stances on industrial policy.", CODEBOOK, "Interview 1", "Some text.",
        allow_new_codes=True, fields=fields,
    )
    assert [message["role"] for message in messages] == ["system", "user"]
    prompt = messages[1]["content"]
    assert prompt.startswith("Code stances on industrial policy.")
    assert '- ["Economy"] — About the economy' in prompt
    assert '- ["Economy", "Jobs"]' in prompt
    assert '"Agrees": answer "yes" or "no"' in prompt
    assert '"new_code"' in prompt
    assert "Document “Interview 1”:\n<<<\nSome text.\n>>>" in prompt

    plain = driver.build_messages("Go.", [], "D", "T")[1]["content"]
    assert "new_code" not in plain and "fields" not in plain
    assert "(There are no codes yet.)" in plain


@pytest.mark.parametrize(
    "content",
    [
        '{"passages": [{"quote": "Q", "code_path": ["Economy", "Jobs"]}]}',
        '```json\n{"passages": [{"quote": "Q", "code_path": ["Economy", "Jobs"]}]}\n```',
        'Here you go: {"passages": [{"quote": "Q", "code_path": "Economy > Jobs"}]} Done.',
        '[{"quote": "Q", "code": "Economy > Jobs"}]',
    ],
)
def test_answers_in_several_shapes_are_read(content):
    assert driver.parse_answer(content) == [{"quote": "Q", "code_path": ["Economy", "Jobs"]}]


@pytest.mark.parametrize(
    "content", ["", "I cannot help with that.", '{"passages": "none"}', "{broken", None]
)
def test_malformed_answers_are_reported_as_unreadable(content):
    assert driver.parse_answer(content) is None


def test_empty_answer_and_odd_passages_pass_through_for_aqda_to_refuse():
    assert driver.parse_answer('{"passages": []}') == []
    parsed = driver.parse_answer(json.dumps({"passages": [
        "just a string",
        {"quote": "Q", "new_code": {"name": "N"}, "code": "ignored", "extra": 1},
        {"quote": "Q", "code_id": 4, "fields": {"Actor": "Eve"}, "before": "B"},
    ]}))
    assert parsed == [
        "just a string",
        {"quote": "Q", "new_code": {"name": "N"}},
        {"quote": "Q", "code_id": 4, "fields": {"Actor": "Eve"}, "before": "B"},
    ]


def test_arguments_take_the_name_of_the_key_variable():
    args = driver.parse_arguments([
        "--project", "3", "--base-url", "http://localhost:11434/v1", "--model", "m",
        "--api-key-env", "MY_KEY", "--label", "L", "--instructions", "i.txt", "--dry-run",
    ])
    assert args.aqda == driver.DEFAULT_AQDA
    assert args.api_key_env == "MY_KEY"
    assert args.dry_run and not args.allow_new_codes and args.limit is None


def test_driver_codes_a_project_end_to_end(tmp_path, use_data_dir, monkeypatch, capsys):
    """The whole loop against AQDA in-process; the model is a stand-in, nothing goes online."""
    import asyncio

    from fastapi.testclient import TestClient

    import aqda.db as db_module
    from aqda.app import app

    use_data_dir(tmp_path / "data")
    asyncio.run(db_module.init_db())
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    project = client.post("/api/projects", json={"name": "Driver"}).json()
    pid = project["id"]
    economy = client.post("/api/codes", json={"project_id": pid, "name": "Economy"}).json()
    client.post("/api/codes", json={"project_id": pid, "name": "Jobs",
                                    "parent_id": economy["id"]})

    async def add_documents():
        db = await db_module.get_db()
        try:
            for name, content, excluded in [
                ("A", "We want jobs. Taxes are high.", 0),
                ("B", "Nothing to see.", 0),
                ("C", "Excluded text.", 1),
                ("D", "   ", 0),
            ]:
                await db.execute(
                    "INSERT INTO document (project_id, name, content, exclude_from_ai) "
                    "VALUES (?, ?, ?, ?)",
                    (pid, name, content, excluded),
                )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(add_documents())
    answers = {
        "A": '{"passages": [{"quote": "We want jobs.", "code_path": "Economy > Jobs"},'
             '{"quote": "We want houses.", "code_path": ["Economy"]}]}',
        "B": "Sorry, I cannot answer in JSON.",
    }
    model_calls = []

    def fake_http_json(method, url, payload=None, headers=None, timeout=60):
        if url.startswith("http://model.test"):
            model_calls.append((payload, headers))
            document = payload["messages"][1]["content"].split("Document “")[1][0]
            return {"choices": [{"message": {"content": answers[document]}}]}
        path = url.removeprefix("http://127.0.0.1:8765")
        response = client.request(method, path, json=payload)
        if response.status_code >= 400:
            raise driver.RequestFailed(response.status_code, response.text)
        return response.json()

    monkeypatch.setattr(driver, "http_json", fake_http_json)
    monkeypatch.setenv("TEST_MODEL_KEY", "secret-value")
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("Code what people want.", encoding="utf-8")
    code = driver.main([
        "--project", str(pid), "--base-url", "http://model.test/v1", "--model", "m",
        "--api-key-env", "TEST_MODEL_KEY", "--label", "Test run",
        "--instructions", str(instructions),
    ])
    output = capsys.readouterr().out
    assert code == 0
    assert "secret-value" not in output
    assert model_calls[0][1] == {"Authorization": "Bearer secret-value"}
    # Reasoning is off unless asked for, and the program says so.
    assert model_calls[0][0]["reasoning_effort"] == "none"
    assert "Reasoning: none." in output
    assert len(model_calls) == 2  # C is excluded, D has no text
    assert "A: 1 placed, 1 refused" in output
    assert "B: the answer could not be read; recorded with no passages" in output
    assert "D: no text, skipped" in output

    run = client.get(f"/api/runs?project_id={pid}").json()[0]
    assert run["status"] == "finished"
    assert (run["documents_read"], run["codings"], run["refused"]) == (2, 1, 1)
    detail = client.get(f"/api/runs/{run['uuid']}").json()
    raw = {row["document_name"]: row["raw_response"] for row in detail["documents"]}
    assert raw["B"] == answers["B"]


def test_driver_shows_proposed_codes_to_later_documents(tmp_path, use_data_dir, monkeypatch):
    """With new codes allowed, a code proposed for one document is in the next prompt."""
    import asyncio

    from fastapi.testclient import TestClient

    import aqda.db as db_module
    from aqda.app import app

    use_data_dir(tmp_path / "data")
    asyncio.run(db_module.init_db())
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    pid = client.post("/api/projects", json={"name": "Open coding"}).json()["id"]

    async def add_documents():
        db = await db_module.get_db()
        try:
            for name, content in [("A", "We are proud."), ("B", "They are proud too.")]:
                await db.execute(
                    "INSERT INTO document (project_id, name, content) VALUES (?, ?, ?)",
                    (pid, name, content),
                )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(add_documents())
    prompts = []

    def fake_http_json(method, url, payload=None, headers=None, timeout=60):
        if url.startswith("http://model.test"):
            prompt = payload["messages"][1]["content"]
            prompts.append(prompt)
            if "Document “A”" in prompt:
                answer = ('{"passages": [{"quote": "We are proud.", "new_code": '
                          '{"name": "Pride", "description": "Pride in the work."}}]}')
            else:
                answer = ('{"passages": [{"quote": "They are proud too.", '
                          '"code_path": ["Proposed by Open pass", "Pride"]}]}')
            return {"choices": [{"message": {"content": answer}}]}
        response = client.request(method, url.removeprefix("http://127.0.0.1:8765"), json=payload)
        if response.status_code >= 400:
            raise driver.RequestFailed(response.status_code, response.text)
        return response.json()

    monkeypatch.setattr(driver, "http_json", fake_http_json)
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("Code openly.", encoding="utf-8")
    assert driver.main([
        "--project", str(pid), "--base-url", "http://model.test/v1", "--model", "m",
        "--label", "Open pass", "--instructions", str(instructions), "--allow-new-codes",
    ]) == 0
    assert "Pride" not in prompts[0].split("Document “")[0]
    assert '- ["Proposed by Open pass", "Pride"] — Pride in the work.' in prompts[1]
    codes = [code["name"] for code in client.get(f"/api/codes?project_id={pid}").json()]
    assert codes.count("Pride") == 1
    run = client.get(f"/api/runs?project_id={pid}").json()[0]
    assert (run["codings"], run["refused"]) == (2, 0)


def test_codes_for_run_hides_what_other_runs_proposed():
    """A run sees the researcher's codes and its own proposals, not another run's."""
    codes = [
        {"id": 1, "parent_id": None, "name": "Jobs"},
        {"id": 2, "parent_id": None, "name": "Proposed by A"},
        {"id": 3, "parent_id": 2, "name": "Pride"},
        {"id": 4, "parent_id": 3, "name": "Pride in craft"},
        {"id": 5, "parent_id": None, "name": "Proposed by B"},
        {"id": 6, "parent_id": 5, "name": "Solidarity"},
        # Proposed by A once, then moved into the codebook by the researcher.
        {"id": 7, "parent_id": 1, "name": "Adopted"},
    ]
    runs = [
        {"uuid": "a", "proposed_parent_code_id": 2},
        {"uuid": "b", "proposed_parent_code_id": 5},
        {"uuid": "c", "proposed_parent_code_id": None},
    ]

    def names(run_uuid):
        return [code["name"] for code in driver.codes_for_run(codes, runs, run_uuid)]

    assert names("a") == ["Jobs", "Proposed by A", "Pride", "Pride in craft", "Adopted"]
    assert names("b") == ["Jobs", "Proposed by B", "Solidarity", "Adopted"]
    # A run that has not proposed anything, and a dry run, see the researcher's codes only.
    assert names("c") == names(None) == ["Jobs", "Adopted"]


def test_temperature_can_be_left_out(monkeypatch):
    sent = []

    def fake_http_json(method, url, payload=None, headers=None, timeout=60):
        sent.append(payload)
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(driver, "http_json", fake_http_json)
    driver.ask_model("http://model.test/v1", "m", None, [], True, 5)
    driver.ask_model("http://model.test/v1", "m", None, [], False, 5, None)
    driver.ask_model("http://model.test/v1", "m", None, [], False, 5, None, "none")
    assert sent[0]["temperature"] == 0 and "response_format" in sent[0]
    assert "temperature" not in sent[1] and "response_format" not in sent[1]
    # The reasoning setting is sent only when asked for.
    assert "reasoning_effort" not in sent[0] and sent[2]["reasoning_effort"] == "none"


def test_driver_refuses_an_empty_codebook_without_new_codes(
    tmp_path, use_data_dir, monkeypatch, capsys
):
    """Nothing to apply and nothing may be proposed: say so before starting a run."""
    import asyncio

    from fastapi.testclient import TestClient

    import aqda.db as db_module
    from aqda.app import app

    use_data_dir(tmp_path / "data")
    asyncio.run(db_module.init_db())
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    pid = client.post("/api/projects", json={"name": "Empty"}).json()["id"]

    def fake_http_json(method, url, payload=None, headers=None, timeout=60):
        response = client.request(method, url.removeprefix("http://127.0.0.1:8765"), json=payload)
        return response.json()

    monkeypatch.setattr(driver, "http_json", fake_http_json)
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("Code openly.", encoding="utf-8")
    code = driver.main([
        "--project", str(pid), "--base-url", "http://model.test/v1", "--model", "m",
        "--label", "No codes", "--instructions", str(instructions),
    ])
    assert code == 2
    assert "--allow-new-codes" in capsys.readouterr().out
    assert client.get(f"/api/runs?project_id={pid}").json() == []


def test_codebook_lines_stay_unambiguous_for_names_with_colons():
    """A code named "Topic: detail" must not read as code "Topic" with a definition."""
    text = driver.codebook_text([
        {"path": ["Proposed by X", "Trust: Distrust of authority"], "name": "n",
         "description": "Skepticism towards promises."},
        {"path": ["Plain"], "name": "Plain", "description": ""},
    ])
    assert text.splitlines() == [
        '- ["Proposed by X", "Trust: Distrust of authority"] — Skepticism towards promises.',
        '- ["Plain"]',
    ]
    assert driver.codebook_text([]) == "(There are no codes yet.)"
