import { useState } from 'react';
import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { ArrowLeft, Bot, ChevronDown, ChevronRight, CircleStop, RotateCcw, Trash2 } from 'lucide-react';
import { runs as runsApi, type ModelRun, type ModelRunStatus } from '../api';
import { RUN_REFRESH_MS, parseServerTime, runInProgress, useModelRuns, useNow } from './coderLayers';
import { readableError } from './codingFields';

// "3 coded segments", "1 coded segment".
const countOf = (n: number, noun: string) => `${n} ${noun}${n === 1 ? '' : 's'}`;

// A server time (UTC, "YYYY-MM-DD HH:MM:SS") in the researcher's local date and time.
function formatWhen(stamp: string | null | undefined): string {
  if (!stamp) return '';
  const date = parseServerTime(stamp);
  return date ? date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : stamp;
}

// A setting's value as short text.
function settingText(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value);
}

// Refresh everything a change to a run affects: the runs, codings, counts, and answers.
function refreshAfterRunChange(queryClient: QueryClient, projectId: number) {
  queryClient.invalidateQueries({ queryKey: ['model-runs', projectId] });
  queryClient.invalidateQueries({ queryKey: ['model-run'] });
  queryClient.invalidateQueries({ queryKey: ['codings'] });
  queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
  queryClient.invalidateQueries({ queryKey: ['coding-fields', projectId] });
}

const STATUS_STYLE: Record<ModelRunStatus, string> = {
  running: 'bg-green-50 text-green-700',
  finished: 'bg-gray-100 text-gray-600',
  stopped: 'bg-amber-50 text-amber-700',
};

/** A run's status as a small chip; a running run has a pulsing dot. A run still marked
 *  running but without activity for 30 minutes is shown as no longer active. */
function StatusChip({ status, stale }: { status: ModelRunStatus; stale?: boolean }) {
  if (stale) {
    return (
      <span
        className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded font-medium shrink-0 bg-gray-100 text-gray-500"
        title="Still marked as running, but the program has sent nothing for over 30 minutes; it has probably stopped"
        data-run-stale
      >
        running · not active
      </span>
    );
  }
  return (
    <span className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded font-medium shrink-0 ${STATUS_STYLE[status]}`}>
      {status === 'running' && <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />}
      {status}
    </span>
  );
}

/** The footer entry of the AI panel that opens the list of model runs. It appears only
 *  once the project has a run (removed ones count, so they can be restored). */
export function ModelRunsEntry({ projectId, onOpen }: { projectId: number; onOpen: () => void }) {
  const { runs } = useModelRuns(projectId);
  const now = useNow();
  const active = runs.filter((run) => !run.removed);
  const running = active.filter((run) => runInProgress(run, now)).length;
  if (runs.length === 0) return null;
  return (
    <button
      onClick={onOpen}
      className="shrink-0 mx-2 mb-2 mt-1 px-2 py-1.5 rounded-md border border-gray-100 text-xs text-gray-600 hover:bg-gray-50 flex items-center gap-1.5"
      title="Coded segments that a language model made through a program on this computer"
      data-runs-entry
    >
      <Bot size={13} className="text-gray-400" />
      <span className="font-medium">Model runs</span>
      {active.length > 0 && <span className="text-gray-400">{active.length}</span>}
      {running > 0 && (
        <span className="inline-flex items-center gap-1 text-green-700">
          <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" /> {running} running
        </span>
      )}
      <ChevronRight size={12} className="ml-auto text-gray-400" />
    </button>
  );
}

/** Instructions, note, settings, and the documents a run read with its refused quotes. */
function RunDetails({ run, projectId, onOpenDocument }: {
  run: ModelRun; projectId: number; onOpenDocument: (docId: number) => void;
}) {
  const { data, error, isPending } = useQuery({
    queryKey: ['model-run', run.uuid, projectId],
    queryFn: () => runsApi.get(run.uuid, projectId),
    refetchInterval: () => (runInProgress(run, Date.now()) ? RUN_REFRESH_MS : false),
  });
  if (isPending) return <p className="text-[11px] text-gray-400 py-1">Loading…</p>;
  if (error) return <p className="text-[11px] text-red-600 py-1">{readableError(error)}</p>;
  const settings = Object.entries(data.settings ?? {});
  return (
    <div className="mt-2 space-y-2 text-[11px] text-gray-600">
      <div>
        <p className="text-[10px] text-gray-400 uppercase tracking-wider mb-0.5">Instructions</p>
        {data.instructions ? (
          <p className="whitespace-pre-wrap break-words max-h-40 overflow-auto rounded bg-gray-50 px-2 py-1">{data.instructions}</p>
        ) : (
          <p className="text-gray-400 italic">None recorded</p>
        )}
      </div>
      {data.note && (
        <div>
          <p className="text-[10px] text-gray-400 uppercase tracking-wider mb-0.5">Note</p>
          <p className="whitespace-pre-wrap break-words">{data.note}</p>
        </div>
      )}
      {settings.length > 0 && (
        <div>
          <p className="text-[10px] text-gray-400 uppercase tracking-wider mb-0.5">Settings</p>
          <ul className="space-y-0.5">
            {settings.map(([key, value]) => (
              <li key={key} className="break-words"><span className="font-medium">{key}:</span> {settingText(value)}</li>
            ))}
          </ul>
        </div>
      )}
      <p className="text-gray-400">
        Started {formatWhen(data.started_at)}
        {data.finished_at ? ` · ended ${formatWhen(data.finished_at)}` : data.last_activity_at ? ` · last activity ${formatWhen(data.last_activity_at)}` : ''}
        {data.allow_new_codes ? ' · could propose new codes' : ''}
      </p>
      <div>
        <p className="text-[10px] text-gray-400 uppercase tracking-wider mb-0.5">Documents read</p>
        {data.documents.length === 0 ? (
          <p className="text-gray-400 italic">None yet</p>
        ) : (
          <ul className="space-y-1.5 max-h-72 overflow-auto">
            {data.documents.map((doc) => (
              <li key={doc.document_id}>
                <div className="flex items-baseline gap-2">
                  <button
                    onClick={() => onOpenDocument(doc.document_id)}
                    className="text-indigo-600 hover:text-indigo-800 hover:underline truncate text-left"
                    title="Open this document"
                  >
                    {doc.document_name}
                  </button>
                  <span className="text-gray-400 shrink-0 ml-auto">
                    {countOf(doc.codings, 'coded segment')}
                    {doc.refused.length > 0 && <span className="text-amber-700"> · {doc.refused.length} refused</span>}
                  </span>
                </div>
                {doc.refused.length > 0 && (
                  <ul className="mt-0.5 ml-2 pl-2 border-l-2 border-amber-200 space-y-1">
                    {doc.refused.map((item) => (
                      <li key={item.index}>
                        {item.quote ? (
                          <span className="italic text-gray-700 break-words">
                            “{item.quote.length > 200 ? `${item.quote.slice(0, 200)}…` : item.quote}”
                          </span>
                        ) : (
                          <span className="italic text-gray-400">No quote</span>
                        )}
                        <span className="block text-amber-800">{item.reason}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/** One run with its counts, expandable details, and actions (stop, remove). */
function RunCard({ run, projectId, onOpenDocument }: {
  run: ModelRun; projectId: number; onOpenDocument: (docId: number) => void;
}) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const now = useNow();
  // Still marked running, but nothing arrived for 30 minutes: the program probably stopped.
  const stale = run.status === 'running' && !runInProgress(run, now);
  const stopMut = useMutation({
    mutationFn: () => runsApi.finish(run.uuid, projectId, 'stopped'),
    onSettled: () => refreshAfterRunChange(queryClient, projectId),
  });
  const removeMut = useMutation({
    mutationFn: () => runsApi.remove(run.uuid, projectId),
    onSuccess: () => setConfirmingRemove(false),
    onSettled: () => refreshAfterRunChange(queryClient, projectId),
  });
  const error = stopMut.error ?? removeMut.error;

  return (
    <div className="rounded-md border border-gray-100 p-2" data-run={run.label}>
      <div className="flex items-start gap-1.5">
        <button
          onClick={() => setExpanded(!expanded)}
          className="p-0.5 mt-0.5 text-gray-400 hover:text-gray-600 shrink-0"
          title={expanded ? 'Hide details' : 'Show details'}
        >
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <button onClick={() => setExpanded(!expanded)} className="text-sm font-medium text-gray-800 truncate text-left">
              {run.label}
            </button>
            <StatusChip status={run.status} stale={stale} />
          </div>
          {stale && (
            <p className="text-[11px] text-gray-500">
              No activity since {formatWhen(run.last_activity_at ?? run.started_at)}
            </p>
          )}
          <p className="text-[11px] text-gray-500 truncate" title={run.model}>
            {run.model || 'Model not named'} · {formatWhen(run.started_at)}
          </p>
          <p className="text-[11px] text-gray-500">
            {countOf(run.documents_read, 'document')} read · {countOf(run.codings + run.codings_accepted, 'coded segment')}
            {run.codings_accepted > 0 && <span title="Coded segments of this run that were accepted; they now belong to the coder who accepted them"> ({run.codings_accepted} accepted)</span>}
            {' · '}
            <span className={run.refused ? 'text-amber-700' : undefined}>{countOf(run.refused, 'refused quote')}</span>
          </p>
        </div>
      </div>

      {expanded && <RunDetails run={run} projectId={projectId} onOpenDocument={onOpenDocument} />}

      {confirmingRemove ? (
        <div className="mt-2 rounded-md border border-red-100 bg-red-50 p-2 text-[11px] text-red-800">
          <p>
            Remove “{run.label}”? {run.codings
              ? `Its ${countOf(run.codings, 'coded segment')} disappear${run.codings === 1 ? 's' : ''} from the documents, the Segments tab, and exports.`
              : 'It has no coded segments at the moment.'}
            {run.codings_accepted > 0 ? ` The ${countOf(run.codings_accepted, 'coded segment')} you accepted stay${run.codings_accepted === 1 ? 's' : ''}.` : ''}
            {run.status === 'running' ? ' The run is also marked as stopped, so the program cannot add to it.' : ''}
            {' '}You can restore the run, with its coded segments, under “Removed runs”.
          </p>
          <div className="mt-1.5 flex gap-1.5">
            <button
              onClick={() => removeMut.mutate()}
              disabled={removeMut.isPending}
              className="px-2 py-1 rounded bg-red-600 text-white font-medium hover:bg-red-700 disabled:opacity-50"
            >
              {removeMut.isPending ? 'Removing…' : 'Remove run'}
            </button>
            <button
              onClick={() => setConfirmingRemove(false)}
              className="px-2 py-1 rounded bg-white text-gray-600 border border-gray-200 hover:bg-gray-50"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-1.5 flex items-center justify-end gap-1">
          {run.status === 'running' && (
            <button
              onClick={() => stopMut.mutate()}
              disabled={stopMut.isPending}
              className="flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded text-gray-500 hover:text-amber-700 hover:bg-amber-50 disabled:opacity-50"
              title="The program can then no longer add to this run; its coded segments stay"
            >
              <CircleStop size={11} /> Mark as stopped
            </button>
          )}
          <button
            onClick={() => setConfirmingRemove(true)}
            className="flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded text-gray-500 hover:text-red-600 hover:bg-red-50"
          >
            <Trash2 size={11} /> Remove
          </button>
        </div>
      )}
      {error && <p className="mt-1 text-[11px] text-red-600" role="alert">{readableError(error)}</p>}
    </div>
  );
}

/** Removed runs, collapsed by default; each can be restored with its codings. */
function RemovedRuns({ runs, projectId }: { runs: ModelRun[]; projectId: number }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const restoreMut = useMutation({
    mutationFn: (uuid: string) => runsApi.restore(uuid, projectId),
    onSettled: () => refreshAfterRunChange(queryClient, projectId),
  });
  if (runs.length === 0) return null;
  return (
    <div className="mt-3 border-t border-gray-100 pt-2">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1 px-1 text-xs text-gray-400 hover:text-gray-600"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <Trash2 size={12} /> Removed runs ({runs.length})
      </button>
      {open && (
        <div className="mt-1 space-y-1">
          {runs.map((run) => (
            <div key={run.uuid} className="flex items-start gap-2 rounded-md px-2 py-1.5 hover:bg-gray-50" data-removed-run={run.label}>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-gray-600">{run.label}</p>
                <p className="text-[11px] text-gray-400 truncate">
                  {run.model ? `${run.model} · ` : ''}{countOf(run.codings_removed_with_run, 'coded segment')} come{run.codings_removed_with_run === 1 ? 's' : ''} back
                </p>
              </div>
              <button
                onClick={() => restoreMut.mutate(run.uuid)}
                disabled={restoreMut.isPending}
                className="flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-xs text-indigo-600 hover:bg-indigo-50 disabled:opacity-50"
                title="Restore this run and the coded segments removed with it"
              >
                <RotateCcw size={11} /> Restore
              </button>
            </div>
          ))}
          {restoreMut.error && <p className="px-2 text-[11px] text-red-600">{readableError(restoreMut.error)}</p>}
        </div>
      )}
    </div>
  );
}

/** The list of model runs in the AI panel: what each run did, with Stop, Remove, Restore. */
export function ModelRunsPanel({ projectId, onOpenDocument, onClose }: {
  projectId: number;
  onOpenDocument: (docId: number) => void;
  onClose: () => void;
}) {
  const { runs, loaded } = useModelRuns(projectId);
  const active = runs.filter((run) => !run.removed);
  const removed = runs.filter((run) => run.removed);

  return (
    <div className="p-2 flex flex-col h-full min-h-0" data-runs-panel>
      <div className="px-1 mb-2 flex items-center justify-between">
        <span className="text-xs font-medium text-gray-500 uppercase tracking-wide flex items-center gap-1.5">
          <Bot size={12} /> Model runs
        </span>
        <button onClick={onClose} className="text-[11px] text-purple-600 hover:text-purple-700 flex items-center gap-1">
          <ArrowLeft size={11} /> AI tools
        </button>
      </div>
      <div className="flex-1 overflow-y-auto min-h-0">
        {!loaded ? (
          <p className="text-xs text-gray-400 text-center py-6">Loading…</p>
        ) : active.length === 0 && removed.length > 0 ? (
          <p className="px-1 py-2 text-xs text-gray-500">All model runs of this project are removed; restore one below.</p>
        ) : active.length === 0 ? (
          <div className="px-1 py-2 text-xs leading-relaxed text-gray-500 space-y-2">
            <p>
              No model runs yet. A program on this computer can let a language model code
              passages through AQDA’s local interface. Every segment it codes is marked with its
              run, so you can read it, accept it, or remove it.
            </p>
            <p>
              An example program and instructions are in{' '}
              <a
                href="https://github.com/tseidl/aqda#letting-a-language-model-code"
                target="_blank"
                rel="noreferrer"
                className="text-indigo-600 hover:text-indigo-800 underline"
              >
                AQDA’s documentation
              </a>.
            </p>
          </div>
        ) : (
          <div className="space-y-1.5">
            {active.map((run) => (
              <RunCard key={run.uuid} run={run} projectId={projectId} onOpenDocument={onOpenDocument} />
            ))}
          </div>
        )}
        <RemovedRuns runs={removed} projectId={projectId} />
      </div>
    </div>
  );
}
