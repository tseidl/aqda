import { useState, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2, StickyNote, FileText, Pencil } from 'lucide-react';
import { memos as memosApi, type Memo, type Code } from '../api';
import { MentionTextarea, MentionView } from './MentionTextarea';
import { buildMentionCandidates, type MentionCandidate } from './mentions';

interface Props {
  projectId: number;
  codes: Code[];
  selectedMemoId: number | null;
  onSelectMemo: (id: number | null) => void;
  onJumpToMention: (c: MentionCandidate) => void;
  selectedDocId?: number | null;
  selectedDocName?: string;
  onNavigate?: (docId: number, startPos?: number, endPos?: number) => void;
}

/** Memo detail editor: clickable read view + click-to-edit, with @-mention references. */
function MemoDetail({ memo, codes, memos, projectId, onBack, onNavigate, onJumpToMention }: {
  memo: Memo;
  codes: Code[];
  memos: Memo[];
  projectId: number;
  onBack: () => void;
  onNavigate?: (docId: number, startPos?: number, endPos?: number) => void;
  onJumpToMention: (c: MentionCandidate) => void;
}) {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState(memo.title);
  const [content, setContent] = useState(memo.content);
  const [editing, setEditing] = useState(() => !(memo.content ?? '').trim());
  const saveTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  // State is seeded from the memo on mount; MemoPanel remounts this via key on memo switch.

  const updateMut = useMutation({
    mutationFn: (data: { title?: string; content?: string }) => memosApi.update(memo.id, data),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memos', projectId] }),
  });
  const deleteMut = useMutation({
    mutationFn: () => memosApi.delete(memo.id),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['memos', projectId] }); onBack(); },
  });

  const scheduleSave = (t: string, c: string) => {
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => updateMut.mutate({ title: t, content: c }), 500);
  };

  const candidates = buildMentionCandidates(codes, memos, memo.id);

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-gray-100 flex items-center gap-2">
        <button onClick={onBack} className="text-xs text-gray-500 hover:text-gray-700">&larr; Back</button>
        <button
          onClick={() => { if (confirm('Delete this memo?')) deleteMut.mutate(); }}
          className="ml-auto p-1 text-gray-400 hover:text-red-500"
        >
          <Trash2 size={13} />
        </button>
      </div>
      {memo.document_id && memo.document_name && (
        <button
          onClick={() => onNavigate?.(memo.document_id!, memo.start_pos ?? undefined, memo.end_pos ?? undefined)}
          className="mx-3 mt-2 flex items-center gap-1.5 text-xs text-indigo-600 hover:text-indigo-800 hover:underline"
          title="Open the linked passage"
        >
          <FileText size={12} className="shrink-0" />
          <span className="truncate">Go to {memo.document_name}</span>
        </button>
      )}
      <input
        value={title}
        onChange={(e) => { setTitle(e.target.value); scheduleSave(e.target.value, content); }}
        className="px-3 py-2 text-sm font-medium border-b border-gray-100 focus:outline-none"
        placeholder="Memo title"
      />
      {editing ? (
        <MentionTextarea
          fill
          autoFocus
          className="flex-1"
          textClassName="px-3 py-2 text-sm"
          value={content}
          onChange={(v) => { setContent(v); scheduleSave(title, v); }}
          candidates={candidates}
          placeholder="Write your memo here… (type @ to reference a code or memo)"
          onBlur={() => setEditing(false)}
        />
      ) : (
        <MentionView
          value={content}
          candidates={candidates}
          onJump={onJumpToMention}
          onEdit={() => setEditing(true)}
          className="flex-1 overflow-auto px-3 py-2 text-sm text-gray-800"
          placeholder="Write your memo here… (type @ to reference a code or memo)"
        />
      )}
    </div>
  );
}

export function MemoPanel({
  projectId, codes, selectedMemoId, onSelectMemo, onJumpToMention,
  selectedDocId, selectedDocName, onNavigate,
}: Props) {
  const queryClient = useQueryClient();
  const [showNew, setShowNew] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [attachToDoc, setAttachToDoc] = useState(true);

  const { data: memoList = [] } = useQuery({
    queryKey: ['memos', projectId],
    queryFn: () => memosApi.list({ project_id: projectId }),
  });

  const createMut = useMutation({
    mutationFn: (vars: { title: string; document_id: number | null }) =>
      memosApi.create({
        project_id: projectId,
        title: vars.title,
        content: '',
        document_id: vars.document_id ?? undefined,
      }),
    onSuccess: (memo) => {
      queryClient.invalidateQueries({ queryKey: ['memos', projectId] });
      onSelectMemo(memo.id);
      setShowNew(false);
      setNewTitle('');
    },
  });

  const deleteMut = useMutation({
    mutationFn: memosApi.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memos', projectId] });
      onSelectMemo(null);
    },
  });

  const selectedMemo = memoList.find((m: Memo) => m.id === selectedMemoId);

  const submitNew = () => {
    if (!newTitle.trim()) return;
    createMut.mutate({
      title: newTitle.trim(),
      document_id: attachToDoc && selectedDocId ? selectedDocId : null,
    });
  };

  if (selectedMemo) {
    return (
      <MemoDetail
        key={selectedMemo.id}
        memo={selectedMemo}
        codes={codes}
        memos={memoList}
        projectId={projectId}
        onBack={() => onSelectMemo(null)}
        onNavigate={onNavigate}
        onJumpToMention={onJumpToMention}
      />
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
              if (e.key === 'Enter') submitNew();
              if (e.key === 'Escape') { setShowNew(false); setNewTitle(''); }
            }}
          />
          {selectedDocId && (
            <label className="flex items-center gap-1.5 mt-1.5 px-1 text-xs text-gray-500 cursor-pointer">
              <input
                type="checkbox"
                checked={attachToDoc}
                onChange={(e) => setAttachToDoc(e.target.checked)}
                className="w-3.5 h-3.5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
              />
              <span className="truncate">Link to “{selectedDocName ?? 'current document'}”</span>
            </label>
          )}
        </div>
      )}

      {memoList.length === 0 && !showNew ? (
        <p className="text-sm text-gray-400 text-center py-8">No memos yet</p>
      ) : (
        <div className="space-y-0.5">
          {memoList.map((memo: Memo) => (
            <div key={memo.id} className="group flex items-start rounded-md hover:bg-gray-50">
              <button
                onClick={() => onSelectMemo(memo.id)}
                className="flex-1 min-w-0 text-left px-3 py-2"
              >
                <div className="flex items-center gap-2">
                  <StickyNote size={13} className="text-amber-500 shrink-0" />
                  <span className="text-sm text-gray-700 truncate">
                    {memo.title || 'Untitled memo'}
                  </span>
                </div>
                {memo.document_name && (
                  <span className="ml-5 mt-0.5 flex items-center gap-1 text-[10px] text-indigo-500">
                    <FileText size={10} className="shrink-0" />
                    <span className="truncate">{memo.document_name}</span>
                  </span>
                )}
                {memo.content && (
                  <p className="text-xs text-gray-400 mt-0.5 truncate ml-5">
                    {memo.content.slice(0, 60)}
                  </p>
                )}
              </button>
              <div className="hidden group-hover:flex items-center gap-0.5 pr-2 pt-2 shrink-0">
                <button
                  onClick={() => onSelectMemo(memo.id)}
                  className="p-0.5 text-gray-400 hover:text-gray-600"
                  title="Edit memo"
                >
                  <Pencil size={12} />
                </button>
                <button
                  onClick={() => { if (confirm('Delete this memo?')) deleteMut.mutate(memo.id); }}
                  className="p-0.5 text-gray-400 hover:text-red-500"
                  title="Delete memo"
                >
                  <Trash2 size={12} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
