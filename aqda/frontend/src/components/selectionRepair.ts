import { useEffect, useRef } from 'react';

/** Keep a selection on the same record when the server replaces record IDs (a
 *  collaborator's version arriving), and clear it when the record is gone.
 *
 *  A missing ID is only acted on once the list was fetched after the selection was
 *  made, so a record created a moment ago is not dropped while its list reloads. The
 *  replacement must match `identify` exactly, be the only such record, and carry an ID
 *  that did not exist before; anything less certain clears the selection instead of
 *  guessing. Pass a stable (module-level) `identify`. */
export function useSelectionRepair<T extends { id: number }>(
  selectedId: number | null,
  setSelectedId: (id: number | null) => void,
  items: T[],
  listUpdatedAt: number,
  identify: (item: T) => string,
) {
  const selectedAt = useRef(0);
  const known = useRef<{ key: string; ids: Set<number> } | null>(null);

  useEffect(() => {
    selectedAt.current = Date.now();
    known.current = null;
  }, [selectedId]);

  useEffect(() => {
    if (selectedId == null) return;
    const current = items.find((item) => item.id === selectedId);
    if (current) {
      known.current = { key: identify(current), ids: new Set(items.map((item) => item.id)) };
      return;
    }
    if (listUpdatedAt <= selectedAt.current) return;
    const before = known.current;
    const matches = before ? items.filter((item) => identify(item) === before.key) : [];
    const replacement = matches.length === 1 && !before?.ids.has(matches[0].id) ? matches[0] : null;
    setSelectedId(replacement ? replacement.id : null);
  }, [selectedId, setSelectedId, items, listUpdatedAt, identify]);
}
