import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Loader2, Merge, X } from 'lucide-react';
import { codes as codesApi, type Code, type CodeMergeImpact, type CodeMergeResult } from '../api';
import { readableError } from './codingFields';

interface Props {
  projectId: number;
  /** The code to merge into another one. */
  source: Code;
  codes: Code[];
  selectedCodeId: number | null;
  onSelectCode: (id: number | null) => void;
  onClose: () => void;
}

// "3 coded segments", "1 coded segment".
const countOf = (n: number, noun: string) => `${n} ${noun}${n === 1 ? '' : 's'}`;
// "moves" for one, "move" for several.
const verb = (n: number, one: string, many: string) => (n === 1 ? one : many);

// The code and every code below it.
function subtreeIds(codes: Code[], rootId: number): Set<number> {
  const ids = new Set([rootId]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const code of codes) {
      if (code.parent_id != null && ids.has(code.parent_id) && !ids.has(code.id)) {
        ids.add(code.id);
        grew = true;
      }
    }
  }
  return ids;
}

// The names of a code's parents, from the top level down.
function parentPath(byId: Map<number, Code>, code: Code): string[] {
  const names: string[] = [];
  const seen = new Set<number>();
  let parent = code.parent_id != null ? byId.get(code.parent_id) : undefined;
  while (parent && !seen.has(parent.id)) {
    seen.add(parent.id);
    names.unshift(parent.name);
    parent = parent.parent_id != null ? byId.get(parent.parent_id) : undefined;
  }
  return names;
}

// What a merge does, as plain sentences for the confirmation.
function impactSentences(impact: CodeMergeImpact): string[] {
  const s = `“${impact.source.name}”`;
  const t = `“${impact.target.name}”`;
  const lines: string[] = [];
  const moved = impact.codings_moved;
  if (moved > 0) {
    lines.push(`${countOf(moved, 'coded segment')} ${verb(moved, 'moves', 'move')} to ${t}.`);
  } else if (impact.duplicates_dropped === 0) {
    lines.push(`${s} has no coded segments.`);
  }
  if (impact.duplicates_dropped > 0) {
    const d = impact.duplicates_dropped;
    lines.push(
      `${countOf(d, 'coded segment')} ${verb(d, 'is an exact duplicate', 'are exact duplicates')} of ` +
      `${verb(d, 'a coded segment', 'coded segments')} already under ${t} (same passage, coder or model run, and answers) ` +
      `and ${verb(d, 'is', 'are')} dropped.`,
    );
    if (impact.passage_memos_relinked > 0) {
      lines.push(
        `${countOf(impact.passage_memos_relinked, 'memo')} written on ${verb(d, 'it', 'them')} ` +
        `${verb(impact.passage_memos_relinked, 'stays', 'stay')}, attached to the matching coded segment under ${t}.`,
      );
    }
  }
  if (impact.trashed_codings_moved > 0) {
    lines.push(
      `${countOf(impact.trashed_codings_moved, 'removed coded segment')} (in the trash or of a removed ` +
      `model run) ${verb(impact.trashed_codings_moved, 'moves', 'move')} along, so restoring brings ` +
      `${verb(impact.trashed_codings_moved, 'it', 'them')} back under ${t}.`,
    );
  }
  if (impact.memos_moved > 0) {
    lines.push(`${countOf(impact.memos_moved, 'memo')} about ${s} ${verb(impact.memos_moved, 'moves', 'move')} to ${t}.`);
  }
  if (impact.children_moved > 0) {
    lines.push(`${countOf(impact.children_moved, 'child code')} ${verb(impact.children_moved, 'moves', 'move')} under ${t}.`);
  }
  if (impact.trashed_children_moved > 0) {
    lines.push(
      `${countOf(impact.trashed_children_moved, 'deleted child code')} in the code trash ` +
      `${verb(impact.trashed_children_moved, 'moves', 'move')} under ${t} as well.`,
    );
  }
  if (impact.description === 'carried_over') {
    lines.push(`${t} has no definition yet and takes over the definition of ${s}.`);
  } else if (impact.description === 'saved_as_memo') {
    lines.push(`Both codes have a definition: ${t} keeps its own, and the definition of ${s} is saved as a memo on ${t}.`);
  }
  return lines;
}

// What the merge did, from the numbers the server returned, as plain sentences.
function resultSentences(result: CodeMergeResult): string[] {
  const s = `“${result.source.name}”`;
  const t = `“${result.code.name}”`;
  const lines = [`${s} was merged into ${t}.`];
  const moved = result.codings_moved;
  if (moved > 0) lines.push(`${countOf(moved, 'coded segment')} moved to ${t}.`);
  if (result.duplicates_dropped > 0) {
    const d = result.duplicates_dropped;
    lines.push(`${countOf(d, 'exact duplicate')} of coded segments already under ${t} ${verb(d, 'was', 'were')} dropped.`);
  }
  if (result.passage_memos_relinked > 0) {
    lines.push(`${countOf(result.passage_memos_relinked, 'memo')} on dropped duplicates now ${verb(result.passage_memos_relinked, 'belongs', 'belong')} to the matching coded segment.`);
  }
  if (result.trashed_codings_moved > 0) {
    lines.push(`${countOf(result.trashed_codings_moved, 'removed coded segment')} moved along, so restoring brings ${verb(result.trashed_codings_moved, 'it', 'them')} back under ${t}.`);
  }
  if (result.memos_moved > 0) lines.push(`${countOf(result.memos_moved, 'memo')} about ${s} moved to ${t}.`);
  if (result.children_moved > 0) lines.push(`${countOf(result.children_moved, 'child code')} moved under ${t}.`);
  if (result.trashed_children_moved > 0) {
    lines.push(`${countOf(result.trashed_children_moved, 'deleted child code')} in the code trash moved under ${t} as well.`);
  }
  if (result.description === 'carried_over') lines.push(`${t} took over the definition of ${s}.`);
  if (result.description_memo_id != null) {
    lines.push(`Both codes had a definition, so the definition of ${s} was saved as a memo on ${t}.`);
  }
  return lines;
}

/** Merge a code into another one: choose the target, read what will happen, confirm. */
export function MergeCodeDialog({ projectId, source, codes, selectedCodeId, onSelectCode, onClose }: Props) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState('');
  const [target, setTarget] = useState<Code | null>(null);
  const [result, setResult] = useState<CodeMergeResult | null>(null);

  // Every active code except this one and the codes below it, with its parents' names.
  const candidates = useMemo(() => {
    const byId = new Map(codes.map((code) => [code.id, code]));
    const excluded = subtreeIds(codes, source.id);
    return codes
      .filter((code) => !excluded.has(code.id))
      .map((code) => ({ code, parents: parentPath(byId, code) }))
      .sort((a, b) => [...a.parents, a.code.name].join('\u0000').localeCompare([...b.parents, b.code.name].join('\u0000')));
  }, [codes, source.id]);
  const needle = filter.trim().toLowerCase();
  const shown = candidates.filter(({ code, parents }) =>
    !needle || [...parents, code.name].join(' ').toLowerCase().includes(needle));

  const mergeMut = useMutation({
    mutationFn: () => codesApi.merge(source.id, target!.id),
    onSuccess: (merged) => {
      setResult(merged);
      if (selectedCodeId === source.id) onSelectCode(merged.code.id);
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
      queryClient.invalidateQueries({ queryKey: ['codes-trash', projectId] });
      queryClient.invalidateQueries({ queryKey: ['codings'] });
      queryClient.invalidateQueries({ queryKey: ['memos', projectId] });
      queryClient.invalidateQueries({ queryKey: ['coding-fields', projectId] });
      queryClient.invalidateQueries({ queryKey: ['coding-field-suggestions'] });
      queryClient.invalidateQueries({ queryKey: ['model-runs', projectId] });
    },
  });

  // What the merge would change; refetched when the project changes meanwhile (ProjectView
  // invalidates 'merge-impact' on every new project revision), paused while merging.
  const impactQuery = useQuery({
    queryKey: ['merge-impact', source.id, target?.id],
    queryFn: () => codesApi.mergeImpact(source.id, target!.id),
    enabled: target !== null && result === null && !mergeMut.isPending,
    gcTime: 0,
    refetchOnWindowFocus: false,
  });

  const close = () => { if (!mergeMut.isPending) onClose(); };

  // Escape closes the dialog wherever the focus is (also after it left the search box).
  const pending = mergeMut.isPending;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !pending) onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pending, onClose]);

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-gray-950/35 p-4"
      onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}
      onClick={(e) => e.stopPropagation()}
      data-merge-dialog
    >
      <div className="w-full max-w-lg rounded-xl border border-gray-200 bg-white shadow-2xl">
        <div className="flex items-start gap-3 border-b border-gray-100 px-5 py-4">
          <div className="rounded-lg bg-indigo-50 p-2 text-indigo-700"><Merge size={20} /></div>
          <div className="min-w-0 flex-1">
            <h2 className="font-semibold text-gray-900 break-words">
              {result ? 'Codes merged' : target ? `Merge “${source.name}” into “${target.name}”?` : `Merge “${source.name}” into…`}
            </h2>
            {!target && (
              <p className="mt-1 text-xs text-gray-500">
                Choose the code that takes over its coded segments, memos, and child codes.
                “{source.name}” is deleted afterwards.
              </p>
            )}
          </div>
          <button
            onClick={close}
            disabled={mergeMut.isPending}
            className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 disabled:opacity-40"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {!target ? (
          <div className="px-5 py-3">
            <input
              autoFocus
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && shown[0]) setTarget(shown[0].code); }}
              placeholder="Search codes…"
              className="w-full px-2 py-1.5 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500"
              aria-label="Search codes"
            />
            <div className="mt-2 max-h-72 overflow-auto space-y-0.5">
              {shown.length === 0 ? (
                <p className="py-4 text-center text-sm text-gray-400">
                  {candidates.length === 0 ? 'There is no other code to merge into.' : 'No code matches.'}
                </p>
              ) : shown.map(({ code, parents }) => (
                <button
                  key={code.id}
                  onClick={() => setTarget(code)}
                  className="w-full text-left px-2 py-1.5 rounded text-sm hover:bg-indigo-50 flex items-center gap-2"
                >
                  <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: code.color }} />
                  <span className="truncate flex-1">
                    {parents.length > 0 && <span className="text-gray-400">{parents.join(' › ')} › </span>}
                    <span className="text-gray-800">{code.name}</span>
                  </span>
                  {code.coding_count ? <span className="text-xs text-gray-400 shrink-0">{code.coding_count}</span> : null}
                </button>
              ))}
            </div>
          </div>
        ) : result ? (
          <div className="px-5 py-4 space-y-2 text-sm text-gray-700">
            <ul className="space-y-1" data-merge-result>
              {resultSentences(result).map((line) => <li key={line}>{line}</li>)}
            </ul>
            <p className="text-xs text-gray-400 break-all">Backup taken before the merge: {result.backup}</p>
            <div className="pt-1 flex justify-end">
              <button onClick={onClose} className="px-3 py-1.5 text-sm rounded-md bg-indigo-600 text-white hover:bg-indigo-700">
                Close
              </button>
            </div>
          </div>
        ) : (
          <div className="px-5 py-4">
            {impactQuery.isPending ? (
              <p className="py-3 text-center text-sm text-gray-400 flex items-center justify-center gap-1.5">
                <Loader2 size={14} className="animate-spin" /> Checking what the merge would change…
              </p>
            ) : impactQuery.error ? (
              <p className="rounded-md border border-red-100 bg-red-50 p-2.5 text-sm text-red-800" role="alert">
                {readableError(impactQuery.error)}
              </p>
            ) : (
              <div className="space-y-3 text-sm text-gray-700">
                <ul className="list-disc pl-5 space-y-1" data-merge-impact>
                  {impactSentences(impactQuery.data).map((line) => <li key={line}>{line}</li>)}
                  <li>“{source.name}” is then deleted.</li>
                </ul>
                <p className="rounded-md bg-amber-50 border border-amber-100 px-3 py-2 text-xs text-amber-900">
                  AQDA makes a backup first. A merge cannot be undone inside AQDA.
                </p>
              </div>
            )}
            {mergeMut.error && (
              <p className="mt-2 text-sm text-red-600" role="alert">{readableError(mergeMut.error)}</p>
            )}
            <div className="mt-4 flex items-center gap-2">
              <button
                onClick={() => { setTarget(null); mergeMut.reset(); }}
                disabled={mergeMut.isPending}
                className="flex items-center gap-1 px-2 py-1.5 text-sm rounded-md text-gray-600 hover:bg-gray-100 disabled:opacity-50"
              >
                <ArrowLeft size={14} /> Choose another code
              </button>
              <div className="flex-1" />
              <button
                onClick={close}
                disabled={mergeMut.isPending}
                className="px-3 py-1.5 text-sm rounded-md bg-gray-100 text-gray-600 hover:bg-gray-200 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={() => mergeMut.mutate()}
                disabled={!impactQuery.data || mergeMut.isPending}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                {mergeMut.isPending && <Loader2 size={14} className="animate-spin" />}
                Merge
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
