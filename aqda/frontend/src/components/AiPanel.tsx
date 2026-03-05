import { useState, useRef, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Sparkles, Search, Wand2, X, Power, RefreshCw,
  ShieldCheck, GitBranch, BookOpen, Settings2,
} from 'lucide-react';
import {
  ai, settings as settingsApi,
  type Code, type SimilarResult, type ConsistencyResult, type HierarchySuggestion,
} from '../api';

interface Props {
  projectId: number;
  codes: Code[];
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

export function AiPanel({ projectId, codes, onNavigate }: Props) {
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
  const [definitionResult, setDefinitionResult] = useState<{ definition: string; segment_count: number } | null>(null);

  // Model overrides
  const [llmOverride, setLlmOverride] = useState<string>('');
  const [embedOverride, setEmbedOverride] = useState<string>('');

  // Embedding progress polling
  const [embeddingProgress, setEmbeddingProgress] = useState<{
    active: boolean; current: number; total: number; doc_name: string;
  } | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const progressRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Poll embedding progress while loading
  useEffect(() => {
    if (loading) {
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
  }, [loading]);

  const { data: ollamaStatus, refetch: refreshStatus } = useQuery({
    queryKey: ['ollama-status'],
    queryFn: settingsApi.ollamaStatus,
    refetchInterval: 15000,
    retry: false,
  });

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

  const effectiveLlm = llmOverride || currentSettings?.llm_model || '';
  const effectiveEmbed = embedOverride || currentSettings?.embedding_model || '';

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

  const handleCancel = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setLoading(false);
  };

  const clearResults = () => {
    setSimilarResults([]);
    setConsistencyResults([]);
    setHierarchyResult(null);
    setDefinitionResult(null);
    setError(null);
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
            { project_id: projectId, query: query.trim(), code_id: selectedCodeId ?? undefined, embedding_model: effectiveEmbed || undefined },
            abortRef.current.signal,
          );
          setSimilarResults(res);
          break;
        }
        case 'autocode': {
          const res = await ai.autoCode(
            { project_id: projectId, code_id: selectedCodeId!, embedding_model: effectiveEmbed || undefined },
            abortRef.current.signal,
          );
          setSimilarResults(res);
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
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes('503') || msg.includes('connect')) {
        setError('Cannot connect to Ollama. Make sure it is running and the required model is pulled.');
      } else if (msg.includes('400')) {
        setError('No model configured. Select one below or in Settings.');
      } else {
        setError(msg);
      }
    } finally {
      setLoading(false);
      abortRef.current = null;
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
              onClick={() => { setMode(m.id); clearResults(); }}
              className={`py-1.5 px-1 text-[11px] rounded-md flex flex-col items-center gap-0.5 transition-colors ${
                mode === m.id
                  ? 'bg-purple-100 text-purple-700 font-medium'
                  : 'bg-gray-50 text-gray-500 hover:bg-gray-100 hover:text-gray-700'
              }`}
              title={m.description}
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
              onClick={() => { setMode(m.id); clearResults(); }}
              className={`py-1.5 px-1 text-[11px] rounded-md flex flex-col items-center gap-0.5 transition-colors ${
                mode === m.id
                  ? 'bg-purple-100 text-purple-700 font-medium'
                  : 'bg-gray-50 text-gray-500 hover:bg-gray-100 hover:text-gray-700'
              }`}
              title={m.description}
            >
              {m.icon}
              <span className="leading-tight text-center">{m.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Mode description */}
      <p className="text-[11px] text-gray-400 px-1 mb-2">{currentMode.description}</p>

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
            onChange={(e) => setSelectedCodeId(e.target.value ? Number(e.target.value) : null)}
            className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-purple-500"
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
            className="w-full py-2 bg-red-500 text-white rounded-md text-sm font-medium hover:bg-red-600 flex items-center justify-center gap-1.5"
          >
            <X size={14} /> Cancel
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
          {loading && !embeddingProgress && (
            <p className="text-[10px] text-gray-400 text-center animate-pulse">Processing...</p>
          )}
        </div>
      ) : (
        <button
          onClick={handleRun}
          disabled={!canRun()}
          className="w-full py-2 bg-purple-600 text-white rounded-md text-sm font-medium hover:bg-purple-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-1.5 transition-colors"
        >
          {currentMode.icon}
          <span>Run {currentMode.label}</span>
        </button>
      )}

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
            <p className="text-xs text-gray-500 px-1 sticky top-0 bg-white py-1">{similarResults.length} results</p>
            {similarResults.map((r, i) => (
              <button
                key={i}
                onClick={() => onNavigate(r.document_id, r.start_pos, r.end_pos)}
                className="w-full text-left p-2 rounded-md hover:bg-purple-50 border border-gray-100 transition-colors"
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-medium text-gray-600 truncate">{r.document_name}</span>
                  <span className="text-xs text-purple-600 font-mono">{(r.similarity * 100).toFixed(0)}%</span>
                </div>
                <p className="text-xs text-gray-600 leading-relaxed">
                  {r.text.length > 150 ? r.text.slice(0, 150) + '...' : r.text}
                </p>
              </button>
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
                {cr.outliers.length > 0 ? (
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
                          {g.children.map((child) => (
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
                <p className="text-[10px] text-gray-400 px-1 italic">
                  This is a suggestion — review and apply changes manually in the code tree.
                </p>
              </>
            )}
          </div>
        )}

        {/* Definition result */}
        {definitionResult && (
          <div className="space-y-2">
            <div className="border border-gray-100 rounded-md p-3 bg-gray-50/50">
              <p className="text-xs text-gray-500 mb-1.5">
                Generated from {definitionResult.segment_count} coded segment{definitionResult.segment_count !== 1 ? 's' : ''}
              </p>
              <p className="text-sm text-gray-800 leading-relaxed whitespace-pre-wrap">
                {definitionResult.definition}
              </p>
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
