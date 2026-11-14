import { useCallback, useState, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Trash2 } from 'lucide-react';
import { codings as codingsApi, type Code, type Coding } from '../api';
import { CodingAnswers } from './CodingFieldsForm';
import { useCodingFields } from './codingFields';
import { CoderLabel } from './CoderLabel';
import { NO_CODER_NAME, useModelRuns } from './coderLayers';

interface Props {
  projectId: number;
  codes: Code[];
  onNavigate: (docId: number, startPos?: number, endPos?: number) => void;
}

export function SegmentsBrowser({ projectId, codes, onNavigate }: Props) {
  const queryClient = useQueryClient();

  const deleteCodingMut = useMutation({
    mutationFn: codingsApi.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['codings'] });
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
    },
  });
  const [filterCodeId, setFilterCodeId] = useState<number | null>(null);
  const [filterCoder, setFilterCoder] = useState<string>('');
  // The project's coding fields, for showing and editing each segment's answers.
  const codingFields = useCodingFields(projectId);

  const { data: allCodings = [] } = useQuery({
    queryKey: ['codings', 'project', projectId, filterCodeId],
    queryFn: () =>
      codingsApi.list(
        filterCodeId
          ? { code_id: filterCodeId, project_id: projectId }
          : { project_id: projectId }
      ),
  });

  // Model runs by record ID, for keying the coder filter by run uuid (a collaborator's
  // update re-creates record IDs, while uuids stay).
  const { runs: runList, loaded: runsLoaded } = useModelRuns(projectId);
  // The filter key of a model coding's run; a run not (yet) in the list gets a key by ID.
  const runKeyOf = useCallback((runId: number) => {
    const run = runList.find((r) => r.id === runId);
    return run ? `run:${run.uuid}` : `run-id:${runId}`;
  }, [runList]);

  // Who coded the (code-filtered) codings, for the coder dropdown: hand coders by name
  // ("hand:<name>") and model runs by their run ("run:<uuid>"), never by the label text.
  const coderOptions = useMemo(() => {
    const hand = new Set<string>();
    const runLabels = new Map<number, string>();
    for (const c of allCodings) {
      if (c.run_id == null) hand.add(c.coder ?? '');
      else runLabels.set(c.run_id, c.run_label ?? c.coder ?? 'Model run');
    }
    const named = [...hand].filter(Boolean).sort();
    const handOptions = named.map((name) => ({ value: `hand:${name}`, label: name }));
    // Codings without a coder name are a choice only next to other coders.
    if (hand.has('') && (named.length > 0 || runLabels.size > 0)) {
      handOptions.push({ value: 'hand:', label: NO_CODER_NAME });
    }
    const runOptions = [...runLabels.entries()]
      .sort((a, b) => b[0] - a[0])
      .map(([id, label]) => ({ value: runKeyOf(id), label }));
    return { hand: handOptions, runs: runOptions };
  }, [allCodings, runKeyOf]);

  // A chosen run that no longer exists (removed, or no longer in the project) is dropped,
  // so the dropdown shows "All coders" and the choice does not come back by itself.
  if (filterCoder.startsWith('run') && runsLoaded &&
      !runList.some((run) => !run.removed && `run:${run.uuid}` === filterCoder)) {
    setFilterCoder('');
  }

  // Ignore a stale coder selection that no longer appears under the current code
  // filter, so switching codes never leaves the list silently empty.
  const effectiveCoder = filterCoder &&
    [...coderOptions.hand, ...coderOptions.runs].some((o) => o.value === filterCoder) ? filterCoder : '';

  // Coder filtering is done client-side over the already-loaded codings.
  const shownCodings = useMemo(() => {
    if (!effectiveCoder) return allCodings;
    if (effectiveCoder.startsWith('run')) {
      return allCodings.filter((c: Coding) => c.run_id != null && runKeyOf(c.run_id) === effectiveCoder);
    }
    const name = effectiveCoder.slice(5);
    return allCodings.filter((c: Coding) => c.run_id == null && (c.coder ?? '') === name);
  }, [allCodings, effectiveCoder, runKeyOf]);

  return (
    <div className="p-2">
      <div className="px-1 mb-2">
        <span className="text-xs font-medium text-gray-500 uppercase tracking-wide">
          Coded Segments
        </span>
      </div>

      {/* Filter by code */}
      <select
        value={filterCodeId ?? ''}
        onChange={(e) => setFilterCodeId(e.target.value ? Number(e.target.value) : null)}
        className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md mb-2 focus:outline-none focus:ring-1 focus:ring-indigo-500"
      >
        <option value="">All codes</option>
        {codes.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name} ({c.coding_count ?? 0})
          </option>
        ))}
      </select>

      {/* Filter by coder or model run — shown only when codings carry attribution */}
      {coderOptions.hand.length + coderOptions.runs.length > 0 && (
        <select
          value={effectiveCoder}
          onChange={(e) => setFilterCoder(e.target.value)}
          className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md mb-2 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        >
          <option value="">All coders</option>
          {coderOptions.runs.length === 0 ? (
            coderOptions.hand.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)
          ) : (<>
            {coderOptions.hand.length > 0 && (
              <optgroup label="Coded by hand">
                {coderOptions.hand.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </optgroup>
            )}
            <optgroup label="Model runs">
              {coderOptions.runs.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </optgroup>
          </>)}
        </select>
      )}

      {shownCodings.length === 0 ? (
        <p className="text-sm text-gray-400 text-center py-8">
          {filterCodeId || effectiveCoder ? 'No matching segments' : 'No coded segments yet'}
        </p>
      ) : (
        <div className="space-y-1">
          {shownCodings.map((coding: Coding) => (
            <div
              key={coding.id}
              className="p-2 rounded-md hover:bg-gray-50 border border-gray-100 group"
            >
              <div className="flex items-center gap-2 mb-1 min-w-0">
                <span
                  className="w-2.5 h-2.5 rounded-sm shrink-0"
                  style={{ backgroundColor: coding.code_color }}
                />
                <span className="text-xs font-medium text-gray-600 truncate min-w-[4rem]">
                  {coding.code_name}
                </span>
                <span className={`text-xs text-gray-400 truncate max-w-[40%] ${coding.run_id == null ? 'shrink-0' : 'min-w-0'}`}>
                  {coding.document_name}
                </span>
                <CoderLabel coding={coding} />
                {coding.repair_status?.startsWith('review_') && (
                  <span
                    className="text-amber-600 shrink-0"
                    title="AQDA repaired this older segment to the nearest matching text. Please verify it."
                  >
                    <AlertTriangle size={12} />
                  </span>
                )}
                <button
                  onClick={(e) => { e.stopPropagation(); deleteCodingMut.mutate(coding.id); }}
                  className="p-0.5 text-gray-300 hover:text-red-500 shrink-0"
                  title="Remove coding"
                >
                  <Trash2 size={12} />
                </button>
              </div>
              <button
                onClick={() => onNavigate(coding.document_id, coding.start_pos, coding.end_pos)}
                className="w-full text-left"
              >
                <p className="text-xs text-gray-600 leading-relaxed cursor-pointer hover:text-gray-800">
                  &ldquo;{coding.selected_text.length > 120
                    ? coding.selected_text.slice(0, 120) + '...'
                    : coding.selected_text}&rdquo;
                </p>
              </button>
              {/* Field answers (none in a project without coding fields) */}
              <CodingAnswers coding={coding} fields={codingFields} projectId={projectId} />
            </div>
          ))}
          <p className="text-xs text-gray-400 text-center py-1">
            {shownCodings.length} segment{shownCodings.length !== 1 ? 's' : ''}
          </p>
        </div>
      )}
    </div>
  );
}
