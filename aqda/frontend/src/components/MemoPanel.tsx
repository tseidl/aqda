import { useState, useRef, useEffect, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2, StickyNote, FileText, Pencil, AlertTriangle } from 'lucide-react';
import { memos as memosApi, type Memo, type Code } from '../api';
import { MentionTextarea, MentionView } from './MentionTextarea';
import { buildMentionCandidates, type MentionCandidate } from './mentions';
import { dropMemoDraft, keepMemoDraft, memoDraftKey, queueMemoRequest, useMemoDrafts } from './memoDrafts';

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
  const [saveFailed, setSaveFailed] = useState(false);
  // Whether a kept edit for this memo came from this editor (then the editor already shows it).
  const [ownsDraft, setOwnsDraft] = useState(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  // The edit waiting for its debounced save, if any.
  const pendingSave = useRef<{ title: string; content: string } | null>(null);
  const mounted = useRef(true);
  // The memo as last loaded from the server, for naming a kept edit.
  const latestMemo = useRef(memo);
  // State is seeded from the memo on mount; MemoPanel remounts this via key on memo switch.

  useEffect(() => { latestMemo.current = memo; }, [memo]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  // An edit rejected before this editor opened (e.g. before a collaborator's version replaced
  // this memo). Edits rejected while this editor is open are still on screen.
  const keptDraft = useMemoDrafts(projectId).find((draft) => draft.key === memoDraftKey(memo));
  const draftDiffers = Boolean(
    keptDraft && !ownsDraft && (keptDraft.title !== title || keptDraft.content !== content)
  );

  // Queue one save behind every earlier memo request; a rejected save is kept as a draft.
  const save = useCallback((edit: { title: string; content: string }) => {
    queueMemoRequest(() => memosApi.update(memo.id, edit))
      .then(
        () => {
          dropMemoDraft(memoDraftKey(latestMemo.current));
          if (mounted.current) setSaveFailed(false);
          queryClient.invalidateQueries({ queryKey: ['memos', projectId] });
        },
        () => {
          keepMemoDraft({ key: memoDraftKey(latestMemo.current), projectId, ...edit });
          if (mounted.current) {
            setSaveFailed(true);
            setOwnsDraft(true);
          }
        },
      );
  }, [memo.id, projectId, queryClient]);

  const deleteMut = useMutation({
    mutationFn: () => {
      clearTimeout(saveTimer.current);
      return queueMemoRequest(() => memosApi.delete(memo.id));
    },
    onSuccess: () => {
      pendingSave.current = null;
      dropMemoDraft(memoDraftKey(latestMemo.current));
      queryClient.invalidateQueries({ queryKey: ['memos', projectId] });
      onBack();
    },
    onError: (error: Error) => {
      alert(`The memo could not be deleted. ${error.message.replace(/^\d{3}:\s*/, '')}`);
      const pending = pendingSave.current;
      pendingSave.current = null;
      if (pending) save(pending);
    },
  });

  const scheduleSave = (t: string, c: string) => {
    clearTimeout(saveTimer.current);
    pendingSave.current = { title: t, content: c };
    saveTimer.current = setTimeout(() => {
      const edit = pendingSave.current;
      pendingSave.current = null;
      if (edit) save(edit);
    }, 500);
  };

  // Put a kept edit back into the editor and save it.
  const restoreDraft = () => {
    if (!keptDraft) return;
    setOwnsDraft(true);
    setTitle(keptDraft.title);
    setContent(keptDraft.content);
    clearTimeout(saveTimer.current);
    pendingSave.current = null;
    save({ title: keptDraft.title, content: keptDraft.content });
  };

  // Leaving the memo (Back, another memo) sends a save that is still waiting instead of dropping it.
  useEffect(() => () => {
    clearTimeout(saveTimer.current);
    const pending = pendingSave.current;
    pendingSave.current = null;
    if (pending) save(pending);
  }, [save]);

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
      {saveFailed && !draftDiffers && (
        <p className="mx-3 mt-2 text-xs text-red-600">
          This memo could not be saved. AQDA keeps your edit and lists it under Memos until it
          is saved or you discard it.
        </p>
      )}
      {draftDiffers && (
        <div className="mx-3 mt-2 rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">
          <p>An edit you made could not be saved, probably because the memo was updated from the shared folder.</p>
          <p className="mt-1 italic text-amber-800">
            {keptDraft && (keptDraft.title !== title ? `“${keptDraft.title}”: ` : '')}
            {keptDraft && (keptDraft.content.length > 160 ? `${keptDraft.content.slice(0, 160)}…` : keptDraft.content)}
          </p>
          <p className="mt-1">Restoring replaces the text shown below with your edit.</p>
          <div className="mt-1.5 flex gap-2">
            <button onClick={restoreDraft} className="font-medium underline hover:text-amber-950">
              Restore my edit
            </button>
            <button onClick={() => keptDraft && dropMemoDraft(keptDraft.key)} className="underline hover:text-amber-950">
              Discard it
            </button>
          </div>
        </div>
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

  // Deleting from the list waits for earlier memo saves and forgets the memo's kept edit.
  const deleteMut = useMutation({
    mutationFn: (memo: Memo) => queueMemoRequest(() => memosApi.delete(memo.id)),
    onSuccess: (_, memo) => {
      dropMemoDraft(memoDraftKey(memo));
      queryClient.invalidateQueries({ queryKey: ['memos', projectId] });
      onSelectMemo(null);
    },
    onError: (error: Error) => alert(`The memo could not be deleted. ${error.message.replace(/^\d{3}:\s*/, '')}`),
  });

  const selectedMemo = memoList.find((m: Memo) => m.id === selectedMemoId);
  const keptDrafts = useMemoDrafts(projectId);

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

      {keptDrafts.map((draft) => {
        const memo = memoList.find((m: Memo) => memoDraftKey(m) === draft.key);
        return (
          <div key={draft.key} className="mb-2 rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">
            <p className="flex items-start gap-1.5">
              <AlertTriangle size={13} className="mt-0.5 shrink-0" />
              <span>An edit to “{draft.title || 'Untitled memo'}” could not be saved.</span>
            </p>
            <div className="mt-1.5 flex gap-2 pl-5">
              {memo ? (
                <button onClick={() => onSelectMemo(memo.id)} className="font-medium underline">
                  Open the memo
                </button>
              ) : (
                <button
                  onClick={() => navigator.clipboard.writeText(`${draft.title}\n\n${draft.content}`)}
                  className="font-medium underline"
                >
                  Copy the text
                </button>
              )}
              <button onClick={() => dropMemoDraft(draft.key)} className="underline">Discard it</button>
            </div>
          </div>
        );
      })}

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
                  onClick={() => { if (confirm('Delete this memo?')) deleteMut.mutate(memo); }}
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
