import { useState, useMemo, useRef, useEffect, type DragEvent } from 'react';
import { useMutation, useQueryClient, useQuery } from '@tanstack/react-query';
import { ChevronRight, ChevronDown, Plus, Trash2, Pencil, Sparkles, Copy, GripVertical } from 'lucide-react';
import { codes as codesApi, settings as settingsApi, ai, type Code, type Memo } from '../api';
import { MentionTextarea, MentionView } from './MentionTextarea';
import { buildMentionCandidates, type MentionCandidate } from './mentions';

interface Props {
  projectId: number;
  codes: Code[];
  memos: Memo[];
  selectedCodeId: number | null;
  onSelectCode: (id: number | null) => void;
  onJumpToMention: (c: MentionCandidate) => void;
}

const COLOR_SCHEMES: Record<string, string[]> = {
  Default: ['#6366f1','#ec4899','#f59e0b','#10b981','#3b82f6','#8b5cf6','#ef4444','#14b8a6','#f97316','#06b6d4'],
  Pastel: ['#a5b4fc','#f9a8d4','#fcd34d','#6ee7b7','#93c5fd','#c4b5fd','#fca5a5','#5eead4','#fdba74','#67e8f9'],
  Earthy: ['#92400e','#78350f','#365314','#1e3a5f','#4c1d95','#831843','#6b7280','#b45309','#166534','#1e40af'],
  Vivid: ['#dc2626','#2563eb','#16a34a','#d97706','#9333ea','#0891b2','#e11d48','#4f46e5','#059669','#ea580c'],
};

function buildTree(codes: Code[]): Code[] {
  const map = new Map<number, Code & { children: Code[] }>();
  const roots: (Code & { children: Code[] })[] = [];

  for (const c of codes) {
    map.set(c.id, { ...c, children: [] });
  }
  for (const c of codes) {
    const node = map.get(c.id)!;
    if (c.parent_id && map.has(c.parent_id)) {
      map.get(c.parent_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  return roots;
}

type DropPos = 'before' | 'after' | 'inside';

type DndCtx = {
  draggingId: number | null;
  dropTarget: { id: number; pos: DropPos } | null;
  onDragStart: (id: number) => void;
  onDragOver: (id: number, pos: DropPos) => void;
  onDrop: (id: number, pos: DropPos) => void;
  onDragEnd: () => void;
};

/** True if nodeId is ancestorId itself or sits within ancestorId's subtree. */
function isInSubtree(codes: Code[], nodeId: number, ancestorId: number): boolean {
  const byId = new Map(codes.map((c) => [c.id, c]));
  let cur: number | null = nodeId;
  while (cur != null) {
    if (cur === ancestorId) return true;
    cur = byId.get(cur)?.parent_id ?? null;
  }
  return false;
}

/** Read the drop position (before/inside/after) from the pointer's place in a row. */
function dropPosFromEvent(e: DragEvent<HTMLElement>): DropPos {
  const rect = e.currentTarget.getBoundingClientRect();
  const y = e.clientY - rect.top;
  if (y < rect.height * 0.3) return 'before';
  if (y > rect.height * 0.7) return 'after';
  return 'inside';
}

function CodeNode({
  code,
  depth,
  selectedCodeId,
  onSelectCode,
  projectId,
  colors,
  dnd,
}: {
  code: Code & { children?: Code[] };
  depth: number;
  selectedCodeId: number | null;
  onSelectCode: (id: number | null) => void;
  projectId: number;
  colors: string[];
  dnd: DndCtx;
}) {
  const [expanded, setExpanded] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(code.name);
  const [showColorPicker, setShowColorPicker] = useState(false);
  const queryClient = useQueryClient();

  const updateMut = useMutation({
    mutationFn: (data: Partial<Code>) => codesApi.update(code.id, data),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['codes', projectId] }),
  });

  const deleteMut = useMutation({
    mutationFn: () => codesApi.delete(code.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
      if (selectedCodeId === code.id) onSelectCode(null);
    },
  });

  const confirmDelete = async () => {
    try {
      const impact = await codesApi.deleteImpact(code.id);
      const details = impact.child_count > 0
        ? `\n\nThis also moves ${impact.child_count} child code${impact.child_count === 1 ? '' : 's'} and ${impact.coding_count} coded segment${impact.coding_count === 1 ? '' : 's'} to the code trash.`
        : `\n\n${impact.coding_count} coded segment${impact.coding_count === 1 ? '' : 's'} will move to the code trash.`;
      if (confirm(`Move code "${code.name}" to trash?${details}\n\nRestoring this code will restore exactly this deletion.`)) {
        deleteMut.mutate();
      }
    } catch (error) {
      alert(error instanceof Error ? error.message : 'Could not inspect this code.');
    }
  };

  const hasChildren = (code.children?.length ?? 0) > 0;
  const isSelected = selectedCodeId === code.id;
  const isDragging = dnd.draggingId === code.id;
  const isDropTarget = dnd.dropTarget?.id === code.id;
  const dropPos = isDropTarget ? dnd.dropTarget!.pos : null;

  return (
    <div>
      <div
        draggable={!editing}
        onDragStart={(e) => {
          e.dataTransfer.effectAllowed = 'move';
          dnd.onDragStart(code.id);
        }}
        onDragOver={(e) => {
          if (dnd.draggingId == null || dnd.draggingId === code.id) return;
          e.preventDefault();
          dnd.onDragOver(code.id, dropPosFromEvent(e));
        }}
        onDrop={(e) => {
          if (dnd.draggingId == null) return;
          e.preventDefault();
          e.stopPropagation();
          dnd.onDrop(code.id, dropPosFromEvent(e));
        }}
        onDragEnd={dnd.onDragEnd}
        className={`flex items-center gap-1 px-2 py-1 cursor-pointer rounded-md mx-1 group ${
          isSelected ? 'bg-indigo-50' : 'hover:bg-gray-50'
        } ${isDragging ? 'opacity-40' : ''} ${
          isDropTarget && dropPos === 'inside' ? 'ring-2 ring-indigo-400 ring-inset' : ''
        }`}
        style={{
          paddingLeft: `${depth * 16 + 8}px`,
          ...(isDropTarget && dropPos === 'before' ? { boxShadow: 'inset 0 2px 0 0 #6366f1' } : {}),
          ...(isDropTarget && dropPos === 'after' ? { boxShadow: 'inset 0 -2px 0 0 #6366f1' } : {}),
        }}
        onClick={() => onSelectCode(isSelected ? null : code.id)}
      >
        <GripVertical
          size={12}
          className="shrink-0 text-gray-300 opacity-0 group-hover:opacity-100 cursor-grab"
        />
        {hasChildren ? (
          <button
            onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
            className="p-0.5 text-gray-400 hover:text-gray-600"
          >
            {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
        ) : (
          <span className="w-5" />
        )}

        <div className="relative">
          <span
            className="w-3 h-3 rounded-sm shrink-0 cursor-pointer block"
            style={{ backgroundColor: code.color }}
            onClick={(e) => {
              e.stopPropagation();
              setShowColorPicker(!showColorPicker);
            }}
          />
          {showColorPicker && (
            <div
              className="absolute left-0 top-5 z-50 bg-white rounded-lg shadow-xl border border-gray-200 p-2 min-w-[120px]"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="grid grid-cols-5 gap-1">
                {colors.map((c) => (
                  <span
                    key={c}
                    className={`w-5 h-5 rounded-sm cursor-pointer hover:scale-110 transition-transform ${code.color === c ? 'ring-2 ring-offset-1 ring-indigo-500' : ''}`}
                    style={{ backgroundColor: c }}
                    onClick={() => {
                      updateMut.mutate({ color: c });
                      setShowColorPicker(false);
                    }}
                  />
                ))}
              </div>
            </div>
          )}
        </div>

        {editing ? (
          <input
            autoFocus
            value={editName}
            onChange={(e) => setEditName(e.target.value)}
            className="flex-1 px-1 py-0 text-sm border border-indigo-300 rounded focus:outline-none"
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && editName.trim()) {
                updateMut.mutate({ name: editName.trim() });
                setEditing(false);
              }
              if (e.key === 'Escape') {
                setEditing(false);
                setEditName(code.name);
              }
            }}
            onBlur={() => {
              if (editName.trim() && editName !== code.name) {
                updateMut.mutate({ name: editName.trim() });
              }
              setEditing(false);
            }}
          />
        ) : (
          <span className="flex-1 text-sm text-gray-700 truncate">{code.name}</span>
        )}

        <span className="text-xs text-gray-400">{code.coding_count || ''}</span>

        <div className="hidden group-hover:flex items-center gap-0.5">
          <button
            onClick={(e) => { e.stopPropagation(); setEditing(true); setEditName(code.name); }}
            className="p-0.5 text-gray-400 hover:text-gray-600"
          >
            <Pencil size={12} />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              confirmDelete();
            }}
            className="p-0.5 text-gray-400 hover:text-red-500"
          >
            <Trash2 size={12} />
          </button>
        </div>
      </div>

      {expanded && hasChildren && (
        <div>
          {(code.children ?? []).map((child) => (
            <CodeNode
              key={child.id}
              code={child as Code & { children?: Code[] }}
              depth={depth + 1}
              selectedCodeId={selectedCodeId}
              onSelectCode={onSelectCode}
              projectId={projectId}
              colors={colors}
              dnd={dnd}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** Inline editor for a code's description/definition. */
function CodeDescriptionEditor({ code, projectId, codes, memos, onJumpToMention }: {
  code: Code; projectId: number; codes: Code[]; memos: Memo[];
  onJumpToMention: (c: MentionCandidate) => void;
}) {
  const queryClient = useQueryClient();
  const [desc, setDesc] = useState(code.description);
  const [editing, setEditing] = useState(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const [summaryText, setSummaryText] = useState<string | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summarySegments, setSummarySegments] = useState(0);
  const [copied, setCopied] = useState(false);
  const [saveFailed, setSaveFailed] = useState(false);
  // confirmedRef: the description known to be stored on the server.
  // pendingRef: the latest local value not yet confirmed saved (debounced or in flight).
  const confirmedRef = useRef(code.description);
  const pendingRef = useRef<string | null>(null);

  const updateMut = useMutation({
    mutationFn: (description: string) => codesApi.update(code.id, { description }),
    onSuccess: (_, value) => {
      confirmedRef.current = value;
      if (pendingRef.current === value) pendingRef.current = null;
      setSaveFailed(false);
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
    },
    onError: () => setSaveFailed(true),
  });

  // Accept a change made elsewhere (another tab, a sync) only while the draft is
  // pristine, nothing is waiting to be saved, and the editor is not focused. A
  // refetch that merely echoes our own save changes nothing; a stale refetch while
  // a save is pending can no longer roll the draft back.
  useEffect(() => {
    if (editing || pendingRef.current !== null || desc !== confirmedRef.current) return;
    if (code.description !== confirmedRef.current) {
      confirmedRef.current = code.description;
      setDesc(code.description);
    }
  }, [code.description, desc, editing]);

  const handleChange = (value: string) => {
    setDesc(value);
    pendingRef.current = value;
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => updateMut.mutate(value), 800);
  };

  const handleSummarize = async () => {
    setSummaryLoading(true);
    try {
      const result = await ai.summarizeCode({ project_id: projectId, code_id: code.id });
      setSummaryText(result.summary);
      setSummarySegments(result.segment_count);
    } catch {
      setSummaryText('Could not generate summary. Make sure Ollama is running and an LLM model is selected in Settings.');
      setSummarySegments(0);
    } finally {
      setSummaryLoading(false);
    }
  };

  const handleCopy = () => {
    if (summaryText) {
      navigator.clipboard.writeText(summaryText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <div className="border-t border-gray-200 bg-gray-50 p-3 shrink-0 max-h-[50%] overflow-auto">
      <div className="flex items-center gap-2 mb-1.5">
        <span
          className="w-3 h-3 rounded-sm shrink-0"
          style={{ backgroundColor: code.color }}
        />
        <span className="text-sm font-medium text-gray-700 truncate">{code.name}</span>
        <span className="text-xs text-gray-400 ml-auto">{code.coding_count ?? 0} segments</span>
      </div>
      <label className="block text-xs font-medium text-gray-500 mb-1">Definition / Description</label>
      {editing ? (
        <MentionTextarea
          rows={5}
          autoFocus
          className="border border-gray-200 rounded-md bg-white focus-within:ring-1 focus-within:ring-indigo-500"
          textClassName="px-2.5 py-2 text-sm"
          value={desc}
          onChange={handleChange}
          candidates={buildMentionCandidates(codes, memos)}
          placeholder="Define this code: what it means, when to apply it… (type @ to reference a code or memo)"
          onBlur={() => setEditing(false)}
        />
      ) : (
        <MentionView
          value={desc}
          candidates={buildMentionCandidates(codes, memos)}
          onJump={onJumpToMention}
          onEdit={() => setEditing(true)}
          className="min-h-[4.5rem] max-h-40 overflow-auto border border-gray-200 rounded-md bg-white px-2.5 py-2 text-sm text-gray-800"
          placeholder="Define this code: what it means, when to apply it… (type @ to reference a code or memo)"
        />
      )}
      <div className="flex items-center justify-between mt-1">
        <p className="text-xs text-gray-400">
          {updateMut.isPending
            ? 'Saving...'
            : saveFailed
              ? 'Could not save; it will be retried with your next edit'
              : 'Auto-saves as you type'}
        </p>
        <button
          onClick={handleSummarize}
          disabled={summaryLoading || (code.coding_count ?? 0) === 0}
          className="text-xs text-purple-600 hover:text-purple-700 flex items-center gap-1 disabled:opacity-40 disabled:cursor-not-allowed"
          title={((code.coding_count ?? 0) === 0) ? 'Code some passages first' : 'Generate AI summary of all coded passages'}
        >
          <Sparkles size={11} />
          {summaryLoading ? 'Summarizing...' : 'Summarize Theme'}
        </button>
      </div>

      {summaryText && (
        <div className="mt-2 p-2.5 bg-purple-50 border border-purple-100 rounded-md">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-[10px] text-purple-500 font-medium">
              Based on {summarySegments} coded segment{summarySegments !== 1 ? 's' : ''}
            </span>
            <button
              onClick={handleCopy}
              className="text-[10px] text-purple-500 hover:text-purple-700 flex items-center gap-0.5"
            >
              <Copy size={10} /> {copied ? 'Copied!' : 'Copy'}
            </button>
          </div>
          <p className="text-xs text-purple-900 leading-relaxed whitespace-pre-wrap">{summaryText}</p>
        </div>
      )}
    </div>
  );
}

export function CodeTree({ projectId, codes, memos, selectedCodeId, onSelectCode, onJumpToMention }: Props) {
  const [showNew, setShowNew] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const queryClient = useQueryClient();
  const tree = useMemo(() => buildTree(codes), [codes]);
  const mentionCandidates = useMemo(() => buildMentionCandidates(codes, memos), [codes, memos]);
  const selectedCode = codes.find((c) => c.id === selectedCodeId);

  const { data: currentSettings } = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
    staleTime: 60000,
  });
  const colors = COLOR_SCHEMES[currentSettings?.color_scheme ?? 'Default'] ?? COLOR_SCHEMES.Default;

  const createMut = useMutation({
    mutationFn: (data: { name: string; parent_id?: number; description?: string }) =>
      codesApi.create({ project_id: projectId, color: colors[codes.length % colors.length], ...data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
      setNewName('');
      setNewDesc('');
      setShowNew(false);
    },
  });

  const submitNewCode = () => {
    if (!newName.trim()) return;
    createMut.mutate({
      name: newName.trim(),
      parent_id: selectedCodeId ?? undefined,
      description: newDesc.trim() || undefined,
    });
  };

  // --- Drag-and-drop reorganization ---
  const [draggingId, setDraggingId] = useState<number | null>(null);
  const [dropTarget, setDropTarget] = useState<{ id: number; pos: DropPos } | null>(null);

  const handleMove = async (draggedId: number, targetId: number | null, pos: DropPos) => {
    const dragged = codes.find((c) => c.id === draggedId);
    if (!dragged) return;
    const target = targetId == null ? null : codes.find((c) => c.id === targetId);
    // Can't drop a code into itself or its own subtree (server also rejects cycles).
    if (targetId != null && (!target || isInSubtree(codes, targetId, draggedId))) return;

    const newParent: number | null =
      pos === 'inside' ? (target ? target.id : null) : target ? (target.parent_id ?? null) : null;

    // Destination siblings in current display order, excluding the dragged code.
    const siblings = codes
      .filter((c) => (c.parent_id ?? null) === newParent && c.id !== draggedId)
      .sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name));

    let insertIdx = siblings.length;
    if (pos !== 'inside' && target) {
      const ti = siblings.findIndex((c) => c.id === target.id);
      if (ti >= 0) insertIdx = pos === 'before' ? ti : ti + 1;
    }
    const ordered = [...siblings.slice(0, insertIdx), dragged, ...siblings.slice(insertIdx)];

    // Persist only what changed: the dragged code's parent, plus any shifted sort_order.
    const updates: Promise<unknown>[] = [];
    ordered.forEach((c, idx) => {
      const parentChanged = c.id === draggedId && (c.parent_id ?? null) !== newParent;
      const orderChanged = c.sort_order !== idx;
      if (parentChanged || orderChanged) {
        const payload: Partial<Code> = { sort_order: idx };
        if (c.id === draggedId) payload.parent_id = newParent;
        updates.push(codesApi.update(c.id, payload));
      }
    });
    if (updates.length) {
      await Promise.all(updates);
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
    }
  };

  const dnd: DndCtx = {
    draggingId,
    dropTarget,
    onDragStart: (id) => setDraggingId(id),
    onDragOver: (id, pos) => setDropTarget({ id, pos }),
    onDrop: (id, pos) => {
      if (draggingId != null) handleMove(draggingId, id, pos);
      setDraggingId(null);
      setDropTarget(null);
    },
    onDragEnd: () => {
      setDraggingId(null);
      setDropTarget(null);
    },
  };

  return (
    <div className="flex flex-col h-full">
      <div
        className="flex-1 overflow-auto py-2"
        onDragOver={(e) => {
          if (draggingId != null) e.preventDefault();
        }}
        onDrop={(e) => {
          // Dropped on empty space (node drops stop propagation) -> move to top level.
          if (draggingId == null) return;
          e.preventDefault();
          handleMove(draggingId, null, 'inside');
          setDraggingId(null);
          setDropTarget(null);
        }}
      >
        <div className="px-3 mb-2 flex items-center justify-between">
          <span className="text-xs font-medium text-gray-500 uppercase tracking-wide">Codes</span>
          <button
            onClick={() => setShowNew(true)}
            className="p-1 text-gray-400 hover:text-indigo-600 rounded hover:bg-gray-100"
          >
            <Plus size={14} />
          </button>
        </div>

        {showNew && (
          <div className="mx-2 mb-2 space-y-1.5">
            <input
              autoFocus
              placeholder="Code name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              className="w-full px-2 py-1.5 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500"
              onKeyDown={(e) => {
                if (e.key === 'Enter' && newName.trim()) submitNewCode();
                if (e.key === 'Escape') { setShowNew(false); setNewName(''); setNewDesc(''); }
              }}
            />
            <MentionTextarea
              rows={2}
              className="border border-gray-300 rounded-md focus-within:ring-1 focus-within:ring-indigo-500"
              textClassName="px-2 py-1.5 text-sm"
              value={newDesc}
              onChange={setNewDesc}
              candidates={mentionCandidates}
              placeholder="Definition (optional) — type @ to reference a code or memo"
            />
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs text-gray-400 px-1 truncate">
                {selectedCodeId
                  ? `Under “${codes.find((c) => c.id === selectedCodeId)?.name}”`
                  : 'Top level'}
              </p>
              <button
                onClick={submitNewCode}
                disabled={!newName.trim()}
                className="text-xs px-2 py-1 rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
              >
                Create
              </button>
            </div>
          </div>
        )}

        {tree.length === 0 && !showNew ? (
          <p className="text-sm text-gray-400 text-center py-8 px-4">
            No codes yet. Click + to create one, or select text in a document.
          </p>
        ) : (
          tree.map((code) => (
            <CodeNode
              key={code.id}
              code={code}
              depth={0}
              selectedCodeId={selectedCodeId}
              onSelectCode={onSelectCode}
              projectId={projectId}
              colors={colors}
              dnd={dnd}
            />
          ))
        )}
      </div>

      {/* Code description editor — shown when a code is selected */}
      {selectedCode && (
        <CodeDescriptionEditor
          key={selectedCode.id}
          code={selectedCode}
          projectId={projectId}
          codes={codes}
          memos={memos}
          onJumpToMention={onJumpToMention}
        />
      )}
    </div>
  );
}
