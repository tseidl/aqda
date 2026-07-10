import { useState, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Trash2 } from 'lucide-react';
import { codings as codingsApi, type Code, type Coding } from '../api';

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

  const { data: allCodings = [] } = useQuery({
    queryKey: ['codings', 'project', projectId, filterCodeId],
    queryFn: () =>
      codingsApi.list(
        filterCodeId
          ? { code_id: filterCodeId, project_id: projectId }
          : { project_id: projectId }
      ),
  });

  // Distinct coder names present in the (code-filtered) codings, for the coder dropdown.
  const coders = useMemo(
    () => Array.from(new Set(allCodings.map((c: Coding) => c.coder).filter(Boolean) as string[])).sort(),
    [allCodings]
  );

  // Ignore a stale coder selection that no longer appears under the current code
  // filter, so switching codes never leaves the list silently empty.
  const effectiveCoder = filterCoder && coders.includes(filterCoder) ? filterCoder : '';

  // Coder filtering is done client-side over the already-loaded codings.
  const shownCodings = useMemo(
    () => (effectiveCoder ? allCodings.filter((c: Coding) => c.coder === effectiveCoder) : allCodings),
    [allCodings, effectiveCoder]
  );

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

      {/* Filter by coder — shown only when codings carry coder attribution */}
      {coders.length > 0 && (
        <select
          value={effectiveCoder}
          onChange={(e) => setFilterCoder(e.target.value)}
          className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md mb-2 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        >
          <option value="">All coders</option>
          {coders.map((name) => (
            <option key={name} value={name}>{name}</option>
          ))}
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
                <span className="text-xs font-medium text-gray-600 truncate">
                  {coding.code_name}
                </span>
                <span className="text-xs text-gray-400 truncate shrink-0 max-w-[40%]">
                  {coding.document_name}
                </span>
                {coding.coder && (
                  <span className="text-[10px] text-gray-400 shrink-0" title="Coder">{coding.coder}</span>
                )}
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
