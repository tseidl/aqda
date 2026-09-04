import { useState, useRef, useCallback, useMemo, useEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { X, Tag, Sparkles, ChevronDown, ChevronRight, ChevronUp, Plus, Trash2, Loader2, StickyNote, BookMarked, Search, Minus as MinusIcon, Plus as PlusIcon } from 'lucide-react';
import { codes as codesApi, documents as docsApi, type Document, type Coding, type Code, type Memo } from '../api';
import { MentionTextarea } from './MentionTextarea';
import { buildMentionCandidates } from './mentions';

interface Props {
  document: Document;
  codings: Coding[];
  codes: Code[];
  memos: Memo[];
  selectedCodeId: number | null;
  onApplyCode: (codeId: number, startPos: number, endPos: number, text: string) => void;
  onDeleteCoding: (id: number) => void;
  onSelectCode: (id: number) => void;
  onAddMemo?: (startPos: number, endPos: number, text: string, title?: string, content?: string) => void;
  highlightRange?: { start: number; end: number } | null;
  onHighlightClear?: () => void;
}

interface TextSelection {
  start: number;
  end: number;
  text: string;
  rect: { top: number; bottom: number; left: number };
}

/** Codings store Unicode code-point offsets while the DOM and JS strings count UTF-16
 *  units. Only astral characters (emoji, some CJK) take two units, so recording where
 *  they sit lets both conversions run in O(log n) instead of re-scanning the whole
 *  text for every coding boundary, find match, and selection. */
interface OffsetIndex {
  /** Code-point index of each astral character, ascending. */
  astralCodePoints: number[];
  /** UTF-16 offset at which each astral character starts, ascending. */
  astralUtf16: number[];
  codePointLength: number;
  utf16Length: number;
}

function buildOffsetIndex(text: string): OffsetIndex {
  const astralCodePoints: number[] = [];
  const astralUtf16: number[] = [];
  let cp = 0;
  for (let i = 0; i < text.length; i++, cp++) {
    const unit = text.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff && i + 1 < text.length) {
      const next = text.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        astralCodePoints.push(cp);
        astralUtf16.push(i);
        i++;
      }
    }
  }
  return { astralCodePoints, astralUtf16, codePointLength: cp, utf16Length: text.length };
}

/** Number of entries in an ascending array that are strictly less than `value`. */
function countBelow(sorted: number[], value: number): number {
  let lo = 0;
  let hi = sorted.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid] < value) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function utf16ToCodePoint(index: OffsetIndex, offset: number): number {
  const o = Math.max(0, Math.min(offset, index.utf16Length));
  // A pair only collapses into one code point once both of its units lie before `o`.
  return o - countBelow(index.astralUtf16, o - 1);
}

function codePointToUtf16(index: OffsetIndex, offset: number): number {
  const cp = Math.max(0, Math.min(offset, index.codePointLength));
  return cp + countBelow(index.astralCodePoints, cp);
}

export function DocumentViewer({ document: doc, codings, codes, memos, selectedCodeId, onApplyCode, onDeleteCoding, onAddMemo, highlightRange, onHighlightClear }: Props) {
  const contentRef = useRef<HTMLDivElement>(null);
  const textContentRef = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();
  const [selection, setSelection] = useState<TextSelection | null>(null);
  const [docFontSize, setDocFontSize] = useState(14);
  const [clickedCoding, setClickedCoding] = useState<{
    codings: Coding[]; rect: { top: number; left: number };
  } | null>(null);
  const [showNewCodeInput, setShowNewCodeInput] = useState(false);
  const [newCodeName, setNewCodeName] = useState('');
  const [newCodeDesc, setNewCodeDesc] = useState('');
  const [codeFilter, setCodeFilter] = useState('');
  const [showVariables, setShowVariables] = useState(false);
  const [newVarKey, setNewVarKey] = useState('');
  const [newVarValue, setNewVarValue] = useState('');
  const [showAddVar, setShowAddVar] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [transcribeError, setTranscribeError] = useState<string | null>(null);
  const [editingTag, setEditingTag] = useState(false);
  const [tagValue, setTagValue] = useState('');
  const [memoDraft, setMemoDraft] = useState<{ title: string; content: string } | null>(null);
  const highlightRef = useRef<HTMLElement | null>(null);
  const popupClickRef = useRef(false);
  // In-document find (Cmd/Ctrl+F)
  const [findOpen, setFindOpen] = useState(false);
  const [findQuery, setFindQuery] = useState('');
  const [findIdx, setFindIdx] = useState(0);
  const findInputRef = useRef<HTMLInputElement>(null);

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

  const setLabelMut = useMutation({
    mutationFn: (label: string) => docsApi.update(doc.id, { label }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['document', doc.id] });
      queryClient.invalidateQueries({ queryKey: ['documents', doc.project_id] });
      setEditingTag(false);
    },
  });

  const setExcludeMut = useMutation({
    mutationFn: (exclude: boolean) => docsApi.update(doc.id, { exclude_from_ai: exclude }),
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

  const mentionCandidates = useMemo(() => buildMentionCandidates(codes, memos), [codes, memos]);

  // For audio docs with transcript, coding operates on the transcript text
  const codeableText = (doc.source_type === 'audio' && doc.transcript) ? doc.transcript : (doc.content ?? '');

  // Find/highlighting only make sense over real text (not image/base64 or untranscribed audio).
  const hasSearchableText =
    doc.source_type === 'text' || doc.source_type === 'pdf' ||
    (doc.source_type === 'audio' && !!doc.transcript);

  // One offset index per document; image/base64 content never needs one.
  const offsetIndex = useMemo(
    () => buildOffsetIndex(hasSearchableText ? codeableText : ''),
    [codeableText, hasSearchableText],
  );

  // In-document find: positions of every case-insensitive match of the query.
  const findMatches = useMemo(() => {
    if (!findOpen || !findQuery) return [] as { start: number; end: number }[];
    const hay = codeableText.toLowerCase();
    const needle = findQuery.toLowerCase();
    const out: { start: number; end: number }[] = [];
    let idx = hay.indexOf(needle);
    while (idx !== -1 && out.length < 5000) {
      out.push({
        start: utf16ToCodePoint(offsetIndex, idx),
        end: utf16ToCodePoint(offsetIndex, idx + needle.length),
      });
      idx = hay.indexOf(needle, idx + Math.max(1, needle.length));
    }
    return out;
  }, [findOpen, findQuery, codeableText, offsetIndex]);

  const safeFindIdx = findMatches.length ? Math.min(findIdx, findMatches.length - 1) : 0;

  // The highlight actually shown: the active find match takes precedence over the
  // parent-driven highlight (memo/AI navigation) while find is open.
  const activeHighlight = useMemo(() => {
    if (findOpen && findMatches.length > 0) {
      return findMatches[safeFindIdx];
    }
    return highlightRange ?? null;
  }, [findOpen, findMatches, safeFindIdx, highlightRange]);

  const gotoNextMatch = useCallback(() => {
    if (findMatches.length) setFindIdx((i) => (i + 1) % findMatches.length);
  }, [findMatches.length]);
  const gotoPrevMatch = useCallback(() => {
    if (findMatches.length) setFindIdx((i) => (i - 1 + findMatches.length) % findMatches.length);
  }, [findMatches.length]);

  // Cmd/Ctrl+F opens the in-document find bar; Escape closes it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') {
        if (!hasSearchableText) return;  // let the browser's own find handle image/audio docs
        // Don't steal the shortcut while the user is typing in a field (memo, code
        // name, doc search) — only hijack it for the document body.
        const el = window.document.activeElement as HTMLElement | null;
        if (el && el !== findInputRef.current &&
            (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) {
          return;
        }
        e.preventDefault();
        setFindOpen(true);
        requestAnimationFrame(() => { findInputRef.current?.focus(); findInputRef.current?.select(); });
      } else if (e.key === 'Escape' && findOpen) {
        setFindOpen(false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [findOpen, hasSearchableText]);

  // Reset find state when switching documents.
  useEffect(() => {
    setFindOpen(false);
    setFindQuery('');
    setFindIdx(0);
  }, [doc.id]);

  // Scroll the active find match into view as the user steps through matches.
  useEffect(() => {
    if (!findOpen || findMatches.length === 0) return;
    const raf = requestAnimationFrame(() => {
      highlightRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    return () => cancelAnimationFrame(raf);
  }, [findOpen, safeFindIdx, findMatches]);

  // Build rendered content with highlight spans
  const renderedContent = useMemo(() => {
    const text = codeableText;
    if (codings.length === 0 && !activeHighlight) return [{ text, codings: [] as Coding[], highlighted: false }];

    type Event = { pos: number; type: 'start' | 'end'; coding?: Coding; highlight?: boolean };
    const events: Event[] = [];
    for (const c of codings) {
      events.push({ pos: c.start_pos, type: 'start', coding: c });
      events.push({ pos: c.end_pos, type: 'end', coding: c });
    }
    if (activeHighlight) {
      events.push({ pos: activeHighlight.start, type: 'start', highlight: true });
      events.push({ pos: activeHighlight.end, type: 'end', highlight: true });
    }
    events.sort((a, b) => a.pos - b.pos || (a.type === 'end' ? -1 : 1));

    const segments: { text: string; codings: Coding[]; highlighted: boolean }[] = [];
    const active = new Set<Coding>();
    let isHighlighted = false;
    let lastPos = 0;

    for (const ev of events) {
      const jsPos = codePointToUtf16(offsetIndex, ev.pos);
      if (jsPos > lastPos) {
        segments.push({ text: text.slice(lastPos, jsPos), codings: [...active], highlighted: isHighlighted });
      }
      if (ev.highlight) {
        isHighlighted = ev.type === 'start';
      } else if (ev.coding) {
        if (ev.type === 'start') active.add(ev.coding);
        else active.delete(ev.coding);
      }
      lastPos = jsPos;
    }
    if (lastPos < text.length) {
      segments.push({ text: text.slice(lastPos), codings: [], highlighted: false });
    }

    return segments;
  }, [codeableText, codings, activeHighlight, offsetIndex]);

  const firstHighlightIdx = useMemo(
    () => renderedContent.findIndex((seg) => seg.highlighted),
    [renderedContent],
  );

  // Handle text selection or click on coded passage
  const handleMouseUp = useCallback(() => {
    // Skip if the click was on a popup (buttons, etc.)
    if (popupClickRef.current) { popupClickRef.current = false; return; }

    const sel = window.getSelection();
    if (!sel || !contentRef.current || !textContentRef.current) return;

    // Text was selected — show apply-code popup
    if (!sel.isCollapsed) {
      setClickedCoding(null);
      setMemoDraft(null);
      const range = sel.getRangeAt(0);
      if (!textContentRef.current.contains(range.startContainer) ||
          !textContentRef.current.contains(range.endContainer)) return;

      const walker = document.createTreeWalker(textContentRef.current, NodeFilter.SHOW_TEXT);
      let offset = 0;
      let rawStart = -1;
      let rawEnd = -1;

      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (node === range.startContainer) rawStart = offset + range.startOffset;
        if (node === range.endContainer) { rawEnd = offset + range.endOffset; break; }
        offset += node.textContent?.length ?? 0;
      }

      if (rawStart >= 0 && rawEnd > rawStart) {
        const text = codeableText.slice(rawStart, rawEnd);
        if (!text.trim()) { setSelection(null); return; }
        const startPos = utf16ToCodePoint(offsetIndex, rawStart);
        const endPos = utf16ToCodePoint(offsetIndex, rawEnd);
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
    if (sel.anchorNode && textContentRef.current.contains(sel.anchorNode)) {
      const walker = document.createTreeWalker(textContentRef.current, NodeFilter.SHOW_TEXT);
      let offset = 0;
      let rawClickPos = -1;
      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (node === sel.anchorNode) { rawClickPos = offset + sel.anchorOffset; break; }
        offset += node.textContent?.length ?? 0;
      }

      if (rawClickPos >= 0) {
        const clickPos = utf16ToCodePoint(offsetIndex, rawClickPos);
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
    setMemoDraft(null);
  }, [codings, codeableText, offsetIndex]);

  // The popup's code filter starts empty for every new selection.
  useEffect(() => { setCodeFilter(''); }, [selection]);

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

  // Save the memo drafted in the selection popup
  const saveMemo = useCallback(() => {
    if (!selection || !onAddMemo) return;
    onAddMemo(selection.start, selection.end, selection.text, memoDraft?.title ?? '', memoDraft?.content ?? '');
    setMemoDraft(null);
    setSelection(null);
    window.getSelection()?.removeAllRanges();
  }, [selection, onAddMemo, memoDraft]);

  // Create a new code (with optional definition) and apply it to the selection
  const createNewCode = useCallback(async () => {
    if (!newCodeName.trim()) return;
    const newCode = await codesApi.create({
      project_id: doc.project_id,
      name: newCodeName.trim(),
      description: newCodeDesc.trim() || undefined,
    });
    queryClient.invalidateQueries({ queryKey: ['codes', doc.project_id] });
    applyCode(newCode.id);
    setNewCodeName('');
    setNewCodeDesc('');
    setShowNewCodeInput(false);
  }, [newCodeName, newCodeDesc, doc.project_id, applyCode, queryClient]);

  return (
    <div className="h-full flex flex-col">
      {/* Document header */}
      <div className="border-b border-gray-200 bg-white shrink-0">
        <div className="px-4 py-2 flex items-center justify-between">
          <div className="flex items-center gap-2 min-w-0">
            <h2 className="text-sm font-medium text-gray-700 truncate">{doc.name}</h2>
            {/* User-set short tag */}
            {editingTag ? (
              <input
                autoFocus
                value={tagValue}
                maxLength={6}
                placeholder="TAG"
                onChange={(e) => setTagValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') setLabelMut.mutate(tagValue.trim());
                  else if (e.key === 'Escape') setEditingTag(false);
                }}
                onBlur={() => setLabelMut.mutate(tagValue.trim())}
                className="w-16 text-[10px] uppercase px-1 py-0.5 border border-indigo-300 rounded bg-white outline-none shrink-0"
              />
            ) : doc.label ? (
              <button
                onClick={() => { setTagValue(doc.label ?? ''); setEditingTag(true); }}
                className="text-[10px] px-1.5 py-0.5 rounded font-semibold shrink-0 bg-indigo-100 text-indigo-700 uppercase hover:bg-indigo-200"
                title="Edit tag"
              >
                {doc.label}
              </button>
            ) : (
              <button
                onClick={() => { setTagValue(''); setEditingTag(true); }}
                className="text-[10px] px-1.5 py-0.5 rounded text-gray-400 hover:text-indigo-600 hover:bg-gray-100 flex items-center gap-0.5 shrink-0"
                title="Add a short tag"
              >
                <Tag size={11} /> tag
              </button>
            )}
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
            <button
              onClick={() => setExcludeMut.mutate(!doc.exclude_from_ai)}
              className={`text-xs flex items-center gap-1 ${
                doc.exclude_from_ai ? 'text-amber-600 font-medium' : 'text-gray-400 hover:text-gray-600'
              }`}
              title="Reference material is excluded from AI search and code suggestions — useful for pre-coded examples / training documents."
            >
              <BookMarked size={13} />
              {doc.exclude_from_ai ? 'Reference (no AI)' : 'Mark as reference'}
            </button>
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
            {/* Char count: for audio show the transcript length; for images the
                "content" is a base64 data URI, so the count is meaningless — hide it. */}
            {doc.source_type !== 'image' && (
              <span className="text-xs text-gray-400">
                {(doc.source_type === 'audio'
                  ? (doc.transcript?.length ?? 0)
                  : (doc.content?.length ?? 0)
                ).toLocaleString()} chars
              </span>
            )}
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
        {/* In-document find bar (Cmd/Ctrl+F) */}
        {findOpen && hasSearchableText && (
          <div className="sticky top-0 z-40 flex justify-end pointer-events-none">
            <div className="pointer-events-auto m-2 flex items-center gap-1 bg-white border border-gray-200 rounded-lg shadow-md pl-2 pr-1 py-1">
              <Search size={13} className="text-gray-400 shrink-0" />
              <input
                ref={findInputRef}
                value={findQuery}
                onChange={(e) => { setFindQuery(e.target.value); setFindIdx(0); }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); if (e.shiftKey) gotoPrevMatch(); else gotoNextMatch(); }
                  else if (e.key === 'Escape') { e.preventDefault(); setFindOpen(false); }
                }}
                placeholder="Find in document"
                className="text-sm px-1 py-0.5 w-44 outline-none bg-transparent"
              />
              <span className="text-[11px] text-gray-400 tabular-nums w-12 text-center shrink-0">
                {findQuery ? `${findMatches.length ? safeFindIdx + 1 : 0}/${findMatches.length}` : ''}
              </span>
              <button
                onClick={gotoPrevMatch}
                disabled={findMatches.length === 0}
                className="p-1 text-gray-400 hover:text-gray-600 disabled:opacity-30 rounded hover:bg-gray-100"
                title="Previous match (Shift+Enter)"
              >
                <ChevronUp size={14} />
              </button>
              <button
                onClick={gotoNextMatch}
                disabled={findMatches.length === 0}
                className="p-1 text-gray-400 hover:text-gray-600 disabled:opacity-30 rounded hover:bg-gray-100"
                title="Next match (Enter)"
              >
                <ChevronDown size={14} />
              </button>
              <button
                onClick={() => setFindOpen(false)}
                className="p-1 text-gray-400 hover:text-gray-600 rounded hover:bg-gray-100"
                title="Close (Esc)"
              >
                <X size={14} />
              </button>
            </div>
          </div>
        )}
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
              <div ref={textContentRef} className="p-6 max-w-4xl mx-auto text-gray-800 whitespace-pre-wrap select-text" style={{ fontSize: `${docFontSize}px`, lineHeight: 1.8 }}>
                {renderedContent.map((seg, i) => {
                  const isFirstHighlight = i === firstHighlightIdx;
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
        <div ref={textContentRef} className="p-6 max-w-4xl mx-auto text-gray-800 whitespace-pre-wrap select-text" style={{ fontSize: `${docFontSize}px`, lineHeight: 1.8 }}>
          {renderedContent.map((seg, i) => {
            const isFirstHighlight = i === firstHighlightIdx;
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
          const needle = codeFilter.trim().toLowerCase();
          const filteredCodes = codes.filter(
            (c) => c.id !== selectedCodeId && (!needle || c.name.toLowerCase().includes(needle))
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
            {memoDraft ? (
              <div onMouseDown={(e) => { e.stopPropagation(); popupClickRef.current = true; }}>
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs text-gray-500 flex items-center gap-1"><StickyNote size={12} /> New memo</span>
                  <button onClick={() => setMemoDraft(null)} className="text-gray-400 hover:text-gray-600"><X size={14} /></button>
                </div>
                <p className="text-[11px] text-gray-400 italic mb-2 max-h-12 overflow-hidden">
                  “{selection.text.length > 90 ? selection.text.slice(0, 90) + '…' : selection.text}”
                </p>
                <input
                  autoFocus
                  placeholder="Title (optional)"
                  value={memoDraft.title}
                  onChange={(e) => setMemoDraft((d) => (d ? { ...d, title: e.target.value } : d))}
                  className="w-full px-2 py-1 border border-gray-200 rounded text-sm mb-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
                />
                <MentionTextarea
                  rows={3}
                  className="border border-gray-200 rounded mb-2 focus-within:ring-1 focus-within:ring-amber-400"
                  textClassName="px-2 py-1 text-sm"
                  value={memoDraft.content}
                  onChange={(v) => setMemoDraft((d) => (d ? { ...d, content: v } : d))}
                  candidates={mentionCandidates}
                  placeholder="Write your note… (type @ to reference a code or memo)"
                />
                <div className="flex gap-1">
                  <button onClick={saveMemo} className="flex-1 py-1 text-sm font-medium rounded bg-amber-500 text-white hover:bg-amber-600">Save memo</button>
                  <button onClick={() => setMemoDraft(null)} className="px-2 py-1 text-sm rounded bg-gray-100 text-gray-500 hover:bg-gray-200">Cancel</button>
                </div>
              </div>
            ) : (<>
            {/* Existing codings on this selection — with delete */}
            {overlapping.length > 0 && (
              <div className="mb-2 pb-2 border-b border-gray-100">
                <p className="text-[10px] text-gray-400 uppercase tracking-wider mb-1">Active codings</p>
                {overlapping.map((c) => (
                  <div key={c.id} className="flex items-center gap-2 px-2 py-1 rounded hover:bg-red-50 group">
                    <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: c.code_color }} />
                    <span className="text-sm text-gray-700 truncate flex-1">{c.code_name}</span>
                    {c.coder && <span className="text-[10px] text-gray-400 shrink-0" title="Coder">{c.coder}</span>}
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
              <button onClick={() => { setSelection(null); setShowNewCodeInput(false); }} className="text-gray-400 hover:text-gray-600">
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

            {/* Code list, narrowed by the filter box; Enter applies the first match */}
            {codes.length > 8 && (
              <input
                autoFocus
                value={codeFilter}
                placeholder="Type to filter codes…"
                onChange={(e) => setCodeFilter(e.target.value)}
                onMouseDown={(e) => { e.stopPropagation(); popupClickRef.current = true; }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    const first = filteredCodes[0];
                    if (first) applyCode(first.id);
                  } else if (e.key === 'Escape') {
                    setSelection(null);
                  }
                }}
                className="w-full px-2 py-1 mb-1 border border-gray-200 rounded text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            )}
            <div className="max-h-40 overflow-auto space-y-0.5">
              {filteredCodes
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
              <div className="mt-2 pt-2 border-t border-gray-100 space-y-1.5">
                <input
                  autoFocus
                  placeholder="New code name"
                  value={newCodeName}
                  onChange={(e) => setNewCodeName(e.target.value)}
                  onMouseDown={(e) => { e.stopPropagation(); popupClickRef.current = true; }}
                  className="w-full px-2 py-1 border border-gray-200 rounded text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500"
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && newCodeName.trim()) createNewCode();
                    if (e.key === 'Escape') { setShowNewCodeInput(false); setNewCodeName(''); setNewCodeDesc(''); }
                  }}
                />
                <MentionTextarea
                  rows={2}
                  className="border border-gray-200 rounded focus-within:ring-1 focus-within:ring-indigo-500"
                  textClassName="px-2 py-1 text-sm"
                  value={newCodeDesc}
                  onChange={setNewCodeDesc}
                  candidates={mentionCandidates}
                  placeholder="Definition (optional) — what does this code mean, when to apply it…"
                  onTextareaMouseDown={(e) => { e.stopPropagation(); popupClickRef.current = true; }}
                />
                <button
                  onClick={createNewCode}
                  disabled={!newCodeName.trim()}
                  className="w-full py-1 text-sm font-medium rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Create &amp; apply
                </button>
              </div>
            ) : (
              <button
                onClick={() => setShowNewCodeInput(true)}
                className="w-full text-left px-2 py-1.5 mt-1 rounded text-sm text-indigo-600 hover:bg-indigo-50 border-t border-gray-100 pt-2"
              >
                + New code
              </button>
            )}

            {/* Add a memo anchored to this passage */}
            {onAddMemo && (
              <button
                onClick={() => setMemoDraft({ title: '', content: '' })}
                className="w-full text-left px-2 py-1.5 mt-1 rounded text-sm text-amber-600 hover:bg-amber-50 flex items-center gap-1.5"
              >
                <StickyNote size={13} />
                Add memo
              </button>
            )}

            </>)}
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
                {c.coder && <span className="text-[10px] text-gray-400 shrink-0" title="Coder">{c.coder}</span>}
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
