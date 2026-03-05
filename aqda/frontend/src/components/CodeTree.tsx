import { useState, useMemo, useRef, useEffect } from 'react';
import { useMutation, useQueryClient, useQuery } from '@tanstack/react-query';
import { ChevronRight, ChevronDown, Plus, Trash2, Pencil, Sparkles, Copy } from 'lucide-react';
import { codes as codesApi, settings as settingsApi, ai, type Code } from '../api';

interface Props {
  projectId: number;
  codes: Code[];
  selectedCodeId: number | null;
  onSelectCode: (id: number | null) => void;
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

function CodeNode({
  code,
  depth,
  selectedCodeId,
  onSelectCode,
  projectId,
  colors,
}: {
  code: Code & { children?: Code[] };
  depth: number;
  selectedCodeId: number | null;
  onSelectCode: (id: number | null) => void;
  projectId: number;
  colors: string[];
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

  const hasChildren = (code.children?.length ?? 0) > 0;
  const isSelected = selectedCodeId === code.id;

  return (
    <div>
      <div
        className={`flex items-center gap-1 px-2 py-1 cursor-pointer rounded-md mx-1 group ${
          isSelected ? 'bg-indigo-50' : 'hover:bg-gray-50'
        }`}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
        onClick={() => onSelectCode(isSelected ? null : code.id)}
      >
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
              if (confirm(`Delete code "${code.name}"?`)) deleteMut.mutate();
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
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** Inline editor for a code's description/definition. */
function CodeDescriptionEditor({ code, projectId }: { code: Code; projectId: number }) {
  const queryClient = useQueryClient();
  const [desc, setDesc] = useState(code.description);
  const saveTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const [summaryText, setSummaryText] = useState<string | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summarySegments, setSummarySegments] = useState(0);
  const [copied, setCopied] = useState(false);

  // Sync when switching codes
  useEffect(() => {
    setDesc(code.description);
    setSummaryText(null);
    setSummarySegments(0);
  }, [code.id, code.description]);

  const updateMut = useMutation({
    mutationFn: (description: string) => codesApi.update(code.id, { description }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['codes', projectId] }),
  });

  const handleChange = (value: string) => {
    setDesc(value);
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      updateMut.mutate(value);
    }, 800);
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
      <textarea
        value={desc}
        onChange={(e) => handleChange(e.target.value)}
        placeholder="Define this code: what does it mean, when should it be applied, what are inclusion/exclusion criteria..."
        className="w-full px-2.5 py-2 text-sm border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500 resize-y bg-white min-h-[4.5rem]"
        rows={5}
      />
      <div className="flex items-center justify-between mt-1">
        <p className="text-xs text-gray-400">
          {updateMut.isPending ? 'Saving...' : 'Auto-saves as you type'}
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

export function CodeTree({ projectId, codes, selectedCodeId, onSelectCode }: Props) {
  const [showNew, setShowNew] = useState(false);
  const [newName, setNewName] = useState('');
  const queryClient = useQueryClient();
  const tree = useMemo(() => buildTree(codes), [codes]);
  const selectedCode = codes.find((c) => c.id === selectedCodeId);

  const { data: currentSettings } = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
    staleTime: 60000,
  });
  const colors = COLOR_SCHEMES[currentSettings?.color_scheme ?? 'Default'] ?? COLOR_SCHEMES.Default;

  const createMut = useMutation({
    mutationFn: (data: { name: string; parent_id?: number }) =>
      codesApi.create({ project_id: projectId, color: colors[codes.length % colors.length], ...data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
      setNewName('');
      setShowNew(false);
    },
  });

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-auto py-2">
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
          <div className="mx-2 mb-2">
            <input
              autoFocus
              placeholder="Code name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              className="w-full px-2 py-1.5 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500"
              onKeyDown={(e) => {
                if (e.key === 'Enter' && newName.trim()) {
                  createMut.mutate({
                    name: newName.trim(),
                    parent_id: selectedCodeId ?? undefined,
                  });
                }
                if (e.key === 'Escape') { setShowNew(false); setNewName(''); }
              }}
            />
            <p className="text-xs text-gray-400 mt-1 px-1">
              {selectedCodeId
                ? `Will be created under "${codes.find((c) => c.id === selectedCodeId)?.name}"`
                : 'Will be created at top level'}
            </p>
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
        />
      )}
    </div>
  );
}
