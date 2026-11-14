"""Model coding runs: shared rules that more than one module needs.

A run is "in progress" while its status is `running` and a program sent something within
the last `RUN_ACTIVITY_MINUTES`. During that time shared-folder sync leaves the project
alone, because applying a collaborator's version re-creates every record ID in the middle
of the run; sharing, resolving a conflict copy, and any import that would replace the
project's contents are refused. A run that went quiet for longer no longer blocks anything.
"""

from fastapi import HTTPException

RUN_ACTIVITY_MINUTES = 30

# The colour schemes of the code tree (keep in step with COLOR_SCHEMES in
# frontend/src/components/CodeTree.tsx), so codes proposed by a run get distinct colours
# the same way codes created by hand do.
CODE_COLOR_SCHEMES = {
    "Default": ["#6366f1", "#ec4899", "#f59e0b", "#10b981", "#3b82f6",
                "#8b5cf6", "#ef4444", "#14b8a6", "#f97316", "#06b6d4"],
    # The eight categorical colours of the seidlpal palette (github.com/tseidl/seidlpal),
    # chosen to stay distinguishable with the common forms of colour blindness.
    "Color-blind safe": ["#3D649E", "#E8772E", "#1E452F", "#029581",
                         "#982F5D", "#E8CB48", "#DF97AA", "#A1CCF1"],
    "Pastel": ["#a5b4fc", "#f9a8d4", "#fcd34d", "#6ee7b7", "#93c5fd",
               "#c4b5fd", "#fca5a5", "#5eead4", "#fdba74", "#67e8f9"],
    "Earthy": ["#92400e", "#78350f", "#365314", "#1e3a5f", "#4c1d95",
               "#831843", "#6b7280", "#b45309", "#166534", "#1e40af"],
    "Vivid": ["#dc2626", "#2563eb", "#16a34a", "#d97706", "#9333ea",
              "#0891b2", "#e11d48", "#4f46e5", "#059669", "#ea580c"],
}


# Pick the colour for a project's next new code: the next one in the chosen scheme.
async def next_code_color(db, project_id: int) -> str:
    row = await (
        await db.execute("SELECT value FROM setting WHERE key='color_scheme'")
    ).fetchone()
    colors = CODE_COLOR_SCHEMES.get(row["value"] if row else "", CODE_COLOR_SCHEMES["Default"])
    count = await (
        await db.execute(
            "SELECT COUNT(*) AS n FROM code WHERE project_id=? AND deleted_at IS NULL",
            (project_id,),
        )
    ).fetchone()
    return colors[count["n"] % len(colors)]


# Start of the shared-folder status shown while sync waits for a run; used to clear it.
RUN_PAUSE_PREFIX = "AQDA waits for the model run"


# The status message shown while a project's sync waits for a model run.
def run_pause_message(label: str) -> str:
    return (
        f"{RUN_PAUSE_PREFIX} “{label}” to finish before it exchanges changes with "
        "collaborators. Sync resumes on its own when the run is finished, stopped, or "
        f"removed, or after {RUN_ACTIVITY_MINUTES} minutes without activity."
    )


# Return the label of a project's model run that is still in progress, or None.
async def run_in_progress_label(db, project_id: int) -> str | None:
    row = await (
        await db.execute(
            "SELECT label FROM coding_run WHERE project_id=? AND status='running' "
            "AND deleted_at IS NULL AND last_activity_at >= datetime('now', ?) "
            "ORDER BY last_activity_at DESC LIMIT 1",
            (project_id, f"-{RUN_ACTIVITY_MINUTES} minutes"),
        )
    ).fetchone()
    return row["label"] if row else None


class RunInProgressError(HTTPException):
    """A step that would publish or replace a project while a model run is in progress.

    Replacing the project's contents re-creates every record ID under the running program,
    and publishing would hand collaborators a half-finished run. Shared-folder sync
    catches it and shows the message in the project's status.
    """

    def __init__(self, label: str, action: str):
        super().__init__(
            409,
            f"AQDA cannot {action} while the model run “{label}” is in progress. The run has "
            "to finish or be stopped first (Model runs → mark as stopped), then try again.",
        )


# Refuse ``action`` (e.g. "share this project") while one of these projects has a run going.
async def refuse_while_run_in_progress(db, project_ids, action: str) -> None:
    for project_id in project_ids:
        label = await run_in_progress_label(db, project_id)
        if label is not None:
            raise RunInProgressError(label, action)
