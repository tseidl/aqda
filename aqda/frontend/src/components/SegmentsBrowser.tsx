import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Trash2 } from 'lucide-react';
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

  const { data: allCodings = [] } = useQuery({
    queryKey: ['codings', 'project', projectId, filterCodeId],
    queryFn: () =>
      codingsApi.list(
        filterCodeId
          ? { code_id: filterCodeId, project_id: projectId }
          : { project_id: projectId }
      ),
  });

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

      {allCodings.length === 0 ? (
        <p className="text-sm text-gray-400 text-center py-8">
          {filterCodeId ? 'No segments with this code' : 'No coded segments yet'}
        </p>
      ) : (
        <div className="space-y-1">
          {allCodings.map((coding: Coding) => (
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
            {allCodings.length} segment{allCodings.length !== 1 ? 's' : ''}
          </p>
        </div>
      )}
    </div>
  );
}
