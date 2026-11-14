#!/usr/bin/env python3
"""Example driver: code the documents of an AQDA project with a language model.

The model never touches AQDA's database. This script reads each document's text and the
codebook over AQDA's local interface, asks a model through any OpenAI-compatible endpoint
(OpenRouter, or Ollama at http://localhost:11434/v1), and sends back verbatim quotes with
a code. AQDA places every quote itself and refuses, with a reason, any quote it cannot
place exactly once. Everything is recorded as one "model run"; in AQDA the researcher reads
what the model coded, accepts or removes single codings, and can remove or restore the run.

Only the Python standard library is used, so the file also runs on its own with any
Python 3.10+ (python model_coding.py ...). AQDA must be running.

Example (OpenRouter; the key is read from the environment variable you name):

    export OPENROUTER_API_KEY=...
    python -m aqda.examples.model_coding --project 3 \\
        --base-url https://openrouter.ai/api/v1 --model some/model \\
        --api-key-env OPENROUTER_API_KEY --label "First pass" \\
        --instructions my-instructions.txt --limit 5

Example (Ollama; no key needed):

    python -m aqda.examples.model_coding --project 3 \\
        --base-url http://localhost:11434/v1 --model qwen3.5:9b \\
        --label "Local pass" --instructions my-instructions.txt --dry-run

With --allow-new-codes the model may propose codes; the codes it proposed so far are shown
to it with every further document, so that it reuses them. This also works with an empty
codebook (open coding). Codes proposed by other runs are not shown, so several models can
code the same project independently.

The model is asked to answer without reasoning first (--reasoning none, the default); the
program says so when it starts. --reasoning low, medium or high lets it think.

Limits of this example: one request per document (a very long document may not fit the
model's context), no retries, and no batching.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_AQDA = "http://127.0.0.1:8765"


# ---------------------------------------------------------------------------
# 1. Talking to AQDA and to the model over HTTP
# ---------------------------------------------------------------------------

class RequestFailed(Exception):
    """An HTTP request that failed; `status` is None when there was no answer at all."""

    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


# Send a JSON request and return the decoded JSON answer.
def http_json(method: str, url: str, payload=None, headers=None, timeout: float = 60):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except (ValueError, AttributeError):
            pass
        raise RequestFailed(exc.code, f"{exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RequestFailed(None, f"no answer from {url}: {exc}") from exc
    return json.loads(body) if body else None


class Aqda:
    """The few AQDA routes this driver uses."""

    def __init__(self, address: str):
        self.base = address.rstrip("/") + "/api"

    def get(self, path: str):
        return http_json("GET", self.base + path)

    def post(self, path: str, payload=None):
        return http_json("POST", self.base + path, payload or {}, timeout=300)


# Ask the model once and return the text of its answer.
def ask_model(
    base_url: str, model: str, api_key: str | None, messages: list[dict],
    json_mode: bool, timeout: float, temperature: float | None = 0,
    reasoning: str | None = None,
) -> str:
    payload = {"model": model, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    if reasoning:
        payload["reasoning_effort"] = reasoning
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    answer = http_json(
        "POST", base_url.rstrip("/") + "/chat/completions", payload, headers, timeout
    )
    try:
        return answer["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return json.dumps(answer)


# ---------------------------------------------------------------------------
# 2. Building the prompt (pure functions, tested without a network)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You assist a qualitative researcher by coding documents. Follow the researcher's "
    "instructions and answer only with JSON."
)

FIELD_HINTS = {
    "yes_no": 'answer "yes" or "no"',
    "number": "answer with a whole number",
    "text": "answer with a short text",
}


# Turn the codes from GET /api/codes into the codebook shape a run records.
def codebook_from_codes(codes: list[dict]) -> list[dict]:
    by_id = {code["id"]: code for code in codes}

    def path(code: dict) -> list[str]:
        names, seen = [], set()
        while code is not None and code["id"] not in seen:
            seen.add(code["id"])
            names.insert(0, code["name"])
            code = by_id.get(code.get("parent_id"))
        return names

    entries = [
        {"path": path(code), "name": code["name"], "description": code.get("description") or ""}
        for code in codes
    ]
    return sorted(entries, key=lambda entry: [name.casefold() for name in entry["path"]])


# The codes a run works with: the researcher's codebook plus the codes this run proposed.
# Codes that other runs proposed and that still sit under their "Proposed by ..." code are
# left out, so several models can code the same project without seeing each other's ideas.
# A proposed code the researcher has moved into the codebook counts as the researcher's.
def codes_for_run(codes: list[dict], runs: list[dict], run_uuid: str | None) -> list[dict]:
    foreign_parents = {
        run["proposed_parent_code_id"] for run in runs
        if run.get("proposed_parent_code_id") is not None and run.get("uuid") != run_uuid
    }
    by_id = {code["id"]: code for code in codes}

    def foreign(code: dict) -> bool:
        seen = set()
        while code is not None and code["id"] not in seen:
            if code["id"] in foreign_parents:
                return True
            seen.add(code["id"])
            code = by_id.get(code.get("parent_id"))
        return False

    return [code for code in codes if not foreign(code)]


# One line per code: its path exactly as the answer must give it (a JSON list), then its
# definition. A plain "name: definition" line is ambiguous for a code named "Topic: detail";
# a model then takes "Topic" for the code.
def codebook_text(codebook: list[dict]) -> str:
    lines = []
    for entry in codebook:
        line = "- " + json.dumps(entry["path"], ensure_ascii=False)
        if entry.get("description"):
            line += " — " + " ".join(entry["description"].split())
        lines.append(line)
    return "\n".join(lines) if lines else "(There are no codes yet.)"


# Build the chat messages for one document.
def build_messages(
    instructions: str,
    codebook: list[dict],
    document_name: str,
    text: str,
    allow_new_codes: bool = False,
    fields: list[dict] | None = None,
) -> list[dict]:
    parts = [
        instructions.strip(),
        "Codes so far (each line is a code's path from the top level down; to use a code, "
        'copy the whole list into "code_path"):\n'
        + codebook_text(codebook),
    ]
    example = {"quote": "exact words copied from the document", "code_path": ["Parent", "Code"]}
    if fields:
        hints = [
            f'- "{field["name"]}": {FIELD_HINTS.get(field["kind"], "answer briefly")}'
            for field in fields
        ]
        parts.append(
            "For every passage also answer these questions under \"fields\" (leave one out "
            "if the passage does not say):\n" + "\n".join(hints)
        )
        example["fields"] = {field["name"]: "..." for field in fields}
    if allow_new_codes:
        parts.append(
            "If no code fits a relevant passage, you may propose a new code: give "
            '"new_code": {"name": "...", "description": "..."} instead of "code_path".'
        )
    parts.append(
        "Answer with JSON of this form:\n"
        + json.dumps({"passages": [example]}, ensure_ascii=False)
        + "\nRules: copy every quote character for character from the document; never "
        'shorten a quote with "..."; one passage per code and statement; if the same words '
        'occur more than once in the document, add "before" with the words that come '
        'directly before the quote. If the document contains nothing relevant at all, answer '
        '{"passages": []}.'
    )
    parts.append(f"Document “{document_name}”:\n<<<\n{text}\n>>>")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ---------------------------------------------------------------------------
# 3. Reading the model's answer (pure functions, tested without a network)
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


# Return the passages of an answer, or None when the answer cannot be read.
def parse_answer(content: str) -> list | None:
    if not isinstance(content, str):
        return None
    fenced = _FENCE.match(content)
    candidates = [fenced.group(1) if fenced else content]
    start, end = content.find("{"), content.rfind("}")
    if 0 <= start < end:
        candidates.append(content[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("passages"), list):
            return [normalise_passage(passage) for passage in parsed["passages"]]
        if isinstance(parsed, list):
            return [normalise_passage(passage) for passage in parsed]
    return None


# Bring one passage into the shape AQDA expects; anything odd is left for AQDA to refuse.
def normalise_passage(passage):
    if not isinstance(passage, dict):
        return passage
    result = {
        key: passage[key]
        for key in ("quote", "before", "after", "code_path", "code_id", "new_code", "fields")
        if passage.get(key) is not None
    }
    # Models often write a path as one string, or call it "code".
    path = result.get("code_path", passage.get("code"))
    if isinstance(path, str):
        path = [name.strip() for name in path.split(">") if name.strip()]
    if path is not None and "code_id" not in result and "new_code" not in result:
        result["code_path"] = path
    return result


# ---------------------------------------------------------------------------
# 4. The run: one request per document
# ---------------------------------------------------------------------------

# What the program says about the reasoning setting when a run starts.
REASONING_NOTES = {
    "none": "Reasoning: none. The model answers directly (--reasoning low, medium or high "
            "lets it think first, which is slower).",
    "low": "Reasoning: low.",
    "medium": "Reasoning: medium.",
    "high": "Reasoning: high. Answers can take minutes per document.",
    "default": "Reasoning: left to the service (no setting sent).",
}


# Read the command line.
def parse_arguments(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--aqda", default=DEFAULT_AQDA, help="AQDA address")
    parser.add_argument("--project", type=int, required=True, help="AQDA project ID")
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible base URL")
    parser.add_argument("--model", required=True, help="model name at that endpoint")
    parser.add_argument(
        "--api-key-env", default=None,
        help="NAME of the environment variable that holds the API key (not the key)",
    )
    parser.add_argument("--label", required=True, help="name of the run, shown in AQDA")
    parser.add_argument(
        "--instructions", required=True, type=Path, help="text file with coding instructions"
    )
    parser.add_argument("--limit", type=int, default=None, help="code at most N documents")
    parser.add_argument(
        "--allow-new-codes", action="store_true", help="let the model propose new codes"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show the documents and the first prompt; call no model, change nothing",
    )
    parser.add_argument(
        "--no-json-mode", action="store_true",
        help="do not ask the endpoint for JSON output (for models that do not support it)",
    )
    parser.add_argument(
        "--no-temperature", action="store_true",
        help="do not send temperature 0 (for services that refuse it, such as Claude models)",
    )
    parser.add_argument(
        "--reasoning", choices=["none", "low", "medium", "high", "default"], default="none",
        help="how much the model reasons before it answers. The default is none: the model "
             "answers directly, which is fast and keeps local reasoning models from thinking "
             "for minutes. Raise it with low, medium or high; 'default' leaves the setting "
             "out, so the service decides",
    )
    parser.add_argument("--timeout", type=float, default=600, help="seconds per model answer")
    return parser.parse_args(argv)


# Run the whole loop; return a process exit code.
def main(argv=None) -> int:
    args = parse_arguments(argv)
    instructions = args.instructions.read_text(encoding="utf-8")
    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    if args.api_key_env and not api_key:
        print(f"The environment variable {args.api_key_env} is empty or not set.")
        return 2
    aqda = Aqda(args.aqda)

    # The documents to code: not excluded from AI, in name order.
    documents = [
        document for document in aqda.get(f"/documents?project_id={args.project}")
        if not document.get("exclude_from_ai")
    ]
    if args.limit is not None:
        documents = documents[:args.limit]
    fields = aqda.get(f"/coding-fields?project_id={args.project}")
    temperature = None if args.no_temperature else 0
    # "default" means: send no reasoning setting at all.
    reasoning = None if args.reasoning == "default" else args.reasoning

    # Read the codebook as a run sees it (see codes_for_run).
    def read_codebook(run_uuid: str | None) -> list[dict]:
        codes = aqda.get(f"/codes?project_id={args.project}")
        runs = aqda.get(f"/runs?project_id={args.project}&include_removed=true")
        return codebook_from_codes(codes_for_run(codes, runs, run_uuid))

    if not args.allow_new_codes and not read_codebook(None):
        print(
            "This project has no codes for the model to apply. Add --allow-new-codes to let "
            "the model propose codes (open coding), or create codes in AQDA first."
        )
        return 2

    if args.dry_run:
        codebook = read_codebook(None)
        print(f"Dry run: {len(documents)} documents would be sent to {args.model}.")
        print(REASONING_NOTES[args.reasoning])
        for document in documents:
            print(f"  {document['id']}: {document['name']}")
        if documents:
            first = aqda.get(f"/documents/{documents[0]['id']}/text")
            messages = build_messages(
                instructions, codebook, first["name"], first["text"],
                args.allow_new_codes, fields,
            )
            print("\nPrompt for the first document:\n")
            print(messages[1]["content"])
        return 0

    run = aqda.post("/runs", {
        "project_id": args.project,
        "label": args.label,
        "model": args.model,
        "instructions": instructions,
        "settings": {
            "base_url": args.base_url,
            "temperature": temperature,
            "reasoning": args.reasoning,
            "json_mode": not args.no_json_mode,
            "driver": "aqda.examples.model_coding",
        },
        "allow_new_codes": args.allow_new_codes,
    })
    run_path = f"/runs/{run['uuid']}?project_id={args.project}"
    submit_path = f"/runs/{run['uuid']}/documents/{{}}?project_id={args.project}"
    print(f"Started run “{run['label']}” ({run['uuid']}) on {len(documents)} documents.")
    print(REASONING_NOTES[args.reasoning])

    totals = {"documents": 0, "accepted": 0, "refused": 0, "unreadable": 0, "failed": 0}
    status = "finished"
    hinted = False
    try:
        for number, document in enumerate(documents, 1):
            prefix = f"[{number}/{len(documents)}] {document['name']}:"
            text = aqda.get(f"/documents/{document['id']}/text")
            if not text["text"].strip():
                print(f"{prefix} no text, skipped")
                continue
            # Read the codebook again for every document, so the model sees the codes it
            # proposed so far and reuses them instead of proposing a near-duplicate each
            # time. Codes proposed by other runs are not shown.
            codebook = read_codebook(run["uuid"])
            messages = build_messages(
                instructions, codebook, text["name"], text["text"],
                args.allow_new_codes, fields,
            )
            try:
                answer = ask_model(
                    args.base_url, args.model, api_key, messages,
                    not args.no_json_mode, args.timeout, temperature, reasoning,
                )
            except RequestFailed as exc:
                print(f"{prefix} the model did not answer ({exc}); not recorded")
                if exc.status is None and args.reasoning != "none" and not hinted:
                    hinted = True
                    print(
                        "  A local reasoning model may think for a very long time. "
                        "Try --reasoning none or low."
                    )
                if exc.status == 400 and not totals["documents"] and not hinted:
                    hinted = True
                    print(
                        "  Some services refuse parts of the request. Try --no-json-mode, "
                        "--no-temperature and --reasoning default."
                    )
                totals["failed"] += 1
                continue
            passages = parse_answer(answer)
            unreadable = passages is None
            try:
                result = aqda.post(submit_path.format(document["id"]), {
                    "text_hash": text["text_hash"],
                    "raw_response": answer,
                    "passages": passages or [],
                })
            except RequestFailed as exc:
                print(f"{prefix} not saved ({exc})")
                totals["failed"] += 1
                current = aqda.get(run_path)
                if current["removed"] or current["status"] != "running":
                    print("The run was stopped or removed in AQDA; ending here.")
                    status = None
                    break
                continue
            counts = result["counts"]
            totals["documents"] += 1
            totals["accepted"] += counts["accepted"]
            totals["refused"] += counts["refused"]
            if unreadable:
                totals["unreadable"] += 1
                print(f"{prefix} the answer could not be read; recorded with no passages")
            else:
                print(f"{prefix} {counts['accepted']} placed, {counts['refused']} refused")
    except KeyboardInterrupt:
        status = "stopped"
        print("\nInterrupted; marking the run as stopped.")
    finally:
        if status is not None:
            try:
                aqda.post(
                    f"/runs/{run['uuid']}/finish?project_id={args.project}", {"status": status}
                )
            except RequestFailed as exc:
                print(f"Could not mark the run as {status}: {exc}")

    if totals["documents"] and not totals["accepted"] and not totals["refused"]:
        print(
            "The model returned no passages for any document. Some models need to reason "
            "before they answer: try --reasoning low."
        )
    print(
        f"Done: {totals['documents']} documents recorded, {totals['accepted']} codings, "
        f"{totals['refused']} quotes refused, {totals['unreadable']} unreadable answers, "
        f"{totals['failed']} documents not recorded."
    )
    return 0 if status is not None else 1


if __name__ == "__main__":
    sys.exit(main())
