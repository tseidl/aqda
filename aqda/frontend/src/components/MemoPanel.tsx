import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2, StickyNote } from 'lucide-react';
import { memos as memosApi, type Memo } from '../api';

interface Props {
  projectId: number;
}

export function MemoPanel({ projectId }: Props) {
  const queryClient = useQueryClient();
  const [selectedMemoId, setSelectedMemoId] = useState<number | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [newTitle, setNewTitle] = useState('');

  const { data: memoList = [] } = useQuery({
    queryKey: ['memos', projectId],
    queryFn: () => memosApi.list({ project_id: projectId }),
  });

  const createMut = useMutation({
    mutationFn: (title: string) =>
      memosApi.create({ project_id: projectId, title, content: '' }),
    onSuccess: (memo) => {
      queryClient.invalidateQueries({ queryKey: ['memos', projectId] });
      setSelectedMemoId(memo.id);
      setShowNew(false);
      setNewTitle('');
    },
  });

  const updateMut = useMutation({
    mutationFn: ({ id, ...data }: { id: number; title?: string; content?: string }) =>
      memosApi.update(id, data),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memos', projectId] }),
  });

  const deleteMut = useMutation({
    mutationFn: memosApi.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memos', projectId] });
      setSelectedMemoId(null);
    },
  });

  const selectedMemo = memoList.find((m: Memo) => m.id === selectedMemoId);

  if (selectedMemo) {
    return (
      <div className="flex flex-col h-full">
        <div className="px-3 py-2 border-b border-gray-100 flex items-center gap-2">
          <button
            onClick={() => setSelectedMemoId(null)}
            className="text-xs text-gray-500 hover:text-gray-700"
          >
            &larr; Back
          </button>
          <button
            onClick={() => {
              if (confirm('Delete this memo?')) deleteMut.mutate(selectedMemo.id);
            }}
            className="ml-auto p-1 text-gray-400 hover:text-red-500"
          >
            <Trash2 size={13} />
          </button>
        </div>
        <input
          value={selectedMemo.title}
          onChange={(e) =>
            updateMut.mutate({ id: selectedMemo.id, title: e.target.value })
          }
          className="px-3 py-2 text-sm font-medium border-b border-gray-100 focus:outline-none"
          placeholder="Memo title"
        />
        <textarea
          value={selectedMemo.content}
          onChange={(e) =>
            updateMut.mutate({ id: selectedMemo.id, content: e.target.value })
          }
          className="flex-1 px-3 py-2 text-sm resize-none focus:outline-none"
          placeholder="Write your memo here..."
        />
      </div>
    );
  }

  return (
    <div className="p-2">
      <div className="px-1 mb-2 flex items-center justify-between">
        <span className="text-xs font-medium text-gray-500 uppercase tracking-wide">Memos</span>
        <button
          onClick={() => setShowNew(true)}
          className="p-1 text-gray-400 hover:text-indigo-600 rounded hover:bg-gray-100"
        >
          <Plus size={14} />
        </button>
      </div>

      {showNew && (
        <div className="mb-2">
          <input
            autoFocus
            placeholder="Memo title"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            className="w-full px-2 py-1.5 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && newTitle.trim()) createMut.mutate(newTitle.trim());
              if (e.key === 'Escape') { setShowNew(false); setNewTitle(''); }
            }}
          />
        </div>
      )}

      {memoList.length === 0 && !showNew ? (
        <p className="text-sm text-gray-400 text-center py-8">No memos yet</p>
      ) : (
        <div className="space-y-0.5">
          {memoList.map((memo: Memo) => (
            <button
              key={memo.id}
              onClick={() => setSelectedMemoId(memo.id)}
              className="w-full text-left px-3 py-2 rounded-md hover:bg-gray-50 group"
            >
              <div className="flex items-center gap-2">
                <StickyNote size={13} className="text-amber-500 shrink-0" />
                <span className="text-sm text-gray-700 truncate">
                  {memo.title || 'Untitled memo'}
                </span>
              </div>
              {memo.content && (
                <p className="text-xs text-gray-400 mt-0.5 truncate ml-5">
                  {memo.content.slice(0, 60)}
                </p>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
