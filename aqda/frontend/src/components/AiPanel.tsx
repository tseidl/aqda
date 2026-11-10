import { useState, useRef, useEffect } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Sparkles, Search, Wand2, X, Power, RefreshCw,
  ShieldCheck, GitBranch, BookOpen, Settings2, Check, EyeOff,
} from 'lucide-react';
import {
  ai, settings as settingsApi, codings as codingsApi, documents as documentsApi,
  type Code, type SimilarResult, type ConsistencyResult, type HierarchySuggestion,
  type GenerationTiming,
} from '../api';
import { AnswerTiming, GenerationProgress } from './GenerationProgress';

interface Props {
  projectId: number;
  codes: Code[];
  /** Whether the panel is the open tab. It stays mounted when hidden, so results and a
   *  running task survive a visit to another tab. */
  active: boolean;
  onNavigate: (docId: number, startPos?: number, endPos?: number) => void;
}

type AiMode =
  | 'search'
  | 'autocode'
  | 'consistency'
  | 'hierarchy'
  | 'definition';

interface ModeConfig {
  id: AiMode;
  label: string;
  icon: React.ReactNode;
  group: 'search' | 'codebook';
  description: string;
}

const MODES: ModeConfig[] = [
  { id: 'search', label: 'Topic Search', icon: <Search size={13} />, group: 'search', description: 'Find passages similar to a query or theme' },
  { id: 'autocode', label: 'Code Suggest', icon: <Wand2 size={13} />, group: 'search', description: 'Find uncoded passages matching a code' },
  { id: 'consistency', label: 'Consistency Check', icon: <ShieldCheck size={13} />, group: 'codebook', description: 'Flag outlier segments within codes' },
  { id: 'hierarchy', label: 'Hierarchy Suggest', icon: <GitBranch size={13} />, group: 'codebook', description: 'Suggest parent-child code groupings' },
  { id: 'definition', label: 'Define Code', icon: <BookOpen size={13} />, group: 'codebook', description: 'Generate a definition from coded passages' },
];

/** Choose which documents every AI feature ignores; the choice is saved with the project. */
function DocumentExclusions({ projectId, disabled }: { projectId: number; disabled: boolean }) {
  const queryClient = useQueryClient();
  const { data: docs = [] } = useQuery({
    queryKey: ['documents', projectId],
    queryFn: () => documentsApi.list(projectId),
  });
  const [draft, setDraft] = useState<Set<number> | null>(null);
  // The documents listed when the choice was opened; if one has since disappeared (deleted,
  // or replaced by a collaborator's version) the choice refers to old documents.
  const [openedWith, setOpenedWith] = useState<number[]>([]);
  const [filter, setFilter] = useState('');
  const excludedCount = docs.filter((d) => d.exclude_from_ai).length;
  const listChanged =
    openedWith.length !== docs.length || openedWith.some((id) => !docs.some((d) => d.id === id));

  // Start the choice from the current exclusions of the current documents.
  const openChoice = () => {
    setFilter('');
    setDraft(new Set(docs.filter((d) => d.exclude_from_ai).map((d) => d.id)));
    setOpenedWith(docs.map((d) => d.id));
  };

  const saveMut = useMutation({
    mutationFn: (ids: number[]) => documentsApi.setAiExclusions(projectId, ids, openedWith),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents', projectId] });
      queryClient.invalidateQueries({ queryKey: ['document'] });
      setDraft(null);
      setOpenedWith([]);
    },
  });

  const toggle = (id: number) => setDraft((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });

  if (draft === null) {
    return (
      <div className="mb-2 px-1 flex items-center justify-between gap-2 text-[11px]">
        <span className="flex items-center gap-1 text-gray-500">
          <EyeOff size={11} />
          {excludedCount
            ? `${excludedCount} document${excludedCount === 1 ? '' : 's'} excluded`
            : 'No documents excluded'}
        </span>
        <button
          onClick={openChoice}
          disabled={disabled || docs.length === 0}
          className="text-purple-600 hover:text-purple-700 disabled:opacity-40"
        >
          Exclude documents…
        </button>
      </div>
    );
  }

  const needle = filter.trim().toLowerCase();
  const shown = docs.filter((d) => !needle || d.name.toLowerCase().includes(needle));
  return (
    <div className="mb-2 rounded-md border border-gray-200 bg-gray-50 p-2">
      <p className="mb-1.5 text-[11px] leading-snug text-gray-600">
        Excluded documents are ignored by every AI feature: they are not searched, and their
        coded segments are not used for suggestions, summaries, definitions, or the consistency
        check. Use this for material that is not part of your data, such as interview guides,
        instructions, consent forms, or documents collecting example quotes for your codebook.
      </p>
      {docs.length > 8 && (
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter documents…"
          className="mb-1 w-full rounded border border-gray-200 bg-white px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-purple-500"
        />
      )}
      <div className="max-h-48 space-y-0.5 overflow-auto">
        {shown.map((d) => (
          <label key={d.id} className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 text-xs text-gray-700 hover:bg-white">
            <input type="checkbox" checked={draft.has(d.id)} onChange={() => toggle(d.id)} />
            <span className="truncate">{d.name}</span>
          </label>
        ))}
      </div>
      {listChanged && (
        <p className="mt-1 text-[11px] text-amber-700">
          The document list changed while you were choosing.{' '}
          <button onClick={openChoice} className="font-medium underline">Start again</button>
        </p>
      )}
      {saveMut.error && (
        <p className="mt-1 text-[11px] text-red-600">{saveMut.error.message.replace(/^\d{3}:\s*/, '')}</p>
      )}
      <div className="mt-2 flex gap-1">
        <button
          onClick={() => saveMut.mutate([...draft])}
          disabled={saveMut.isPending || listChanged}
          className="flex-1 rounded bg-purple-600 py-1 text-xs font-medium text-white hover:bg-purple-700 disabled:opacity-50"
        >
          {saveMut.isPending ? 'Saving…' : 'Save'}
        </button>
        <button
          onClick={() => { setDraft(null); setOpenedWith([]); }}
          className="rounded bg-white px-2 py-1 text-xs text-gray-500 hover:bg-gray-100"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

export function AiPanel({ projectId, codes, active, onNavigate }: Props) {
  const [mode, setMode] = useState<AiMode>('search');
  const [query, setQuery] = useState('');
  const [selectedCodeId, setSelectedCodeId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);

  // Results (typed per mode)
  const [similarResults, setSimilarResults] = useState<SimilarResult[]>([]);
  const [consistencyResults, setConsistencyResults] = useState<ConsistencyResult[]>([]);
  const [hierarchyResult, setHierarchyResult] = useState<HierarchySuggestion | null>(null);
  const [definitionResult, setDefinitionResult] = useState<{ definition: string; segment_count: number; total_segments: number; timing?: GenerationTiming } | null>(null);

  // Model overrides
  const [llmOverride, setLlmOverride] = useState<string>('');
  const [embedOverride, setEmbedOverride] = useState<string>('');

  // Embedding progress polling
  const [embeddingProgress, setEmbeddingProgress] = useState<{
    active: boolean; current: number; total: number; doc_name: string;
  } | null>(null);

  // Search, suggestions, and the consistency check embed passages; the other tools ask the
  // model to write, which has its own progress display.
  const usesEmbeddings = ['search', 'autocode', 'consistency'].includes(mode);
  const abortRef = useRef<AbortController | null>(null);
  const progressRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const queryClient = useQueryClient();
  // Track which suggestion (by index) is being applied, to disable its buttons
  const [applyingIdx, setApplyingIdx] = useState<number | null>(null);
  // The code a Code Suggest run was made for; Apply must use this even if the
  // selector changed afterwards.
  const [suggestionCodeId, setSuggestionCodeId] = useState<number | null>(null);
  // Run stays disabled until the server has acknowledged a cancel.
  const [cancelling, setCancelling] = useState(false);

  // Poll embedding progress while loading
  useEffect(() => {
    if (loading && usesEmbeddings) {
      progressRef.current = setInterval(async () => {
        try {
          const p = await ai.embeddingProgress();
          if (p.active) setEmbeddingProgress(p);
          else setEmbeddingProgress(null);
        } catch { /* ignore */ }
      }, 500);
    } else {
      if (progressRef.current) clearInterval(progressRef.current);
      progressRef.current = null;
      setEmbeddingProgress(null);
    }
    return () => {
      if (progressRef.current) clearInterval(progressRef.current);
    };
  }, [loading, usesEmbeddings]);

  const { data: ollamaStatus, refetch: refreshStatus } = useQuery({
    queryKey: ['ollama-status'],
    queryFn: settingsApi.ollamaStatus,
    refetchInterval: active ? 15000 : false,
    retry: false,
  });
  // The status was not checked while the panel was hidden.
  useEffect(() => { if (active) refreshStatus(); }, [active, refreshStatus]);

  const { data: ollamaModels, refetch: refreshModels } = useQuery({
    queryKey: ['ollama-models'],
    queryFn: settingsApi.ollamaModels,
    retry: false,
  });

  const { data: currentSettings } = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
    staleTime: 30000,
  });

  // Models in Ollama's memory, checked after each task: one that only partly fits into
  // graphics memory answers far more slowly, which is worth telling the researcher.
  const { data: loadedModels, refetch: refreshLoaded } = useQuery({
    queryKey: ['ollama-loaded'],
    queryFn: settingsApi.ollamaLoaded,
    retry: false,
  });
  const splitModels = (loadedModels?.models ?? []).filter((m) => m.cpu_share >= 0.05 && m.cpu_share < 1);

  const effectiveLlm = llmOverride || currentSettings?.llm_model || '';
  const effectiveEmbed = embedOverride || currentSettings?.embedding_model || '';

  const { data: projectDocs, isSuccess: docsLoaded } = useQuery({
    queryKey: ['documents', projectId],
    queryFn: () => documentsApi.list(projectId),
  });

  // A code or document the panel refers to can disappear (deleted, or replaced by a
  // collaborator's version); drop the selection and results instead of acting on old IDs.
  useEffect(() => {
    if (selectedCodeId !== null && !codes.some((c) => c.id === selectedCodeId)) {
      // A run for the vanished code must not fill the panel when its answer arrives later.
      abortRef.current?.abort();
      abortRef.current = null;
      setSelectedCodeId(null);
      setSimilarResults([]);
      setSuggestionCodeId(null);
      setConsistencyResults([]);
      setDefinitionResult(null);
    }
  }, [codes, selectedCodeId]);

  useEffect(() => {
    if (!docsLoaded) return;
    const ids = new Set(projectDocs.map((d) => d.id));
    const stale =
      similarResults.some((r) => !ids.has(r.document_id)) ||
      consistencyResults.some((cr) => cr.outliers.some((o) => !ids.has(o.document_id)));
    if (stale) {
      setSimilarResults([]);
      setSuggestionCodeId(null);
      setConsistencyResults([]);
      setError('The project changed since these results were produced, so they were cleared. Run again for current results.');
    }
  }, [docsLoaded, projectDocs, similarResults, consistencyResults]);

  const isRunning = ollamaStatus?.running ?? false;
  const models = ollamaModels?.models ?? [];
  const currentMode = MODES.find((m) => m.id === mode)!;

  // Which modes need a code selection
  const needsCode = ['autocode', 'consistency', 'definition'].includes(mode);
  // consistency can also run on all codes (code optional)
  const codeRequired = ['autocode', 'definition'].includes(mode);

  const handleStartOllama = async () => {
    try {
      await settingsApi.ollamaStart();
      setTimeout(() => { refreshStatus(); refreshModels(); }, 2000);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to start Ollama');
    }
  };

  const handleCancel = async () => {
    setCancelling(true);
    abortRef.current?.abort();
    abortRef.current = null;
    try {
      // Also stop the server-side embedding after its current batch.
      await ai.cancel();
    } catch {
      // the server may already be idle
    } finally {
      setCancelling(false);
      setLoading(false);
    }
  };

  const clearResults = () => {
    setSimilarResults([]);
    setSuggestionCodeId(null);
    setConsistencyResults([]);
    setHierarchyResult(null);
    setDefinitionResult(null);
    setError(null);
  };

  // Accept a Code Suggest result: apply the selected code to its span, then
  // remove it so the list cycles to the remaining suggestions.
  // Drop a suggestion plus any other suggestion overlapping the same passage of the
  // same document, so the list never offers a span that would overlap that coding.
  const dropSuggestionsOverlapping = (r: SimilarResult, index: number) => {
    setSimilarResults((prev) => prev.filter((s, i) =>
      i !== index &&
      !(s.document_id === r.document_id && s.start_pos < r.end_pos && s.end_pos > r.start_pos)
    ));
  };

  const acceptSuggestion = async (index: number) => {
    const r = similarResults[index];
    if (!r || !suggestionCodeId) return;
    setApplyingIdx(index);
    try {
      // Suggestions were filtered when the run finished; a coding made since then
      // (by hand, or in another tab) must not be overlapped by applying one now.
      const current = await codingsApi.list({ code_id: suggestionCodeId, project_id: projectId });
      const codedMeanwhile = current.some((c) =>
        c.document_id === r.document_id && c.start_pos < r.end_pos && c.end_pos > r.start_pos
      );
      if (codedMeanwhile) {
        dropSuggestionsOverlapping(r, index);
        setError('That passage was coded in the meantime, so the suggestion was removed.');
        return;
      }
      await codingsApi.create({
        document_id: r.document_id,
        code_id: suggestionCodeId,
        start_pos: r.start_pos,
        end_pos: r.end_pos,
        selected_text: r.text,
      });
      dropSuggestionsOverlapping(r, index);
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Failed to apply code';
      if (/^409:/.test(message)) {
        // Already applied exactly: treat like a coding made meanwhile.
        dropSuggestionsOverlapping(r, index);
        setError('That passage already carries this code, so the suggestion was removed.');
      } else {
        setError(message);
      }
    } finally {
      queryClient.invalidateQueries({ queryKey: ['codings'] });
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
      setApplyingIdx(null);
    }
  };

  const rejectSuggestion = (index: number) => {
    setSimilarResults((prev) => prev.filter((_, i) => i !== index));
  };

  const canRun = () => {
    if (!isRunning) return false;
    if (mode === 'search' && !query.trim()) return false;
    if (codeRequired && !selectedCodeId) return false;
    return true;
  };

  const handleRun = async () => {
    abortRef.current = new AbortController();
    setLoading(true);
    setError(null);
    clearResults();

    try {
      switch (mode) {
        case 'search': {
          const res = await ai.findSimilar(
            { project_id: projectId, query: query.trim(), embedding_model: effectiveEmbed || undefined },
            abortRef.current.signal,
          );
          setSimilarResults(res);
          break;
        }
        case 'autocode': {
          const codeId = selectedCodeId!;
          const res = await ai.autoCode(
            { project_id: projectId, code_id: codeId, embedding_model: effectiveEmbed || undefined },
            abortRef.current.signal,
          );
          setSimilarResults(res);
          setSuggestionCodeId(codeId);
          break;
        }
        case 'consistency': {
          const res = await ai.consistencyCheck(
            { project_id: projectId, code_id: selectedCodeId ?? undefined, embedding_model: effectiveEmbed || undefined },
            abortRef.current.signal,
          );
          setConsistencyResults(res.results);
          break;
        }
        case 'hierarchy': {
          const res = await ai.suggestHierarchy(
            { project_id: projectId, llm_model: effectiveLlm || undefined },
            abortRef.current.signal,
          );
          setHierarchyResult(res);
          break;
        }
        case 'definition': {
          const res = await ai.generateDefinition(
            { project_id: projectId, code_id: selectedCodeId!, llm_model: effectiveLlm || undefined },
            abortRef.current.signal,
          );
          setDefinitionResult(res);
          break;
        }
      }
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') return;
      // The server's own message (after the "503: " status prefix) says what went wrong.
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg.replace(/^\d{3}:\s*/, ''));
    } finally {
      setLoading(false);
      abortRef.current = null;
      refreshLoaded();
    }
  };

  const searchModes = MODES.filter((m) => m.group === 'search');
  const codebookModes = MODES.filter((m) => m.group === 'codebook');

  return (
    <div className="p-2 flex flex-col h-full">
      {/* Header with Ollama status */}
      <div className="px-1 mb-3 flex items-center justify-between">
        <span className="text-xs font-medium text-gray-500 uppercase tracking-wide flex items-center gap-1.5">
          <Sparkles size={12} /> AI Assistant
        </span>
        <div className="flex items-center gap-2">
          <span className={`w-1.5 h-1.5 rounded-full ${isRunning ? 'bg-green-500' : 'bg-red-400'}`} />
          {!isRunning ? (
            <button
              onClick={handleStartOllama}
              className="text-[10px] text-indigo-600 hover:text-indigo-700 flex items-center gap-0.5"
            >
              <Power size={10} /> Start Ollama
            </button>
          ) : (
            <button
              onClick={() => { refreshStatus(); refreshModels(); }}
              className="text-gray-400 hover:text-gray-600"
              title="Refresh"
            >
              <RefreshCw size={10} />
            </button>
          )}
          <button
            onClick={() => setShowSettings(!showSettings)}
            className={`text-gray-400 hover:text-gray-600 ${showSettings ? 'text-purple-600' : ''}`}
            title="Model settings"
          >
            <Settings2 size={12} />
          </button>
        </div>
      </div>

      {/* Model settings (collapsible) */}
      {showSettings && (
        <div className="mb-3 px-1 py-2 bg-gray-50 rounded-lg space-y-2">
          <div>
            <label className="block text-[10px] text-gray-500 mb-0.5">LLM (analysis, definitions)</label>
            <select
              value={llmOverride || currentSettings?.llm_model || ''}
              onChange={(e) => setLlmOverride(e.target.value)}
              className="w-full px-2 py-1 text-xs border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-purple-500 bg-white"
            >
              <option value="">
                {currentSettings?.llm_model ? `Default (${currentSettings.llm_model})` : 'Select model...'}
              </option>
              {models.filter((m) => m !== currentSettings?.llm_model).map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-[10px] text-gray-500 mb-0.5">Embedding (search, similarity)</label>
            <select
              value={embedOverride || currentSettings?.embedding_model || ''}
              onChange={(e) => setEmbedOverride(e.target.value)}
              className="w-full px-2 py-1 text-xs border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-purple-500 bg-white"
            >
              <option value="">
                {currentSettings?.embedding_model ? `Default (${currentSettings.embedding_model})` : 'Select model...'}
              </option>
              {models.filter((m) => m !== currentSettings?.embedding_model).map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </div>
        </div>
      )}

      {/* Mode selector — grouped */}
      <div className="mb-3 space-y-1.5">
        <div className="px-1 text-[10px] text-gray-400 uppercase tracking-wider">Search & Discovery</div>
        <div className="grid grid-cols-3 gap-1">
          {searchModes.map((m) => (
            <button
              key={m.id}
              disabled={loading}
              onClick={() => { setMode(m.id); clearResults(); }}
              className={`py-1.5 px-1 text-[11px] rounded-md flex flex-col items-center gap-0.5 transition-colors disabled:opacity-60 disabled:cursor-not-allowed ${
                mode === m.id
                  ? 'bg-purple-100 text-purple-700 font-medium'
                  : 'bg-gray-50 text-gray-500 hover:bg-gray-100 hover:text-gray-700'
              }`}
              title={loading ? 'Cancel the running task to use another tool' : m.description}
            >
              {m.icon}
              <span className="leading-tight text-center">{m.label}</span>
            </button>
          ))}
        </div>

        <div className="px-1 text-[10px] text-gray-400 uppercase tracking-wider pt-1">Codebook Tools</div>
        <div className="grid grid-cols-3 gap-1">
          {codebookModes.map((m) => (
            <button
              key={m.id}
              disabled={loading}
              onClick={() => { setMode(m.id); clearResults(); }}
              className={`py-1.5 px-1 text-[11px] rounded-md flex flex-col items-center gap-0.5 transition-colors disabled:opacity-60 disabled:cursor-not-allowed ${
                mode === m.id
                  ? 'bg-purple-100 text-purple-700 font-medium'
                  : 'bg-gray-50 text-gray-500 hover:bg-gray-100 hover:text-gray-700'
              }`}
              title={loading ? 'Cancel the running task to use another tool' : m.description}
            >
              {m.icon}
              <span className="leading-tight text-center">{m.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Mode description */}
      <p className="text-[11px] text-gray-400 px-1 mb-2">
        {loading
          ? `${currentMode.label} is running. Cancel it to use another tool; its result stays here while you work in other tabs.`
          : currentMode.description}
      </p>

      <DocumentExclusions projectId={projectId} disabled={loading} />

      {/* Input area — varies by mode */}
      {mode === 'search' && (
        <textarea
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Describe a topic or theme to search for..."
          className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md mb-2 focus:outline-none focus:ring-1 focus:ring-purple-500 resize-none"
          rows={3}
        />
      )}

      {needsCode && (
        <div className="mb-2">
          <select
            value={selectedCodeId ?? ''}
            disabled={loading || applyingIdx !== null}
            onChange={(e) => {
              // Results belong to the code they were produced for; changing it clears them.
              setSelectedCodeId(e.target.value ? Number(e.target.value) : null);
              clearResults();
            }}
            className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-purple-500 disabled:opacity-60"
          >
            <option value="">{mode === 'consistency' ? 'All codes (or select one)' : 'Select a code...'}</option>
            {codes.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        </div>
      )}

      {/* Run / Cancel button + progress */}
      {loading ? (
        <div className="space-y-2">
          <button
            onClick={handleCancel}
            disabled={cancelling}
            className="w-full py-2 bg-red-500 text-white rounded-md text-sm font-medium hover:bg-red-600 disabled:opacity-60 flex items-center justify-center gap-1.5"
          >
            <X size={14} /> {cancelling ? 'Cancelling…' : 'Cancel'}
          </button>
          {embeddingProgress && (
            <div className="px-1">
              <div className="flex items-center justify-between text-[10px] text-gray-500 mb-1">
                <span>Embedding document {embeddingProgress.current}/{embeddingProgress.total}</span>
                <span>{Math.round((embeddingProgress.current / embeddingProgress.total) * 100)}%</span>
              </div>
              <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                <div
                  className="h-full bg-purple-500 rounded-full transition-all duration-300"
                  style={{ width: `${(embeddingProgress.current / embeddingProgress.total) * 100}%` }}
                />
              </div>
              <p className="text-[10px] text-gray-400 mt-0.5 truncate">{embeddingProgress.doc_name}</p>
            </div>
          )}
          {!usesEmbeddings && <GenerationProgress className="px-1" />}
          {usesEmbeddings && !embeddingProgress && (
            <p className="text-[10px] text-gray-400 text-center animate-pulse">Processing...</p>
          )}
        </div>
      ) : (
        <button
          onClick={handleRun}
          disabled={!canRun() || cancelling}
          className="w-full py-2 bg-purple-600 text-white rounded-md text-sm font-medium hover:bg-purple-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-1.5 transition-colors"
        >
          {currentMode.icon}
          <span>Run {currentMode.label}</span>
        </button>
      )}

      {splitModels.map((m) => (
        <div key={m.name} className="mt-2 p-2 bg-amber-50 border border-amber-200 rounded-md text-[11px] leading-snug text-amber-800">
          “{m.name}” does not fit into this computer's graphics memory: about {Math.round(m.cpu_share * 100)}% of it
          runs on the processor, which makes answers much slower. A smaller model will be faster.
        </div>
      ))}

      {/* Error display */}
      {error && (
        <div className="mt-2 p-2 bg-red-50 border border-red-200 rounded-md text-xs text-red-700">
          {error}
        </div>
      )}

      {/* Results area */}
      <div className="mt-3 flex-1 overflow-y-auto min-h-0">
        {/* Similar results (search, autocode, negative-cases) */}
        {similarResults.length > 0 && (
          <div className="space-y-1">
            <p className="text-xs text-gray-500 px-1 sticky top-0 bg-white py-1">
              {similarResults.length} {mode === 'autocode' ? 'suggestion' : 'result'}{similarResults.length !== 1 ? 's' : ''}
            </p>
            {similarResults.map((r, i) => (
              <div
                key={`${r.document_id}-${r.start_pos}-${r.end_pos}`}
                className="p-2 rounded-md border border-gray-100 hover:bg-purple-50/40 transition-colors"
              >
                <button
                  type="button"
                  onClick={() => onNavigate(r.document_id, r.start_pos, r.end_pos)}
                  className="w-full text-left"
                  title="Go to this passage"
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-medium text-gray-600 truncate">{r.document_name}</span>
                    <span className="text-xs text-purple-600 font-mono ml-2 shrink-0">{(r.similarity * 100).toFixed(0)}%</span>
                  </div>
                  <p className="text-xs text-gray-600 leading-relaxed">
                    {r.text.length > 150 ? r.text.slice(0, 150) + '...' : r.text}
                  </p>
                </button>
                {mode === 'autocode' && suggestionCodeId && (
                  <div className="flex gap-1 mt-1.5">
                    <button
                      type="button"
                      onClick={() => acceptSuggestion(i)}
                      disabled={applyingIdx !== null}
                      className="flex-1 flex items-center justify-center gap-1 py-1 text-xs font-medium rounded bg-green-50 text-green-700 hover:bg-green-100 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <Check size={12} /> Apply
                    </button>
                    <button
                      type="button"
                      onClick={() => rejectSuggestion(i)}
                      disabled={applyingIdx !== null}
                      className="flex-1 flex items-center justify-center gap-1 py-1 text-xs font-medium rounded bg-gray-50 text-gray-500 hover:bg-gray-100 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <X size={12} /> Dismiss
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Consistency check results */}
        {consistencyResults.length > 0 && (
          <div className="space-y-3">
            <p className="text-xs text-gray-500 px-1 sticky top-0 bg-white py-1">
              {consistencyResults.length} code{consistencyResults.length !== 1 ? 's' : ''} analyzed
            </p>
            {consistencyResults.map((cr) => (
              <div key={cr.code_id} className="border border-gray-100 rounded-md p-2">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-semibold text-gray-700">{cr.code_name}</span>
                  <span className="text-[10px] text-gray-400">{cr.segment_count} segments</span>
                </div>
                <div className="flex items-center gap-2 mb-1.5">
                  <div className="flex-1 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full ${cr.avg_similarity > 0.7 ? 'bg-green-400' : cr.avg_similarity > 0.4 ? 'bg-yellow-400' : 'bg-red-400'}`}
                      style={{ width: `${Math.max(5, cr.avg_similarity * 100)}%` }}
                    />
                  </div>
                  <span className="text-[10px] text-gray-500 font-mono w-10 text-right">
                    {(cr.avg_similarity * 100).toFixed(0)}%
                  </span>
                </div>
                {cr.outlier_cutoff === null ? (
                  <p className="text-[10px] text-gray-400">Needs at least four segments to judge consistency</p>
                ) : cr.outliers.length > 0 ? (
                  <div className="space-y-1">
                    <p className="text-[10px] text-orange-600 font-medium">
                      {cr.outliers.length} outlier{cr.outliers.length !== 1 ? 's' : ''} found
                    </p>
                    {cr.outliers.map((o) => (
                      <button
                        key={o.coding_id}
                        onClick={() => onNavigate(o.document_id, o.start_pos, o.end_pos)}
                        className="w-full text-left p-1.5 rounded bg-orange-50 hover:bg-orange-100 transition-colors"
                      >
                        <div className="flex items-center justify-between">
                          <span className="text-[10px] text-gray-500">{o.document_name}</span>
                          <span className="text-[10px] text-orange-600 font-mono">{(o.similarity * 100).toFixed(0)}%</span>
                        </div>
                        <p className="text-xs text-gray-600 mt-0.5">
                          {o.selected_text.length > 120 ? o.selected_text.slice(0, 120) + '...' : o.selected_text}
                        </p>
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="text-[10px] text-green-600">All segments consistent</p>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Hierarchy suggestion */}
        {hierarchyResult && (
          <div className="space-y-2">
            {hierarchyResult.error ? (
              <div className="p-2 bg-yellow-50 border border-yellow-200 rounded-md text-xs text-yellow-800">
                <p className="font-medium mb-1">Could not parse suggestion</p>
                <p className="whitespace-pre-wrap">{hierarchyResult.raw_response || hierarchyResult.error}</p>
              </div>
            ) : (
              <>
                {hierarchyResult.groups && hierarchyResult.groups.length > 0 && (
                  <div className="space-y-2">
                    <p className="text-xs text-gray-500 px-1">Suggested groupings</p>
                    {hierarchyResult.groups.map((g, i) => (
                      <div key={i} className="border border-purple-100 rounded-md p-2 bg-purple-50/30">
                        <p className="text-xs font-semibold text-purple-800 flex items-center gap-1">
                          <GitBranch size={11} /> {g.suggested_parent}
                        </p>
                        {g.description && (
                          <p className="text-[10px] text-gray-500 mt-0.5">{g.description}</p>
                        )}
                        <div className="flex flex-wrap gap-1 mt-1.5">
                          {(g.children ?? []).map((child) => (
                            <span key={child} className="px-1.5 py-0.5 bg-white border border-purple-200 rounded text-[10px] text-purple-700">
                              {child}
                            </span>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                {hierarchyResult.standalone && hierarchyResult.standalone.length > 0 && (
                  <div>
                    <p className="text-xs text-gray-500 px-1 mb-1">Standalone codes</p>
                    <div className="flex flex-wrap gap-1 px-1">
                      {hierarchyResult.standalone.map((s) => (
                        <span key={s} className="px-1.5 py-0.5 bg-gray-100 border border-gray-200 rounded text-[10px] text-gray-600">
                          {s}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {hierarchyResult.codes_included !== undefined &&
                  hierarchyResult.code_count !== undefined &&
                  hierarchyResult.codes_included < hierarchyResult.code_count && (
                  <p className="text-[10px] text-amber-700 px-1">
                    The model saw {hierarchyResult.codes_included} of {hierarchyResult.code_count} codes;
                    the codebook was too long for its context window.
                  </p>
                )}
                {hierarchyResult.answer_truncated && (
                  <p className="text-[10px] text-amber-700 px-1">
                    The model's answer reached AQDA's length limit, so the suggestion may be incomplete.
                  </p>
                )}
                {hierarchyResult.examples_included === false && (
                  <p className="text-[10px] text-amber-700 px-1">
                    Example passages were left out because the codebook was long; the model saw
                    code names, segment counts, and definitions only.
                  </p>
                )}
                <p className="text-[10px] text-gray-400 px-1 italic">
                  This is a suggestion — review and apply changes manually in the code tree.
                </p>
                <AnswerTiming timing={hierarchyResult.timing} className="px-1" />
              </>
            )}
          </div>
        )}

        {/* Definition result */}
        {definitionResult && (
          <div className="space-y-2">
            <div className="border border-gray-100 rounded-md p-3 bg-gray-50/50">
              <p className="text-xs text-gray-500 mb-1.5">
                {definitionResult.segment_count < definitionResult.total_segments
                  ? `Generated from ${definitionResult.segment_count} of ${definitionResult.total_segments} coded segments, spread over all of them; the rest did not fit the model's context window`
                  : `Generated from ${definitionResult.total_segments} coded segment${definitionResult.total_segments !== 1 ? 's' : ''}`}
              </p>
              <p className="text-sm text-gray-800 leading-relaxed whitespace-pre-wrap">
                {definitionResult.definition}
              </p>
              <AnswerTiming timing={definitionResult.timing} className="mt-1.5" />
            </div>
            <p className="text-[10px] text-gray-400 px-1 italic">
              Copy this definition to your code's description field if you'd like to keep it.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
