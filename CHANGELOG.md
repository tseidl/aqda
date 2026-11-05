# Changelog

## 0.6.0

### New features
- **Import spreadsheets** — importing a single `.csv`, `.tsv`, or `.xlsx` file opens a table
  import: each row becomes a document, you choose the column with the text to code and
  (optionally) the column that names the documents, and the other columns become document
  variables. Semicolon-separated CSV files (common in German Excel) are recognized; for Excel
  files you can choose the sheet. Rows directly below the column names can be skipped; for a
  Qualtrics export, AQDA recognizes its two extra header rows (question wording and import
  IDs) and skips them by default. Rows without text are skipped and counted. Spreadsheets in
  a multi-file import are skipped with a note to import them on their own.

### Improvements
- **Skipped files are listed in full** — when a multi-file import skips files, the Docs
  panel now lists every skipped file with its reason until dismissed (before: the first three).
  Files that arrive empty come with a hint that online-only cloud files (Dropbox, Google
  Drive) must be made available offline first.
- **Passage memos** — the cursor starts in the note instead of the optional title, and a memo
  you have typed is saved when you click elsewhere, select another passage, or switch
  documents. Only Cancel or the close button discard it.

### Bug fixes
- **The text no longer jumps when the coding popup opens** — in projects with more than eight
  codes, the popup's filter box scrolled the document whenever the popup opened partly out of
  view (typically for a passage a few lines below the top of the window). The popup now opens
  on the side of the passage where it fits, and its fields take focus without scrolling.
- **The passage being coded stays marked** — the selection highlight disappeared once the
  popup's filter or memo field took focus; the passage now stays marked until the popup closes.
- **Passage highlights stay on their document** — after opening a passage from an AI result,
  a memo, or the Segments tab, its grey highlight was also drawn on the next document opened,
  at the same positions, and its 4-second fade restarted whenever the screen updated.
- **Switching documents closes the coding popup** — it stayed open on the new document, and
  choosing a code there would have coded the new document at the old document's positions.

## 0.5.0

### New features
- **Restore deleted codes** — *Deleted codes* at the bottom of the code list shows each
  deletion with its child codes and coded segments; **Restore** brings back exactly what that
  deletion removed. The server side already existed but had no place in the interface.
- **Exclude documents from AI features** — *Exclude documents…* in the AI panel replaces the
  per-document "Reference" flag. Excluded documents, such as interview guides, instructions,
  or files collecting example quotes, are ignored by every AI feature: they are not searched,
  and their coded segments no longer feed Code Suggest examples, summaries, definitions,
  Hierarchy Suggest, or the Consistency Check. Existing "Reference" marks carry over.

### Bug fixes
- **Audio transcripts can be coded by hand** — selecting transcript text never opened the
  coding popup, and clicking a coded transcript passage showed nothing.
- **Collaborator updates no longer strand the open document** — when a collaborator's
  version arrived, the open document kept showing but lost its highlights, and new codings on
  it failed. The open document, code, and memo now follow the update or close if removed.
- **Summaries and definitions report the passages they send** — AQDA now sets the model's
  context window and answer length explicitly, and sends only as many passages as fit by a
  deliberately generous length estimate. When a code has more, a selection spread over all its
  passages is sent and the result says, for example, "Based on 34 of 120 coded segments".
  Previously Ollama could silently drop passages, and definitions used an arbitrary 50.
  Hierarchy Suggest says when it left out example passages or codes of a long codebook. An
  answer that stops at the length limit is marked as possibly incomplete.
- **Deleting a document keeps its memos** — memos linked to the document are no longer
  deleted with it. Each kept memo starts with a note naming the deleted document and the date,
  and quoting the passage for memos about a passage; a memo also linked to a code stays linked
  to that code. The confirmation states how many coded segments will be lost and that
  documents cannot be restored.
- **Selecting whole lines works** — triple-click and selections that start or end outside
  the text now open the coding popup; surrounding spaces and line breaks are left out of the
  coded passage.
- **Find in document** highlights the right text in documents containing a capital "İ".
- **AI errors show the actual reason** — any message containing "400" used to appear as
  "No model configured".
- **Memo edits are not lost silently** — memo saves and deletions happen strictly in order,
  an edit still waiting is sent when you leave the memo, and an edit the server rejects (for
  example, after a collaborator's version replaced the memo) is kept in this browser, also
  across a reload: the memo offers to restore it (showing what would be restored), and the
  memo list offers it for copying until it is saved or discarded.
- Creating a code from the selection popup reports failures instead of doing nothing.

### Improvements
- Topic Search and Code Suggest compute similarities off the server's main loop, so the rest
  of AQDA stays responsive during searches on large projects.
- Settings explains that an Ollama address on another machine sends passages to it.
- Removed the unused `/api/codings/audit` endpoint; the Segments panel already flags
  segments whose position was repaired.

## 0.4.1

### Bug fixes

- Word import reads one text-bearing alternative per `AlternateContent` block, avoiding
  duplicated text, and retains fallback text when the preferred choice contains only graphics.
- AI searches and consistency checks report failed or incomplete embedding batches instead
  of silently returning partial results. Successful checks use all requested segments.

## 0.4.0

### New features
- **Word documents** — `.docx` files import as plain text (paragraphs preserved, formatting
  dropped). Other office formats (`.doc`, `.rtf`, `.odt`, spreadsheets, presentations) are
  refused with a clear message instead of being imported as garbage.
- **Richer REFI-QDA export** — `.qdpx` now carries document variables (as REFI variables on
  each source) and memos linked to their code, coded passage, passage (as a selection of its
  own), or document; project memos are linked to the project. Empty projects export without
  schema-invalid empty containers. Validated against the official REFI-QDA XSD.
- **Filter box in the apply-code popup** — with more than eight codes, typing narrows the
  list and Enter applies the first match.
- **Paragraph-aware chunking (optional)** — Settings › Text Chunking can keep AI search
  chunks within one line (one paragraph or speaker turn), with a warning about formats that
  break every line.

### Bug fixes
- **`aqda --port` works again** — the same-origin check followed a fixed port list, so on
  any other port the browser's own requests were rejected as cross-site and nothing could
  be saved. The check now follows the port AQDA runs on.
- **Consistency Check now finds outliers** — its fixed similarity threshold never fired with
  real embedding models (even unrelated text scores above it). Outliers are now flagged
  relative to each code: more than 1.5 standard deviations below the code's mean, with a
  minimum gap, for codes with at least four segments; codes with fewer say so instead of
  reporting "consistent".
- **Code Suggest never proposes an already-coded passage** — suggestions are now checked
  against existing codings and de-duplicated *after* snapping to sentence boundaries, applying
  one removes overlapping suggestions from the list, and results stay bound to the code they
  were produced for even if the selector changes afterwards.
- **Restored codes no longer vanish from exports** — a code restored while its former parent
  is still in the trash (or a code caught in a parent cycle in imported data) is exported as
  a top-level code instead of being dropped together with its codings.
- **Typing a code definition no longer loses keystrokes** when an autosave completes
  mid-sentence; edits arriving from elsewhere are still picked up while the draft is untouched.
- **Topic Search no longer silently narrows to a code** selected earlier in another AI mode.
- **Project dates** are read as UTC (Safari showed "Invalid Date", Chrome shifted them by the
  local timezone); a missing date no longer breaks the project list.
- **Cross-site image or iframe loads** can no longer trigger snapshot exports; browsers'
  `Sec-Fetch-Site` header is honoured for every request method.
- A malformed `Host` port now returns a clear 400 instead of a server error.

### Improvements
- **Collaboration backups are pruned** — the full-database backup taken before a
  collaborator's version replaces local data used to accumulate without limit; the ten newest
  are kept by default (Settings › Collaboration).
- **Cancel in the AI panel stops the server too** — embedding halts after the current batch
  and already-embedded chunks stay cached.
- **The same code cannot be applied twice to exactly the same passage**; different codes on
  one passage remain possible as before.
- Large documents stay responsive: coding offsets, find matches, and selections no longer
  re-scan the whole text per conversion.
- Building a collaboration snapshot runs off the event loop, so background publishing of
  large (audio) projects no longer freezes the UI.
- Error messages show the server's explanation instead of a raw JSON body.
- The fresh-database schema version is derived from the migration table.

### Removed
- **AI Analyze** (free-form LLM interpretation of a selected passage) — the one feature where
  the model produced the analysis itself rather than reflecting on the researcher's coding.
- The `--host` flag: AQDA is localhost-only by design, and the flag could not work anyway.
- Unused API endpoints (`negative-cases`, `embedding-status`, `analyze`).

## 0.3.2

### New features
- **Filter documents by variables** — the Docs panel's filter controls now offer one
  dropdown per document variable (with per-value counts); active filters combine, the
  funnel icon turns indigo while any filter is active, and a one-click reset clears them.
  Variables with more than 25 distinct values (IDs, dates) are not offered as filters.

## 0.3.0

### New features
- **Safer turn-based collaboration** — `.aqda` files now carry stable project lineage and
  snapshot ancestry. A clean local project fast-forwards from a collaborator's newer
  snapshot after an automatic database backup; divergent edits are never overwritten and
  can be kept as a clearly named conflicting copy.
- **Automatic shared-folder collaboration** — save multiple Google Drive, Dropbox, OneDrive,
  university-cloud, or ordinary folders and choose the appropriate team location for each
  project. AQDA works from a hidden local cache, publishes immutable snapshots in the
  background, pulls collaborators' changes, and automatically keeps both versions when work
  diverges.
- **Safe Close AQDA button** — performs a final shared-project sync and graceful shutdown;
  pressing Ctrl+C once remains the terminal equivalent.

### Improvements
- Project activity now advances a revision and updates the project modification time.
- `.aqda` round-trips preserve memo-to-coding links.
- Canonical Unicode code-point offsets, server-side span validation, and automatic repair
  of legacy codings prevent find-bar and emoji-related annotation drift.
- Parent-code deletion now warns about impact, moves the complete subtree to trash, and
  restores only the codes/codings deleted by that exact operation.
- Verified pre-migration and rolling daily backups are stored in `~/.aqda/backups/`.
- JSON analysis exports include document variables; XML exports avoid double escaping;
  Unicode project filenames use standards-compliant download headers.
- Embedding cache keys include chunk content and stale rows are invalidated automatically.
- Unknown `/api` routes now return a JSON 404 instead of the frontend HTML.
- Concurrent collaboration branches now stay in one local reference project that follows
  later remote snapshots instead of multiplying projects and shared folders.
- Collaborator-reference projects now offer an explicit resolution choice: keep the current
  shared branch, or switch after creating both a full backup and a named local archive.
- Background sync recovers from transient database errors and reports its health in the UI;
  state-changing localhost API requests reject cross-site browser origins.
- Stopping collaboration removes this computer's writer snapshot, and unchanged manual
  `.aqda` exports reuse their existing snapshot node instead of growing history indefinitely.
- Existing local copies can be connected to a shared project without duplication. If the
  local copy has additional work, AQDA explicitly asks whether to publish it or use the shared
  version after a safety backup.
- Collaboration Settings now confirms when a folder is active and explains that standalone
  `.aqda` files are import/archive files until a project is placed into collaboration.
- Pausing and resuming collaboration reuses the project's existing managed folder instead of
  creating numbered `(2)`, `(3)`, and later duplicates.
- Re-importing a project no longer adds a numeric suffix merely because an older project with
  that name remains in Trash.
- Projects can now be renamed directly from the project header. Shared projects keep their
  stable managed folder while publishing the new display name in subsequent snapshots.
- Collaboration Settings remembers multiple locations, and the **Collaborate** button offers
  a clear per-project choice between saved team folders or a new shared folder.
- Connecting an existing project now asks before acting for both newer and genuinely divergent
  local work. Users can adopt the shared version after a verified backup or explicitly keep
  both branches for comparison; AQDA never silently publishes the divergent branch.
- Localhost Host validation also protects read requests from DNS rebinding, contradictory
  attempts to move an already linked project return an error, and shared `project.json`
  metadata is replaced atomically.

## 0.2.0

### New features
- **Find in document** — Press Cmd/Ctrl+F while viewing a document to search its text, step through matches (Enter / Shift+Enter), and jump to each hit.
- **Filter coded segments by coder** — The Segments browser gains a coder filter (shown once codings carry coder attribution), and each segment now displays its coder.
- **Coder column in CSV export** — Coded-segment CSV exports now include a `coder` column (the JSON export already carried it).

### Bug fixes
- **Exports no longer include trashed codes/codings** — REFI-QDA (.qdpx), codebook (.qdc), CSV, and JSON exports now exclude soft-deleted items. The standalone .aqda backup still preserves them for exact round-tripping.
- **Fixed a hang in AI search** — The text chunker could loop forever with certain chunk-size/overlap settings; it now always makes forward progress.
- **Hardened the local file server** — The SPA route no longer serves files outside the app directory via crafted `..` paths.
- **Transcription and PDF import no longer freeze the app** — Whisper transcription and PDF text extraction run off the event loop, so the rest of the UI stays responsive.
- **Removing a coding is now recoverable** — Deleting a single coding soft-deletes it (consistent with code deletion) instead of removing it permanently.
- **Bulk import is more robust** — An unreadable or empty file is now reported as skipped instead of silently vanishing or aborting the whole batch.
- **Correct character count for audio/images** — Audio documents show the transcript length; images no longer show a meaningless base64 length.
- **Safer migrations** — A genuinely failing schema migration now surfaces instead of being silently swallowed.

## 0.1.2

- **Reference documents** — Mark documents as reference material to exclude them from AI search and code suggestions.
- **Code drag-and-drop** — Reorganize the code hierarchy by dragging codes within the tree.
- **Coder attribution** — Record a coder identity (Settings) that is stamped on codings and exported to REFI-QDA.
- **@-mentions** — Reference codes and memos with `@` in definitions and memos, with clickable jump-to links.
- **Audio search** — Transcribed audio is included in AI similarity search.

## 0.1.1

- **Improved bulk document import** — Importing large numbers of files (100+) no longer times out or silently fails. Uploads are now batched with a progress indicator.
- **Bulk delete documents** — Select multiple documents and delete them at once via the checkbox icon in the document list controls.
- **Parse variables from filenames** — Re-parse filename variables for already-imported documents (filter panel → "Parse variables from filenames").
- **Fixed sidebar resize handle** — The drag handle no longer overlaps the scroll area.

## 0.1.0

- Initial release
