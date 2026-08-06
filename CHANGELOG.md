# Changelog

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
