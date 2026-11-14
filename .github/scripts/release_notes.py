#!/usr/bin/env python3
"""Print the CHANGELOG.md section of one version, for the GitHub release of its tag.

Usage: python .github/scripts/release_notes.py 0.7.0
The section runs from the heading "## 0.7.0" to the next "## " heading; the heading itself
is left out, because the release already carries the version as its title. Wrapped lines
are joined. Exits with an
error if the version has no section, so a release is never published with empty notes.
"""

import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"


# Return the lines below "## <version>" up to the next second-level heading.
def section(version: str, text: str) -> str:
    lines = text.splitlines()
    try:
        start = lines.index(f"## {version}")
    except ValueError:
        return ""
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return unwrap("\n".join(body).strip())


# Join the hard-wrapped lines of a paragraph or list item. GitHub shows every line break of
# a release text as a break, so the changelog's wrapped lines would look ragged there.
def unwrap(text: str) -> str:
    result: list[str] = []
    in_code = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            result.append(line)
            continue
        starts_block = (
            not stripped
            or stripped.startswith(("- ", "* ", "#", "|", ">"))
            or stripped.split(". ", 1)[0].isdigit()
        )
        if in_code or starts_block or not result or not result[-1].strip():
            result.append(line)
        else:
            result[-1] = result[-1].rstrip() + " " + stripped
    return "\n".join(result)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    notes = section(sys.argv[1].removeprefix("v"), CHANGELOG.read_text(encoding="utf-8"))
    if not notes:
        sys.exit(f"CHANGELOG.md has no section for {sys.argv[1]}")
    print(notes)
