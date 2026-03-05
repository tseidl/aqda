import { useState, useRef, useCallback, useMemo, useEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { X, Tag, Sparkles, ChevronDown, ChevronRight, Plus, Trash2, Loader2, Minus as MinusIcon, Plus as PlusIcon } from 'lucide-react';
import { ai, codes as codesApi, documents as docsApi, type Document, type Coding, type Code } from '../api';

interface Props {
  document: Document;
  codings: Coding[];
  codes: Code[];
  selectedCodeId: number | null;
  onApplyCode: (codeId: number, startPos: number, endPos: number, text: string) => void;
  onDeleteCoding: (id: number) => void;
  onSelectCode: (id: number) => void;
  highlightRange?: { start: number; end: number } | null;
  onHighlightClear?: () => void;
}

interface TextSelection {
  start: number;
  end: number;
  text: string;
  rect: { top: number; bottom: number; left: number };
}

export function DocumentViewer({ document: doc, codings, codes, selectedCodeId, onApplyCode, onDeleteCoding, highlightRange, onHighlightClear }: Props) {
  const contentRef = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();
  const [selection, setSelection] = useState<TextSelection | null>(null);
  const [docFontSize, setDocFontSize] = useState(14);
  const [clickedCoding, setClickedCoding] = useState<{
    codings: Coding[]; rect: { top: number; left: number };
  } | null>(null);
  const [showNewCodeInput, setShowNewCodeInput] = useState(false);
  const [newCodeName, setNewCodeName] = useState('');
  const [analysisText, setAnalysisText] = useState<string | null>(null);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [showVariables, setShowVariables] = useState(false);
  const [newVarKey, setNewVarKey] = useState('');
  const [newVarValue, setNewVarValue] = useState('');
  const [showAddVar, setShowAddVar] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [transcribeError, setTranscribeError] = useState<string | null>(null);
  const highlightRef = useRef<HTMLElement | null>(null);
  const popupClickRef = useRef(false);

  // Scroll to highlighted range and auto-clear after 4s
  useEffect(() => {
    if (!highlightRange) return;
    // Wait a tick for the DOM to render the highlight element
    const raf = requestAnimationFrame(() => {
      highlightRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    const timer = setTimeout(() => {
      onHighlightClear?.();
    }, 4000);
    return () => { cancelAnimationFrame(raf); clearTimeout(timer); };
  }, [highlightRange, onHighlightClear]);

  const variables = doc.variables ?? {};

  const setVarMut = useMutation({
    mutationFn: (items: { key: string; value: string }[]) =>
      docsApi.setVariables(doc.id, items),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['document', doc.id] });
      queryClient.invalidateQueries({ queryKey: ['documents', doc.project_id] });
    },
  });

  const deleteVarMut = useMutation({
    mutationFn: (key: string) => docsApi.deleteVariable(doc.id, key),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['document', doc.id] });
      queryClient.invalidateQueries({ queryKey: ['documents', doc.project_id] });
    },
  });

  const handleTranscribe = async () => {
    setTranscribing(true);
    setTranscribeError(null);
    try {
      await docsApi.transcribe(doc.id);
      queryClient.invalidateQueries({ queryKey: ['document', doc.id] });
      queryClient.invalidateQueries({ queryKey: ['documents', doc.project_id] });
    } catch (err) {
      setTranscribeError(
        err instanceof Error ? err.message : 'Transcription failed. Make sure faster-whisper is installed (pip install aqda[audio]).'
      );
    } finally {
      setTranscribing(false);
    }
  };

  // Build a map of code id -> code
  const codeMap = useMemo(() => {
    const m = new Map<number, Code>();
    for (const c of codes) m.set(c.id, c);
    return m;
  }, [codes]);

  // For audio docs with transcript, coding operates on the transcript text
  const codeableText = (doc.source_type === 'audio' && doc.transcript) ? doc.transcript : (doc.content ?? '');

  // Build rendered content with highlight spans
  const renderedContent = useMemo(() => {
    const text = codeableText;
    if (codings.length === 0 && !highlightRange) return [{ text, codings: [] as Coding[], highlighted: false }];

    type Event = { pos: number; type: 'start' | 'end'; coding?: Coding; highlight?: boolean };
    const events: Event[] = [];
    for (const c of codings) {
      events.push({ pos: c.start_pos, type: 'start', coding: c });
      events.push({ pos: c.end_pos, type: 'end', coding: c });
    }
    if (highlightRange) {
      events.push({ pos: highlightRange.start, type: 'start', highlight: true });
      events.push({ pos: highlightRange.end, type: 'end', highlight: true });
    }
    events.sort((a, b) => a.pos - b.pos || (a.type === 'end' ? -1 : 1));

    const segments: { text: string; codings: Coding[]; highlighted: boolean }[] = [];
    const active = new Set<Coding>();
    let isHighlighted = false;
    let lastPos = 0;

    for (const ev of events) {
      if (ev.pos > lastPos) {
        segments.push({ text: text.slice(lastPos, ev.pos), codings: [...active], highlighted: isHighlighted });
      }
      if (ev.highlight) {
        isHighlighted = ev.type === 'start';
      } else if (ev.coding) {
        if (ev.type === 'start') active.add(ev.coding);
        else active.delete(ev.coding);
      }
      lastPos = ev.pos;
    }
    if (lastPos < text.length) {
      segments.push({ text: text.slice(lastPos), codings: [], highlighted: false });
    }

    return segments;
  }, [codeableText, codings, highlightRange]);

  // Handle text selection or click on coded passage
  const handleMouseUp = useCallback(() => {
    // Skip if the click was on a popup (buttons, etc.)
    if (popupClickRef.current) { popupClickRef.current = false; return; }

    const sel = window.getSelection();
    if (!sel || !contentRef.current) return;

    // Text was selected — show apply-code popup
    if (!sel.isCollapsed) {
      setClickedCoding(null);
      const range = sel.getRangeAt(0);
      const text = sel.toString().trim();
      if (!text) { setSelection(null); return; }

      const walker = document.createTreeWalker(contentRef.current, NodeFilter.SHOW_TEXT);
      let offset = 0;
      let startPos = -1;
      let endPos = -1;

      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (node === range.startContainer) startPos = offset + range.startOffset;
        if (node === range.endContainer) { endPos = offset + range.endOffset; break; }
        offset += node.textContent?.length ?? 0;
      }

      if (startPos >= 0 && endPos > startPos) {
        const rect = range.getBoundingClientRect();
        const containerRect = contentRef.current.getBoundingClientRect();
        setSelection({
          start: startPos, end: endPos, text,
          rect: {
            top: rect.top - containerRect.top + contentRef.current.scrollTop,
            bottom: rect.bottom - containerRect.top + contentRef.current.scrollTop,
            left: rect.left - containerRect.left + rect.width / 2,
          },
        });
      }
      return;
    }

    // No selection — check if clicked on a coded passage
    if (sel.anchorNode) {
      const walker = document.createTreeWalker(contentRef.current, NodeFilter.SHOW_TEXT);
      let offset = 0;
      let clickPos = -1;
      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (node === sel.anchorNode) { clickPos = offset + sel.anchorOffset; break; }
        offset += node.textContent?.length ?? 0;
      }

      if (clickPos >= 0) {
        const overlapping = codings.filter(c => c.start_pos <= clickPos && c.end_pos > clickPos);
        if (overlapping.length > 0) {
          const range = document.createRange();
          range.setStart(sel.anchorNode, sel.anchorOffset);
          range.setEnd(sel.anchorNode, sel.anchorOffset);
          const rect = range.getBoundingClientRect();
          const containerRect = contentRef.current.getBoundingClientRect();
          setClickedCoding({
            codings: overlapping,
            rect: {
              top: rect.top - containerRect.top + contentRef.current.scrollTop,
              left: Math.max(10, rect.left - containerRect.left),
            },
          });
          setSelection(null);
          return;
        }
      }
    }

    // Clicked on uncoded text — clear all popups
    setClickedCoding(null);
    setSelection(null);
    setShowNewCodeInput(false);
    setAnalysisText(null);
  }, [codings]);

  // Apply code to selection
  const applyCode = useCallback(
    (codeId: number) => {
      if (!selection) return;
      onApplyCode(codeId, selection.start, selection.end, selection.text);
      setSelection(null);
      window.getSelection()?.removeAllRanges();
    },
    [selection, onApplyCode]
  );

  // AI analyze selection
  const analyzeSelection = useCallback(async () => {
    if (!selection) return;
    setAnalysisLoading(true);
    try {
      const result = await ai.analyze({ text: selection.text });
      setAnalysisText(result.analysis);
    } catch {
      setAnalysisText('AI analysis unavailable. Make sure Ollama is running.');
    } finally {
      setAnalysisLoading(false);
    }
  }, [selection]);

  return (
    <div className="h-full flex flex-col">
      {/* Document header */}
      <div className="border-b border-gray-200 bg-white shrink-0">
        <div className="px-4 py-2 flex items-center justify-between">
          <div className="flex items-center gap-2 min-w-0">
            <h2 className="text-sm font-medium text-gray-700 truncate">{doc.name}</h2>
            <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium shrink-0 ${
              doc.source_type === 'pdf' ? 'bg-red-100 text-red-600'
              : doc.source_type === 'image' ? 'bg-green-100 text-green-600'
              : doc.source_type === 'audio' ? 'bg-amber-100 text-amber-600'
              : 'bg-blue-100 text-blue-600'
            }`}>
              {doc.source_type === 'pdf' ? 'PDF' : doc.source_type === 'image' ? 'IMG' : doc.source_type === 'audio' ? 'AUD' : 'TXT'}
            </span>
          </div>
          <div className="flex items-center gap-3">
            {Object.keys(variables).length > 0 && !showVariables && (
              <span className="text-xs text-gray-400">
                {Object.keys(variables).length} variable{Object.keys(variables).length !== 1 ? 's' : ''}
              </span>
            )}
            <button
              onClick={() => setShowVariables(!showVariables)}
              className="text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1"
            >
              {showVariables ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              Variables
            </button>
            <div className="flex items-center gap-1 border-l border-gray-200 pl-3 ml-1">
              <button
                onClick={() => setDocFontSize((s) => Math.max(10, s - 2))}
                className="w-6 h-6 flex items-center justify-center rounded text-gray-400 hover:text-gray-600 hover:bg-gray-100"
                title="Decrease font size"
              >
                <MinusIcon size={12} />
              </button>
              <span className="text-[10px] text-gray-400 w-6 text-center">{docFontSize}</span>
              <button
                onClick={() => setDocFontSize((s) => Math.min(24, s + 2))}
                className="w-6 h-6 flex items-center justify-center rounded text-gray-400 hover:text-gray-600 hover:bg-gray-100"
                title="Increase font size"
              >
                <PlusIcon size={12} />
              </button>
            </div>
            <span className="text-xs text-gray-400">
              {(doc.content?.length ?? 0).toLocaleString()} chars
            </span>
          </div>
        </div>

        {/* Variables panel */}
        {showVariables && (
          <div className="px-4 pb-3 border-t border-gray-100 bg-gray-50">
            <div className="pt-2 space-y-1.5">
              {Object.entries(variables).map(([key, value]) => (
                <div key={key} className="flex items-center gap-2 group">
                  <span className="text-xs font-medium text-gray-500 w-28 shrink-0 truncate">{key}</span>
                  <input
                    className="flex-1 text-xs px-2 py-1 border border-gray-200 rounded bg-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
                    defaultValue={value}
                    onBlur={(e) => {
                      if (e.target.value !== value) {
                        setVarMut.mutate([{ key, value: e.target.value }]);
                      }
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                    }}
                  />
                  <button
                    onClick={() => deleteVarMut.mutate(key)}
                    className="hidden group-hover:block p-0.5 text-gray-400 hover:text-red-500"
                  >
                    <Trash2 size={11} />
                  </button>
                </div>
              ))}

              {Object.keys(variables).length === 0 && !showAddVar && (
                <p className="text-xs text-gray-400 py-1">
                  No variables yet. Add metadata like author, date, source, etc.
                </p>
              )}

              {showAddVar ? (
                <div className="flex items-center gap-2">
                  <input
                    autoFocus
                    placeholder="Key"
                    value={newVarKey}
                    onChange={(e) => setNewVarKey(e.target.value)}
                    className="w-28 text-xs px-2 py-1 border border-gray-200 rounded bg-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') { setShowAddVar(false); setNewVarKey(''); setNewVarValue(''); }
                    }}
                  />
                  <input
                    placeholder="Value"
                    value={newVarValue}
                    onChange={(e) => setNewVarValue(e.target.value)}
                    className="flex-1 text-xs px-2 py-1 border border-gray-200 rounded bg-white focus:outline-none focus:ring-1 focus:ring-indigo-500"
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && newVarKey.trim()) {
                        setVarMut.mutate([{ key: newVarKey.trim(), value: newVarValue }]);
                        setNewVarKey('');
                        setNewVarValue('');
                        setShowAddVar(false);
                      }
                      if (e.key === 'Escape') { setShowAddVar(false); setNewVarKey(''); setNewVarValue(''); }
                    }}
                  />
                </div>
              ) : (
                <button
                  onClick={() => setShowAddVar(true)}
                  className="text-xs text-indigo-600 hover:text-indigo-700 flex items-center gap-1 pt-1"
                >
                  <Plus size={11} /> Add variable
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Document content */}
      <div className="flex-1 overflow-auto relative" ref={contentRef} onMouseUp={handleMouseUp}>
        {doc.source_type === 'image' ? (
          <div className="p-6 flex items-center justify-center">
            <img
              src={doc.content}
              alt={doc.name}
              className="max-w-full max-h-[80vh] object-contain rounded shadow-sm"
            />
          </div>
        ) : doc.source_type === 'audio' ? (
          <div>
            {/* Audio player */}
            <div className="p-4 border-b border-gray-200 bg-white">
              <audio controls className="w-full" src={doc.content}>
                Your browser does not support the audio element.
              </audio>
              {!doc.transcript && (
                <div className="mt-3">
                  <button
                    onClick={handleTranscribe}
                    disabled={transcribing}
                    className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-indigo-600 text-white text-sm font-medium rounded-md hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    {transcribing ? (
                      <>
                        <Loader2 size={16} className="animate-spin" />
                        Transcribing... (this may take a while)
                      </>
                    ) : (
                      <>
                        <Sparkles size={16} />
                        Transcribe with Whisper
                      </>
                    )}
                  </button>
                  <p className="text-xs text-gray-400 text-center mt-2">
                    Uses local Whisper model to convert speech to text. The transcript appears below the player.
                  </p>
                  {transcribeError && (
                    <div className="mt-2 p-2 bg-red-50 border border-red-100 rounded text-xs text-red-600">
                      {transcribeError}
                    </div>
                  )}
                </div>
              )}
            </div>
            {/* Transcript (codeable text) */}
            {doc.transcript && (
              <div className="p-6 max-w-4xl mx-auto text-gray-800 whitespace-pre-wrap select-text" style={{ fontSize: `${docFontSize}px`, lineHeight: 1.8 }}>
                {renderedContent.map((seg, i) => {
                  const isFirstHighlight = seg.highlighted && !renderedContent.slice(0, i).some((s) => s.highlighted);
                  if (seg.codings.length === 0 && !seg.highlighted) {
                    return <span key={i}>{seg.text}</span>;
                  }
                  if (seg.codings.length === 0 && seg.highlighted) {
                    return (
                      <mark
                        key={i}
                        ref={isFirstHighlight ? (el) => { highlightRef.current = el; } : undefined}
                        className="ai-search-highlight"
                        style={{ backgroundColor: 'rgba(120, 120, 120, 0.2)', borderBottom: '2px solid #9ca3af' }}
                      >
                        {seg.text}
                      </mark>
                    );
                  }
                  const primary = seg.codings[0];
                  const color = primary.code_color ?? '#6366f1';
                  return (
                    <mark
                      key={i}
                      ref={isFirstHighlight ? (el) => { highlightRef.current = el; } : undefined}
                      data-color={color}
                      className="coded-segment cursor-pointer rounded-xs"
                      style={{ backgroundColor: `${color}25`, borderBottom: `2px solid ${color}` }}
                      title={seg.codings.map((c) => c.code_name).join(', ')}
                    >
                      {seg.text}
                    </mark>
                  );
                })}
              </div>
            )}
          </div>
        ) : (<>
        <div className="p-6 max-w-4xl mx-auto text-gray-800 whitespace-pre-wrap select-text" style={{ fontSize: `${docFontSize}px`, lineHeight: 1.8 }}>
          {renderedContent.map((seg, i) => {
            const isFirstHighlight = seg.highlighted && !renderedContent.slice(0, i).some((s) => s.highlighted);
            if (seg.codings.length === 0 && !seg.highlighted) {
              return <span key={i}>{seg.text}</span>;
            }
            if (seg.codings.length === 0 && seg.highlighted) {
              return (
                <mark
                  key={i}
                  ref={isFirstHighlight ? (el) => { highlightRef.current = el; } : undefined}
                  className="ai-search-highlight"
                  style={{ backgroundColor: 'rgba(120, 120, 120, 0.2)', borderBottom: '2px solid #9ca3af' }}
                >
                  {seg.text}
                </mark>
              );
            }
            // Use the first coding's color as primary, show all on hover
            const primary = seg.codings[0];
            const color = primary.code_color ?? '#6366f1';
            return (
              <mark
                key={i}
                ref={isFirstHighlight ? (el) => { highlightRef.current = el; } : undefined}
                data-color={color}
                className={seg.highlighted ? 'ai-search-highlight' : undefined}
                style={{
                  backgroundColor: seg.highlighted
                    ? 'rgba(120, 120, 120, 0.25)'
                    : color + '30',
                  borderBottom: `2px solid ${seg.highlighted ? '#9ca3af' : color}`,
                }}
                title={seg.codings.map((c) => c.code_name).join(', ')}
              >
                {seg.text}
              </mark>
            );
          })}
        </div>

        {/* Selection popup */}
        {selection && (() => {
          // Find codings that overlap the selection
          const overlapping = codings.filter(
            (c) => c.start_pos < selection.end && c.end_pos > selection.start
          );
          // If the selection is near the top of the visible area, show popup below
          const scrollTop = contentRef.current?.scrollTop ?? 0;
          const nearTop = selection.rect.top - scrollTop < 200;
          return (
          <div
            className="absolute z-50 bg-white rounded-lg shadow-xl border border-gray-200 p-3 min-w-[220px] max-w-[280px]"
            style={nearTop ? {
              top: selection.rect.bottom + 8,
              left: Math.max(10, Math.min(selection.rect.left - 110, (contentRef.current?.clientWidth ?? 400) - 290)),
            } : {
              top: selection.rect.top - 10,
              left: Math.max(10, Math.min(selection.rect.left - 110, (contentRef.current?.clientWidth ?? 400) - 290)),
              transform: 'translateY(-100%)',
            }}
            onMouseDown={(e) => { e.preventDefault(); popupClickRef.current = true; }}
          >
            {/* Existing codings on this selection — with delete */}
            {overlapping.length > 0 && (
              <div className="mb-2 pb-2 border-b border-gray-100">
                <p className="text-[10px] text-gray-400 uppercase tracking-wider mb-1">Active codings</p>
                {overlapping.map((c) => (
                  <div key={c.id} className="flex items-center gap-2 px-2 py-1 rounded hover:bg-red-50 group">
                    <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: c.code_color }} />
                    <span className="text-sm text-gray-700 truncate flex-1">{c.code_name}</span>
                    <button
                      onClick={() => { onDeleteCoding(c.id); setSelection(null); }}
                      className="text-gray-300 group-hover:text-red-500 p-0.5"
                      title="Remove coding"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                ))}
              </div>
            )}

            <div className="text-xs text-gray-500 mb-2 flex items-center justify-between">
              <span className="flex items-center gap-1"><Tag size={12} /> Apply code</span>
              <button onClick={() => { setSelection(null); setShowNewCodeInput(false); setAnalysisText(null); }} className="text-gray-400 hover:text-gray-600">
                <X size={14} />
              </button>
            </div>

            {/* Quick apply with selected code */}
            {selectedCodeId && (
              <button
                onClick={() => applyCode(selectedCodeId)}
                className="w-full text-left px-2 py-1.5 rounded text-sm hover:bg-gray-50 flex items-center gap-2 mb-1 font-medium"
              >
                <span
                  className="w-3 h-3 rounded-sm shrink-0"
                  style={{ backgroundColor: codeMap.get(selectedCodeId)?.color }}
                />
                {codeMap.get(selectedCodeId)?.name}
              </button>
            )}

            {/* Code list */}
            <div className="max-h-40 overflow-auto space-y-0.5">
              {codes
                .filter((c) => c.id !== selectedCodeId)
                .map((code) => (
                  <button
                    key={code.id}
                    onClick={() => applyCode(code.id)}
                    className="w-full text-left px-2 py-1 rounded text-sm hover:bg-gray-50 flex items-center gap-2"
                  >
                    <span
                      className="w-2.5 h-2.5 rounded-sm shrink-0"
                      style={{ backgroundColor: code.color }}
                    />
                    <span className="truncate">{code.name}</span>
                    {code.coding_count ? (
                      <span className="text-xs text-gray-400 ml-auto">{code.coding_count}</span>
                    ) : null}
                  </button>
                ))}
            </div>

            {/* New code inline */}
            {showNewCodeInput ? (
              <div className="mt-2 pt-2 border-t border-gray-100">
                <input
                  autoFocus
                  placeholder="New code name"
                  value={newCodeName}
                  onChange={(e) => setNewCodeName(e.target.value)}
                  className="w-full px-2 py-1 border border-gray-200 rounded text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500"
                  onKeyDown={async (e) => {
                    if (e.key === 'Enter' && newCodeName.trim()) {
                      const newCode = await codesApi.create({
                        project_id: doc.project_id,
                        name: newCodeName.trim(),
                      });
                      applyCode(newCode.id);
                      setNewCodeName('');
                      setShowNewCodeInput(false);
                    }
                    if (e.key === 'Escape') {
                      setShowNewCodeInput(false);
                      setNewCodeName('');
                    }
                  }}
                />
              </div>
            ) : (
              <button
                onClick={() => setShowNewCodeInput(true)}
                className="w-full text-left px-2 py-1.5 mt-1 rounded text-sm text-indigo-600 hover:bg-indigo-50 border-t border-gray-100 pt-2"
              >
                + New code
              </button>
            )}

            {/* AI analyze */}
            <button
              onClick={analyzeSelection}
              disabled={analysisLoading}
              className="w-full text-left px-2 py-1.5 mt-1 rounded text-sm text-purple-600 hover:bg-purple-50 flex items-center gap-1.5"
            >
              <Sparkles size={13} />
              {analysisLoading ? 'Analyzing...' : 'AI Analyze'}
            </button>

            {analysisText && (
              <div className="mt-2 p-2 bg-purple-50 rounded text-xs text-purple-900 max-h-32 overflow-auto">
                {analysisText}
              </div>
            )}
          </div>
          );
        })()}

        {/* Click-on-coded-passage popup */}
        {clickedCoding && (
          <div
            className="absolute z-50 bg-white rounded-lg shadow-xl border border-gray-200 py-2 px-1 min-w-[180px]"
            style={{
              top: clickedCoding.rect.top + 20,
              left: Math.min(clickedCoding.rect.left, (contentRef.current?.clientWidth ?? 400) - 200),
            }}
            onMouseDown={(e) => { e.preventDefault(); popupClickRef.current = true; }}
          >
            <div className="px-2 pb-1.5 mb-1 border-b border-gray-100 flex items-center justify-between">
              <span className="text-[10px] text-gray-400 uppercase tracking-wider">Applied codes</span>
              <button onClick={() => setClickedCoding(null)} className="text-gray-400 hover:text-gray-600">
                <X size={12} />
              </button>
            </div>
            {clickedCoding.codings.map((c) => (
              <div key={c.id} className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-red-50 group">
                <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: c.code_color }} />
                <span className="text-sm text-gray-700 truncate flex-1">{c.code_name}</span>
                <button
                  onClick={() => { onDeleteCoding(c.id); setClickedCoding(null); }}
                  className="text-gray-300 group-hover:text-red-500 p-0.5"
                  title="Remove coding"
                >
                  <Trash2 size={12} />
                </button>
              </div>
            ))}
          </div>
        )}

        </>)}
      </div>
    </div>
  );
}
