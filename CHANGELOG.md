# Changelog

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
