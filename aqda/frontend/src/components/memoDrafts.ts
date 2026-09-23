import { useSyncExternalStore } from 'react';

/** A memo edit the server did not accept, kept until it is restored, saved, or discarded. */
export interface MemoDraft {
  key: string;
  projectId: number;
  title: string;
  content: string;
}

// Kept edits survive a reload of the page; they stay in this browser on this computer.
const STORAGE_KEY = 'aqda.memoDrafts';
const listeners = new Set<() => void>();
let snapshot: MemoDraft[] = readStored();

// Load kept edits from an earlier page session.
function readStored(): MemoDraft[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

// Store the kept edits and tell subscribed components.
function publish(next: MemoDraft[]) {
  snapshot = next;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // storage unavailable or full: the edits stay available until the page is closed
  }
  listeners.forEach((listener) => listener());
}

/** Identity of a memo that survives a collaborator's version replacing its ID. */
export const memoDraftKey = (memo: {
  project_id: number; created_at: string; modified_at: string; title: string;
}) => [memo.project_id, memo.created_at, memo.modified_at, memo.title].join('\u0000');

// Keep (or replace) the unsaved edit for one memo.
export function keepMemoDraft(draft: MemoDraft) {
  publish([...snapshot.filter((kept) => kept.key !== draft.key), draft]);
}

// Forget the kept edit for one memo, once it is saved or discarded.
export function dropMemoDraft(key: string) {
  if (snapshot.some((kept) => kept.key === key)) {
    publish(snapshot.filter((kept) => kept.key !== key));
  }
}

/** Kept memo edits of one project; components re-render when the set changes. */
export function useMemoDrafts(projectId: number): MemoDraft[] {
  const all = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    () => snapshot,
  );
  return all.filter((draft) => draft.projectId === projectId);
}

// Every memo save and delete in the app, one after another.
let queue: Promise<unknown> = Promise.resolve();

/** Run a memo request after all earlier ones, so an older save can never land last. */
export function queueMemoRequest<T>(request: () => Promise<T>): Promise<T> {
  const result = queue.then(request);
  queue = result.catch(() => undefined);
  return result;
}
