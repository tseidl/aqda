import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { codings as codingsApi, runs as runsApi, settings as settingsApi, type Coding, type ModelRun } from '../api';
import { useStoredState } from './storedState';

/** How often the runs list (and with it the open document's codings) refreshes while a
 *  model run is still coding. */
export const RUN_REFRESH_MS = 4000;

/** A running run without activity for this long is no longer taken as in progress (as
 *  RUN_ACTIVITY_MINUTES in aqda/services/coding_runs.py): probably its program stopped. */
export const RUN_ACTIVITY_WINDOW_MS = 30 * 60 * 1000;

// A server time (UTC, "YYYY-MM-DD HH:MM:SS") as a Date; null when missing or unreadable.
export function parseServerTime(stamp: string | null | undefined): Date | null {
  if (!stamp) return null;
  const date = new Date(`${stamp.slice(0, 19).replace(' ', 'T')}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

// Whether a run is still coding: running, not removed, and active in the last 30 minutes.
export function runInProgress(run: ModelRun, now: number): boolean {
  if (run.status !== 'running' || run.removed) return false;
  const last = parseServerTime(run.last_activity_at ?? run.started_at);
  return last !== null && now - last.getTime() < RUN_ACTIVITY_WINDOW_MS;
}

// The current time, updated every minute, for telling in-progress runs from stale ones.
export function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

/** Who made a group of codings: one person coding by hand, or one model run. */
export interface CoderLayer {
  /** Stable key: "hand:<coder name>" or "run:<run uuid>" (uuids survive collaborator updates). */
  key: string;
  kind: 'hand' | 'run';
  label: string;
  /** The model's name, for runs. */
  model?: string;
  /** Active codings in this layer. */
  count: number;
}

/** Start of the collaboration status while sync waits for a model run to finish
 *  (RUN_PAUSE_PREFIX in aqda/services/coding_runs.py); shown as a notice, not a problem. */
export const RUN_PAUSE_PREFIX = 'AQDA waits for the model run';

/** Shown for hand codings saved without a coder name (none set in Settings). */
export const NO_CODER_NAME = 'No coder name';

// The key of a hand coder's layer.
export const handLayerKey = (coder: string | null | undefined) => `hand:${coder ?? ''}`;
// The key of a model run's layer.
export const runLayerKey = (uuid: string) => `run:${uuid}`;

// The layer key of one coding. A run that is not (yet) in the runs list gets a key that is
// never stored, so its codings stay visible rather than vanish unexplained.
export function codingLayerKey(coding: Coding, runUuids: Map<number, string>): string {
  if (coding.run_id == null) return handLayerKey(coding.coder);
  const uuid = runUuids.get(coding.run_id);
  return uuid ? runLayerKey(uuid) : `run-id:${coding.run_id}`;
}

// The project's model runs, removed ones included, newest first. While a run is coding
// (active within the last 30 minutes), the list refreshes every few seconds; a run left
// "running" by a program that stopped does not keep it polling. New codings also follow
// through the project revision. `loaded` is false until the first answer arrives.
export function useModelRuns(projectId: number): { runs: ModelRun[]; loaded: boolean } {
  const { data, isSuccess } = useQuery({
    queryKey: ['model-runs', projectId],
    queryFn: () => runsApi.list(projectId, true),
    refetchInterval: (query) =>
      query.state.data?.some((run) => runInProgress(run, Date.now())) ? RUN_REFRESH_MS : false,
  });
  return useMemo(() => ({ runs: data ?? [], loaded: isSuccess }), [data, isSuccess]);
}

// The coder name this computer stamps on hand codings ('' when none is set).
export function useCoderName(): string {
  const { data } = useQuery({ queryKey: ['settings'], queryFn: settingsApi.get, staleTime: 60000 });
  return data?.coder_name ?? '';
}

// Everyone who has coded in the project: hand coders by name, then model runs (not removed).
export function useCoderLayers(projectId: number, runList: ModelRun[]): CoderLayer[] {
  // Same query as the Segments tab without a code filter, so the two share one request.
  const { data: projectCodings = [] } = useQuery({
    queryKey: ['codings', 'project', projectId, null],
    queryFn: () => codingsApi.list({ project_id: projectId }),
  });
  return useMemo(() => {
    const handCounts = new Map<string, number>();
    for (const coding of projectCodings) {
      if (coding.run_id != null) continue;
      const name = coding.coder ?? '';
      handCounts.set(name, (handCounts.get(name) ?? 0) + 1);
    }
    const hand: CoderLayer[] = [...handCounts.entries()]
      .sort(([a], [b]) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b)))
      .map(([name, count]) => ({
        key: handLayerKey(name), kind: 'hand', label: name || NO_CODER_NAME, count,
      }));
    const models: CoderLayer[] = runList
      .filter((run) => !run.removed)
      .map((run) => ({
        key: runLayerKey(run.uuid), kind: 'run', label: run.label, model: run.model, count: run.codings,
      }));
    return [...hand, ...models];
  }, [projectCodings, runList]);
}

/** Which coders' codings the document view hides, per project, remembered in this browser.
 *  Everything is shown by default. */
export function useHiddenLayers(projectId: number) {
  const [stored, setStored] = useStoredState<Record<string, string[]>>('aqda.hiddenCoders', {});
  const hidden = useMemo(() => new Set(stored[String(projectId)] ?? []), [stored, projectId]);

  // Show or hide one layer.
  const toggle = useCallback((key: string) => {
    setStored((current) => {
      const keys = new Set(current[String(projectId)] ?? []);
      if (keys.has(key)) keys.delete(key);
      else keys.add(key);
      const next = { ...current };
      if (keys.size) next[String(projectId)] = [...keys];
      else delete next[String(projectId)];
      return next;
    });
  }, [setStored, projectId]);

  // Show every layer again.
  const showAll = useCallback(() => {
    setStored((current) => {
      const next = { ...current };
      delete next[String(projectId)];
      return next;
    });
  }, [setStored, projectId]);

  return { hidden, toggle, showAll };
}

// Whether two codings carry the same field answers (an unanswered field is simply absent).
export function sameAnswers(a: Record<string, string> | undefined, b: Record<string, string> | undefined): boolean {
  const left = Object.entries(a ?? {});
  const right = b ?? {};
  return left.length === Object.keys(right).length && left.every(([name, value]) => right[name] === value);
}
