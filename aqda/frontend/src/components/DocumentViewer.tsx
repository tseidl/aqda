import { useState, useRef, useCallback, useMemo, useEffect, useLayoutEffect } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { X, Tag, Sparkles, ChevronDown, ChevronRight, ChevronUp, Plus, Trash2, Loader2, StickyNote, EyeOff, Search, Check, Minus as MinusIcon, Plus as PlusIcon } from 'lucide-react';
import { codes as codesApi, codings as codingsApi, documents as docsApi, type Document, type Coding, type Code, type Memo } from '../api';
import { MentionTextarea } from './MentionTextarea';
import { CodingAnswers, CodingFieldsForm } from './CodingFieldsForm';
import { ActionError, invalidateAnswers, readableError, useCodingFieldsState } from './codingFields';
import { buildMentionCandidates } from './mentions';
import { useStoredState } from './storedState';
import { CoderLabel } from './CoderLabel';
import { CoderVisibilityControl } from './CoderVisibilityControl';
import { codingLayerKey, sameAnswers, useCoderLayers, useCoderName, useHiddenLayers, useModelRuns } from './coderLayers';

interface Props {
  document: Document;
  codings: Coding[];
  codes: Code[];
  memos: Memo[];
  selectedCodeId: number | null;
  /** Apply a code with one click. `explainRefusal` may take over the message when the server
   *  refuses the coding; it returns true when it did. */
  onApplyCode: (
    codeId: number, startPos: number, endPos: number, text: string,
    explainRefusal?: (error: unknown) => boolean,
  ) => void;
  onDeleteCoding: (id: number) => void;
  onSelectCode: (id: number) => void;
  onAddMemo?: (documentId: number, startPos: number, endPos: number, text: string, title?: string, content?: string) => void;
  /** Open a memo (from the mark its passage carries in the text). */
  onOpenMemo?: (memoId: number) => void;
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

/** UTF-16 offset of a DOM boundary point within `root`'s text. Points outside `root`
 *  (a triple-click or a drag past the last line) clamp to the start or end of the text,
 *  and points on elements rather than text nodes are measured like any other. */
function textOffset(root: HTMLElement, container: Node, offset: number): number {
  const whole = document.createRange();
  whole.selectNodeContents(root);
  if (!root.contains(container)) {
    const point = document.createRange();
    point.setStart(container, offset);
    return point.compareBoundaryPoints(Range.START_TO_START, whole) <= 0
      ? 0
      : (root.textContent?.length ?? 0);
  }
  whole.setEnd(container, offset);
  return whole.toString().length;
}

/** The DOM range covering UTF-16 offsets [start, end) of root's text (the inverse of textOffset). */
function rangeFromOffsets(root: HTMLElement, start: number, end: number): Range | null {
  const range = document.createRange();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let seen = 0;
  let startSet = false;
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    const len = node.data.length;
    if (!startSet && start <= seen + len) { range.setStart(node, start - seen); startSet = true; }
    if (startSet && end <= seen + len) { range.setEnd(node, end - seen); return range; }
    seen += len;
  }
  return null;
}

// Focus a popup field without scrolling the document to it (a stable ref callback, so it
// runs once when the field appears).
const focusWithoutScroll = (el: HTMLElement | null) => el?.focus({ preventScroll: true });

// Whether the server refused a coding because the same coding already exists.
const isDuplicateRefusal = (error: unknown) => error instanceof Error && /^409:/.test(error.message);

// Name of the CSS highlight that keeps the passage being coded marked (see index.css).
const PENDING_HIGHLIGHT = 'aqda-pending-passage';

/** Lower-case text for case-insensitive find, with maps back to original offsets.
 *  Lower-casing can lengthen text ("İ" becomes "i" plus a combining dot), so match
 *  positions in the folded text are mapped back rather than reused: a match start inside
 *  such a character moves to its start, a match end inside it moves to its end. */
function foldCase(text: string): {
  folded: string;
  startOf: (index: number) => number;
  endOf: (index: number) => number;
} {
  const lowered = text.toLowerCase();
  if (lowered.length === text.length) {
    return { folded: lowered, startOf: (index) => index, endOf: (index) => index };
  }
  let folded = '';
  const charStart: number[] = [];  // per folded unit: where its original character starts
  const charEnd: number[] = [];  // per folded unit: where its original character ends
  const firstUnit: boolean[] = [];  // per folded unit: is it the first unit of that character
  for (let i = 0; i < text.length;) {
    const char = String.fromCodePoint(text.codePointAt(i)!);
    const lower = char.toLowerCase();
    for (let k = 0; k < lower.length; k++) {
      charStart.push(i);
      charEnd.push(i + char.length);
      firstUnit.push(k === 0);
    }
    folded += lower;
    i += char.length;
  }
  return {
    folded,
    startOf: (index) => (index >= folded.length ? text.length : charStart[index]),
    endOf: (index) => {
      if (index >= folded.length) return text.length;
      return firstUnit[index] ? charStart[index] : charEnd[index];
    },
  };
}

export function DocumentViewer({ document: doc, codings: allCodings, codes, memos, selectedCodeId, onApplyCode, onDeleteCoding, onAddMemo, onOpenMemo, highlightRange, onHighlightClear }: Props) {
  const contentRef = useRef<HTMLDivElement>(null);
  const textContentRef = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();
  const [selection, setSelection] = useState<TextSelection | null>(null);
  // Display preferences are remembered: the viewer is re-created for each newly opened document.
  const [docFontSize, setDocFontSize] = useStoredState('aqda.docFontSize', 14);
  // What a click on a passage found there: its codings and the memos written on it.
  const [clickedCoding, setClickedCoding] = useState<{
    codings: Coding[]; memos: Memo[]; rect: { top: number; left: number };
  } | null>(null);
  const [showNewCodeInput, setShowNewCodeInput] = useState(false);
  const [newCodeName, setNewCodeName] = useState('');
  const [newCodeDesc, setNewCodeDesc] = useState('');
  const [codeFilter, setCodeFilter] = useState('');
  const [showVariables, setShowVariables] = useStoredState('aqda.showVariables', false);
  const [newVarKey, setNewVarKey] = useState('');
  const [newVarValue, setNewVarValue] = useState('');
  const [showAddVar, setShowAddVar] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [transcribeError, setTranscribeError] = useState<string | null>(null);
  const [editingTag, setEditingTag] = useState(false);
  const [tagValue, setTagValue] = useState('');
  const [memoDraft, setMemoDraft] = useState<{ title: string; content: string } | null>(null);
  const [newCodeError, setNewCodeError] = useState<string | null>(null);
  // Coding fields: when the project defines any, choosing a code opens a step that asks for
  // the answers (pendingCode) before the coding is saved; otherwise one click applies it. No
  // code is applied before the list is known (`fieldsReady`), so the step is never skipped.
  const {
    fields: codingFields, ready: fieldsReady, error: fieldsError, retry: retryFields,
  } = useCodingFieldsState(doc.project_id);
  const [pendingCode, setPendingCode] = useState<{ id: number; name: string; color: string } | null>(null);
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

  // Who coded what: hand coders and model runs, each of which can be hidden in this view.
  const { runs: runList, loaded: runsLoaded } = useModelRuns(doc.project_id);
  const layers = useCoderLayers(doc.project_id, runList);
  const { hidden: hiddenLayers, toggle: toggleLayer, showAll: showAllLayers } = useHiddenLayers(doc.project_id);
  const cachedCoderName = useCoderName();
  // The coder name the server stamped on this viewer's last new coding. It counts as the
  // current name until the cached settings change, so nothing rests on a stale name.
  const [stampedCoder, setStampedCoder] = useState<{ cached: string; server: string } | null>(null);
  const coderName = stampedCoder && stampedCoder.cached === cachedCoderName ? stampedCoder.server : cachedCoderName;
  // Remember the coder name of a coding the server just created; refresh the settings if
  // they were out of date.
  const noteCreatedCoder = useCallback((created: Coding) => {
    const server = created.coder ?? '';
    setStampedCoder({ cached: cachedCoderName, server });
    if (server !== cachedCoderName) queryClient.invalidateQueries({ queryKey: ['settings'] });
  }, [cachedCoderName, queryClient]);
  const runUuids = useMemo(() => new Map(runList.map((run) => [run.id, run.uuid])), [runList]);
  // The codings drawn and listed here: those of hidden coders and runs are left out. Until
  // the runs are known, model codings stay out if any run is hidden, so they never flash up.
  const codings = useMemo(() => {
    if (!hiddenLayers.size) return allCodings;
    const runHidden = [...hiddenLayers].some((key) => key.startsWith('run:'));
    return allCodings.filter((c) =>
      (runsLoaded || c.run_id == null || !runHidden) && !hiddenLayers.has(codingLayerKey(c, runUuids)));
  }, [allCodings, hiddenLayers, runUuids, runsLoaded]);
  const hiddenInDocument = allCodings.length - codings.length;

  // Whether the researcher's own coding with this code, passage, and answers exists but is
  // hidden in this view: then the server's duplicate refusal needs explaining.
  const hiddenOwnDuplicate = useCallback(
    (codeId: number, start: number, end: number, answers: Record<string, string>) => {
      const shown = new Set(codings.map((c) => c.id));
      return allCodings.some((c) =>
        !shown.has(c.id) && c.run_id == null && c.code_id === codeId && c.start_pos === start &&
        c.end_pos === end && (c.coder ?? '') === coderName && sameAnswers(c.fields, answers));
    },
    [allCodings, codings, coderName],
  );
  const hiddenDuplicateMessage = codingFields.length > 0
    ? 'This code is already applied to this passage with these answers; it is there but hidden in this view.'
    : 'This code is already applied to exactly this passage; it is there but hidden in this view.';
  // Such an explanation after a one-click coding, shown above the text while anything is hidden.
  const [hiddenRefusal, setHiddenRefusal] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  // "Accept" a model coding: it becomes the researcher's own coding (one coding, not two)
  // and remembers the run it came from. Not offered when the researcher already coded the
  // passage the same way independently.
  const [acceptingId, setAcceptingId] = useState<number | null>(null);
  const [acceptError, setAcceptError] = useState<{ id: number; message: string } | null>(null);
  const codedItMyself = useCallback(
    (model: Coding) => allCodings.some((c) =>
      c.run_id == null && c.code_id === model.code_id && c.start_pos === model.start_pos &&
      c.end_pos === model.end_pos && (c.coder ?? '') === coderName && sameAnswers(c.fields, model.fields)),
    [allCodings, coderName],
  );
  const acceptCoding = useCallback(async (model: Coding) => {
    setAcceptingId(model.id);
    setAcceptError(null);
    try {
      noteCreatedCoder(await codingsApi.accept(model.id));
    } catch (error) {
      setAcceptError({ id: model.id, message: readableError(error) });
    }
    // The button stays busy until the list shows the change, so it cannot be sent twice.
    try {
      await Promise.all([
        invalidateAnswers(queryClient, doc.project_id),
        queryClient.invalidateQueries({ queryKey: ['model-runs', doc.project_id] }),
      ]);
    } finally {
      setAcceptingId(null);
    }
  }, [queryClient, doc.project_id, noteCreatedCoder]);

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
    const { folded: hay, startOf, endOf } = foldCase(codeableText);
    const needle = foldCase(findQuery).folded;
    const out: { start: number; end: number }[] = [];
    let idx = hay.indexOf(needle);
    while (idx !== -1 && out.length < 5000) {
      out.push({
        start: utf16ToCodePoint(offsetIndex, startOf(idx)),
        end: utf16ToCodePoint(offsetIndex, endOf(idx + needle.length)),
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

  // Memos written on a passage of this document. Their passages are marked in the text.
  const passageMemos = useMemo(
    () => (hasSearchableText
      ? memos.filter((m) => m.document_id === doc.id && m.start_pos != null && m.end_pos != null && m.end_pos > m.start_pos)
      : []),
    [memos, doc.id, hasSearchableText],
  );

  // Build rendered content with highlight spans. Memo passages only add segment boundaries
  // and a CSS class: the text itself stays as it is, so offsets are computed as before.
  const renderedContent = useMemo(() => {
    const text = codeableText;
    if (codings.length === 0 && !activeHighlight && passageMemos.length === 0) {
      return [{ text, codings: [] as Coding[], memos: [] as Memo[], highlighted: false }];
    }

    type Event = { pos: number; type: 'start' | 'end'; coding?: Coding; memo?: Memo; highlight?: boolean };
    const events: Event[] = [];
    for (const c of codings) {
      events.push({ pos: c.start_pos, type: 'start', coding: c });
      events.push({ pos: c.end_pos, type: 'end', coding: c });
    }
    for (const m of passageMemos) {
      events.push({ pos: m.start_pos!, type: 'start', memo: m });
      events.push({ pos: m.end_pos!, type: 'end', memo: m });
    }
    if (activeHighlight) {
      events.push({ pos: activeHighlight.start, type: 'start', highlight: true });
      events.push({ pos: activeHighlight.end, type: 'end', highlight: true });
    }
    events.sort((a, b) => a.pos - b.pos || (a.type === 'end' ? -1 : 1));

    const segments: { text: string; codings: Coding[]; memos: Memo[]; highlighted: boolean }[] = [];
    const active = new Set<Coding>();
    const activeMemos = new Set<Memo>();
    let isHighlighted = false;
    let lastPos = 0;

    for (const ev of events) {
      const jsPos = codePointToUtf16(offsetIndex, ev.pos);
      if (jsPos > lastPos) {
        segments.push({ text: text.slice(lastPos, jsPos), codings: [...active], memos: [...activeMemos], highlighted: isHighlighted });
      }
      if (ev.highlight) {
        isHighlighted = ev.type === 'start';
      } else if (ev.coding) {
        if (ev.type === 'start') active.add(ev.coding);
        else active.delete(ev.coding);
      } else if (ev.memo) {
        if (ev.type === 'start') activeMemos.add(ev.memo);
        else activeMemos.delete(ev.memo);
      }
      lastPos = jsPos;
    }
    if (lastPos < text.length) {
      segments.push({ text: text.slice(lastPos), codings: [], memos: [], highlighted: false });
    }

    return segments;
  }, [codeableText, codings, passageMemos, activeHighlight, offsetIndex]);

  const firstHighlightIdx = useMemo(
    () => renderedContent.findIndex((seg) => seg.highlighted),
    [renderedContent],
  );

  // Save a memo typed in the selection popup instead of dropping it. Only Cancel and the
  // close button discard a draft; an untouched draft is not saved.
  const keepMemoDraft = useCallback(
    (documentId: number, sel: TextSelection | null, draft: { title: string; content: string } | null) => {
      if (!sel || !draft || !onAddMemo) return;
      if (!draft.title.trim() && !draft.content.trim()) return;
      onAddMemo(documentId, sel.start, sel.end, sel.text, draft.title, draft.content);
    },
    [onAddMemo],
  );

  // Close the selection popup, keeping any memo typed in it.
  const closeMemoDraft = useCallback(() => {
    keepMemoDraft(doc.id, selection, memoDraft);
    setMemoDraft(null);
  }, [keepMemoDraft, doc.id, selection, memoDraft]);

  // The popup state as last rendered, for saving a draft when the document changes.
  const popupState = useRef({ selection, memoDraft, keepMemoDraft });
  useEffect(() => { popupState.current = { selection, memoDraft, keepMemoDraft }; });

  // Switching documents (or leaving the project) closes the popup: its passage belongs to the
  // previous document. A memo typed in it is saved to that document.
  useEffect(() => () => {
    const { selection: sel, memoDraft: draft, keepMemoDraft: keep } = popupState.current;
    keep(doc.id, sel, draft);
    setSelection(null);
    setMemoDraft(null);
    setShowNewCodeInput(false);
    setPendingCode(null);
  }, [doc.id]);

  // Handle text selection or click on coded passage
  const handleMouseUp = useCallback(() => {
    // Skip if the click was on a popup (buttons, etc.)
    if (popupClickRef.current) { popupClickRef.current = false; return; }

    const sel = window.getSelection();
    if (!sel || !contentRef.current || !textContentRef.current) return;

    // Text was selected — show apply-code popup
    if (!sel.isCollapsed) {
      setClickedCoding(null);
      closeMemoDraft();
      const range = sel.getRangeAt(0);
      const root = textContentRef.current;
      if (!range.intersectsNode(root)) return;
      let rawStart = textOffset(root, range.startContainer, range.startOffset);
      let rawEnd = textOffset(root, range.endContainer, range.endOffset);
      // Surrounding whitespace (a triple-click includes the line break) is not part of the passage.
      while (rawStart < rawEnd && /\s/.test(codeableText[rawStart])) rawStart++;
      while (rawEnd > rawStart && /\s/.test(codeableText[rawEnd - 1])) rawEnd--;

      if (rawEnd <= rawStart) { setSelection(null); return; }
      const text = codeableText.slice(rawStart, rawEnd);
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
      return;
    }

    // No selection — check if clicked on a coded passage or on a passage with a memo
    if (sel.anchorNode && textContentRef.current.contains(sel.anchorNode)) {
      const rawClickPos = textOffset(textContentRef.current, sel.anchorNode, sel.anchorOffset);
      const clickPos = utf16ToCodePoint(offsetIndex, rawClickPos);
      const overlapping = codings.filter(c => c.start_pos <= clickPos && c.end_pos > clickPos);
      const memosHere = passageMemos.filter(m => m.start_pos! <= clickPos && m.end_pos! > clickPos);
      if (overlapping.length > 0 || memosHere.length > 0) {
        const range = document.createRange();
        range.setStart(sel.anchorNode, sel.anchorOffset);
        range.setEnd(sel.anchorNode, sel.anchorOffset);
        const rect = range.getBoundingClientRect();
        const containerRect = contentRef.current.getBoundingClientRect();
        setClickedCoding({
          codings: overlapping,
          memos: memosHere,
          rect: {
            top: rect.top - containerRect.top + contentRef.current.scrollTop,
            left: Math.max(10, rect.left - containerRect.left),
          },
        });
        closeMemoDraft();
        setSelection(null);
        return;
      }
    }

    // Clicked on uncoded text — clear all popups
    closeMemoDraft();
    setClickedCoding(null);
    setSelection(null);
    setShowNewCodeInput(false);
  }, [codings, passageMemos, codeableText, offsetIndex, closeMemoDraft]);

  // The popup's code filter and any earlier error start empty for every new selection.
  useEffect(() => { setCodeFilter(''); setNewCodeError(null); setPendingCode(null); }, [selection]);

  // Keep the passage marked while its popup is open: focusing the popup's fields removes the
  // browser's own selection. A CSS highlight paints the range without changing the text.
  useEffect(() => {
    const root = textContentRef.current;
    if (!selection || !root || typeof CSS === 'undefined' || !CSS.highlights) return;
    const range = rangeFromOffsets(
      root,
      codePointToUtf16(offsetIndex, selection.start),
      codePointToUtf16(offsetIndex, selection.end),
    );
    if (!range) return;
    CSS.highlights.set(PENDING_HIGHLIGHT, new Highlight(range));
    return () => { CSS.highlights.delete(PENDING_HIGHLIGHT); };
  }, [selection, offsetIndex, renderedContent]);

  // Open the popup above the passage if it fits in view there, else below if it fits there,
  // else on the side with more room. Measured before paint, so the popup does not flicker.
  const popupRef = useRef<HTMLDivElement>(null);
  const [popupAbove, setPopupAbove] = useState(true);
  const memoOpen = memoDraft !== null;
  useLayoutEffect(() => {
    const popup = popupRef.current;
    const box = contentRef.current;
    if (!selection || !popup || !box) return;
    const height = popup.offsetHeight;
    const roomAbove = selection.rect.top - box.scrollTop - 10;
    const roomBelow = box.scrollTop + box.clientHeight - selection.rect.bottom - 8;
    setPopupAbove(roomAbove >= height || (roomBelow < height && roomAbove > roomBelow));
  }, [selection, memoOpen, showNewCodeInput, pendingCode]);

  // Apply code to selection; with coding fields, first ask for the answers.
  const applyCode = useCallback(
    (codeId: number, known?: Code) => {
      if (!selection || !fieldsReady) return;
      if (codingFields.length > 0) {
        const code = known ?? codeMap.get(codeId);
        setPendingCode({ id: codeId, name: code?.name ?? '', color: code?.color ?? '#6366f1' });
        setShowNewCodeInput(false);
        return;
      }
      const { start, end } = selection;
      // A refusal because the same coding exists, hidden in this view, is explained here.
      const explainRefusal = (error: unknown) => {
        if (!aliveRef.current || !isDuplicateRefusal(error) || !hiddenOwnDuplicate(codeId, start, end, {})) {
          return false;
        }
        setHiddenRefusal(hiddenDuplicateMessage);
        return true;
      };
      setHiddenRefusal(null);
      onApplyCode(codeId, start, end, selection.text, explainRefusal);
      setSelection(null);
      window.getSelection()?.removeAllRanges();
    },
    [selection, onApplyCode, codingFields.length, codeMap, fieldsReady, hiddenOwnDuplicate, hiddenDuplicateMessage]
  );

  // Save the pending code with its answers in one request. A refusal (the same coding
  // already exists, an answer of the wrong kind) rejects and is shown in the popup.
  const saveCodingWithAnswers = useCallback(
    async (answers: Record<string, string>) => {
      if (!selection || !pendingCode) return;
      let created: Coding;
      try {
        created = await codingsApi.create({
          document_id: doc.id,
          code_id: pendingCode.id,
          start_pos: selection.start,
          end_pos: selection.end,
          selected_text: selection.text,
          fields: answers,
        });
      } catch (error) {
        if (isDuplicateRefusal(error) && hiddenOwnDuplicate(pendingCode.id, selection.start, selection.end, answers)) {
          throw new ActionError(hiddenDuplicateMessage, { label: 'Show all', run: showAllLayers });
        }
        throw error;
      }
      noteCreatedCoder(created);
      invalidateAnswers(queryClient, doc.project_id);
      setSelection(null);
      window.getSelection()?.removeAllRanges();
    },
    [selection, pendingCode, doc.id, doc.project_id, queryClient, hiddenOwnDuplicate, hiddenDuplicateMessage, showAllLayers, noteCreatedCoder]
  );

  // Save the memo drafted in the selection popup
  const saveMemo = useCallback(() => {
    if (!selection || !onAddMemo) return;
    onAddMemo(doc.id, selection.start, selection.end, selection.text, memoDraft?.title ?? '', memoDraft?.content ?? '');
    setMemoDraft(null);
    setSelection(null);
    window.getSelection()?.removeAllRanges();
  }, [selection, onAddMemo, memoDraft, doc.id]);

  // Create a new code (with optional definition) and apply it to the selection
  const createNewCode = useCallback(async () => {
    if (!newCodeName.trim()) return;
    let newCode: Code;
    try {
      newCode = await codesApi.create({
        project_id: doc.project_id,
        name: newCodeName.trim(),
        description: newCodeDesc.trim() || undefined,
      });
    } catch (error) {
      setNewCodeError(error instanceof Error ? error.message.replace(/^\d{3}:\s*/, '') : String(error));
      return;
    }
    setNewCodeError(null);
    queryClient.invalidateQueries({ queryKey: ['codes', doc.project_id] });
    applyCode(newCode.id, newCode);
    setNewCodeName('');
    setNewCodeDesc('');
    setShowNewCodeInput(false);
  }, [newCodeName, newCodeDesc, doc.project_id, applyCode, queryClient]);

  // The codeable text with coding highlights; shared by text, PDF, and audio transcripts.
  const textBody = (
    <div ref={textContentRef} className="p-6 max-w-4xl mx-auto text-gray-800 whitespace-pre-wrap select-text" style={{ fontSize: `${docFontSize}px`, lineHeight: 1.8 }}>
      {renderedContent.map((seg, i) => {
        const isFirstHighlight = i === firstHighlightIdx;
        // A passage with a memo carries a dotted grey underline (see index.css).
        const hasMemo = seg.memos.length > 0;
        const memoNote = hasMemo
          ? `Memo: ${seg.memos.map((m) => m.title || 'Untitled memo').join(', ')}`
          : undefined;
        if (seg.codings.length === 0 && !seg.highlighted) {
          return hasMemo
            ? <span key={i} className="memo-passage" title={memoNote}>{seg.text}</span>
            : <span key={i}>{seg.text}</span>;
        }
        if (seg.codings.length === 0 && seg.highlighted) {
          return (
            <mark
              key={i}
              ref={isFirstHighlight ? (el) => { highlightRef.current = el; } : undefined}
              className={`ai-search-highlight${hasMemo ? ' memo-passage' : ''}`}
              title={memoNote}
              style={{ backgroundColor: 'rgba(120, 120, 120, 0.2)', borderBottom: '2px solid #9ca3af' }}
            >
              {seg.text}
            </mark>
          );
        }
        // A hand coding's colour leads where there is one; a passage coded only by model
        // runs gets a lighter fill and a dashed underline in its code's colour. All codings
        // show on hover.
        const hand = seg.codings.find((c) => c.run_id == null);
        const primary = hand ?? seg.codings[0];
        const modelOnly = !hand;
        const color = primary.code_color ?? '#6366f1';
        return (
          <mark
            key={i}
            ref={isFirstHighlight ? (el) => { highlightRef.current = el; } : undefined}
            data-color={color}
            data-model={modelOnly ? '' : undefined}
            className={[seg.highlighted ? 'ai-search-highlight' : '', hasMemo ? 'memo-passage' : ''].join(' ').trim() || undefined}
            style={{
              backgroundColor: seg.highlighted
                ? 'rgba(120, 120, 120, 0.25)'
                : color + (modelOnly ? '14' : '30'),
              borderBottom: `2px ${modelOnly && !seg.highlighted ? 'dashed' : 'solid'} ${seg.highlighted ? '#9ca3af' : color}`,
            }}
            title={seg.codings
              .map((c) => (c.run_id == null ? c.code_name : `${c.code_name} (model run “${c.run_label ?? c.coder}”)`))
              .join(', ') + (memoNote ? `\n${memoNote}` : '')}
          >
            {seg.text}
          </mark>
        );
      })}
    </div>
  );

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
            {doc.exclude_from_ai ? (
              <span
                className="text-xs flex items-center gap-1 text-gray-400"
                title="Excluded from AI features. Change this in the AI panel."
              >
                <EyeOff size={13} /> Excluded from AI
              </span>
            ) : null}
            <CoderVisibilityControl
              layers={layers}
              hidden={hiddenLayers}
              onToggle={toggleLayer}
              onShowAll={showAllLayers}
              hiddenInDocument={hiddenInDocument}
            />
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
              {/* Keyed by document too: the field keeps what was typed, and must never
                  carry one document's value over to the next document. */}
              {Object.entries(variables).map(([key, value]) => (
                <div key={`${doc.id}:${key}:${value}`} className="flex items-center gap-2 group">
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
        {/* A one-click coding refused because the same coding is hidden in this view */}
        {hiddenRefusal && hiddenLayers.size > 0 && (
          <div className="sticky top-0 z-40 flex justify-center pointer-events-none">
            <div
              className="pointer-events-auto m-2 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs text-amber-900 shadow-sm"
              role="alert"
              data-hidden-refusal
            >
              <span>{hiddenRefusal}</span>
              <button
                onClick={() => { showAllLayers(); setHiddenRefusal(null); }}
                className="font-medium text-indigo-600 hover:text-indigo-700 shrink-0"
              >
                Show all
              </button>
              <button onClick={() => setHiddenRefusal(null)} className="text-amber-700 hover:text-amber-900 shrink-0" title="Close">
                <X size={12} />
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
            {doc.transcript && textBody}
          </div>
        ) : (
          textBody
        )}

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
          return (
          <div
            ref={popupRef}
            className="absolute z-50 bg-white rounded-lg shadow-xl border border-gray-200 p-3 min-w-[220px] max-w-[280px]"
            style={!popupAbove ? {
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
                  placeholder="Title (optional)"
                  value={memoDraft.title}
                  onChange={(e) => setMemoDraft((d) => (d ? { ...d, title: e.target.value } : d))}
                  className="w-full px-2 py-1 border border-gray-200 rounded text-sm mb-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
                />
                <MentionTextarea
                  rows={3}
                  autoFocus
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
            ) : pendingCode ? (
              <div onMouseDown={(e) => { e.stopPropagation(); popupClickRef.current = true; }}>
                <div className="text-xs text-gray-500 mb-2 flex items-center justify-between">
                  <span className="flex items-center gap-1"><Tag size={12} /> Apply code</span>
                  <button onClick={() => setSelection(null)} className="text-gray-400 hover:text-gray-600" title="Close">
                    <X size={14} />
                  </button>
                </div>
                <CodingFieldsForm
                  key={pendingCode.id}
                  fields={codingFields}
                  heading={
                    <div className="flex items-center gap-2 px-2 py-1 mb-2 rounded bg-gray-50">
                      <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: pendingCode.color }} />
                      <span className="text-sm font-medium text-gray-700 truncate">{pendingCode.name}</span>
                    </div>
                  }
                  saveLabel="Save coding"
                  backLabel="Back"
                  autoFocus
                  onSave={saveCodingWithAnswers}
                  onBack={() => setPendingCode(null)}
                />
              </div>
            ) : (<>
            {/* Existing codings on this selection — with delete */}
            {overlapping.length > 0 && (
              <div className="mb-2 pb-2 border-b border-gray-100">
                <p className="text-[10px] text-gray-400 uppercase tracking-wider mb-1">Active codings</p>
                {overlapping.map((c) => (
                  <div key={c.id}>
                  <div className="flex items-center gap-2 px-2 py-1 rounded hover:bg-red-50 group">
                    <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: c.code_color }} />
                    <span className="text-sm text-gray-700 truncate flex-1">{c.code_name}</span>
                    <CoderLabel coding={c} />
                    <button
                      onClick={() => { onDeleteCoding(c.id); setSelection(null); }}
                      className="text-gray-300 group-hover:text-red-500 p-0.5"
                      title="Remove coding"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                  {/* Field answers (none in a project without coding fields) */}
                  <div className="pl-6 pr-2" onMouseDown={(e) => { e.stopPropagation(); popupClickRef.current = true; }}>
                    <CodingAnswers coding={c} fields={codingFields} projectId={doc.project_id} />
                  </div>
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

            {/* Until the coding fields are known, codes cannot be applied (the answers step
                must not be skipped); normally this is too brief to see. */}
            {!fieldsReady && (
              <p className="text-[11px] mb-1.5 px-0.5" data-fields-status>
                {fieldsError ? (
                  <span className="text-red-600">
                    AQDA could not load the coding fields, so codes cannot be applied yet. {fieldsError}{' '}
                    <button onClick={() => retryFields()} className="text-indigo-600 hover:text-indigo-700 font-medium">
                      Try again
                    </button>
                  </span>
                ) : (
                  <span className="text-gray-400 flex items-center gap-1">
                    <Loader2 size={11} className="animate-spin" /> Loading coding fields…
                  </span>
                )}
              </p>
            )}

            {/* Quick apply with selected code */}
            {selectedCodeId && (
              <button
                onClick={() => applyCode(selectedCodeId)}
                disabled={!fieldsReady}
                className="w-full text-left px-2 py-1.5 rounded text-sm hover:bg-gray-50 flex items-center gap-2 mb-1 font-medium disabled:opacity-50"
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
                ref={focusWithoutScroll}
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
                    disabled={!fieldsReady}
                    className="w-full text-left px-2 py-1 rounded text-sm hover:bg-gray-50 flex items-center gap-2 disabled:opacity-50"
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
                  ref={focusWithoutScroll}
                  placeholder="New code name"
                  value={newCodeName}
                  onChange={(e) => setNewCodeName(e.target.value)}
                  onMouseDown={(e) => { e.stopPropagation(); popupClickRef.current = true; }}
                  className="w-full px-2 py-1 border border-gray-200 rounded text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500"
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && newCodeName.trim() && fieldsReady) createNewCode();
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
                  disabled={!newCodeName.trim() || !fieldsReady}
                  className="w-full py-1 text-sm font-medium rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Create &amp; apply
                </button>
                {newCodeError && (
                  <p className="text-xs text-red-600">Could not create the code: {newCodeError}</p>
                )}
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
            className="absolute z-50 bg-white rounded-lg shadow-xl border border-gray-200 py-2 px-1 min-w-[180px] max-w-[420px]"
            style={{
              top: clickedCoding.rect.top + 20,
              left: Math.max(10, Math.min(clickedCoding.rect.left, (contentRef.current?.clientWidth ?? 480) - 430)),
            }}
            onMouseDown={(e) => { e.preventDefault(); popupClickRef.current = true; }}
          >
            <div className="px-2 pb-1.5 mb-1 border-b border-gray-100 flex items-center justify-between">
              <span className="text-[10px] text-gray-400 uppercase tracking-wider">
                {clickedCoding.codings.length > 0 ? 'Applied codes' : 'Memo on this passage'}
              </span>
              <button onClick={() => setClickedCoding(null)} className="text-gray-400 hover:text-gray-600">
                <X size={12} />
              </button>
            </div>
            {clickedCoding.codings.map((clicked) => {
              // The current version of the coding, so edited answers show at once; a coding
              // hidden meanwhile (by the Coders control) leaves the list.
              const c = codings.find((x) => x.id === clicked.id) ?? (allCodings.some((x) => x.id === clicked.id) ? null : clicked);
              if (!c) return null;
              const alsoMine = c.run_id != null && codedItMyself(c);
              // A model coding, or one accepted from a model run, says so in words on a
              // line of its own; a plain hand coding keeps the coder's name beside the code.
              const fromRun = c.run_id != null;
              const acceptedFrom = !fromRun && c.accepted_from_run_id != null;
              const runName = (fromRun ? c.run_label : c.accepted_from_run_label) ?? 'a model run';
              const runModel = fromRun ? c.run_model : c.accepted_from_run_model;
              return (
              <div key={c.id}>
              <div className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 group">
                <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: c.code_color }} />
                <span className="text-sm text-gray-700 truncate flex-1" title={c.code_name}>{c.code_name}</span>
                {!fromRun && !acceptedFrom && <CoderLabel coding={c} />}
                {c.run_id != null && (alsoMine ? (
                  <span
                    className="text-[11px] text-gray-400 shrink-0"
                    title="You coded this passage yourself with the same code and answers; your coding is listed separately"
                    data-also-mine
                  >
                    also yours
                  </span>
                ) : (
                  <button
                    onClick={() => acceptCoding(c)}
                    disabled={acceptingId !== null}
                    className="text-[11px] px-1.5 py-0.5 rounded border border-indigo-200 text-indigo-600 hover:bg-indigo-50 disabled:opacity-50 shrink-0"
                    title="Make this coding your own. It stays if the model run is removed, and it remembers which run suggested it."
                    data-accept
                  >
                    {acceptingId === c.id ? 'Accepting…' : 'Accept'}
                  </button>
                ))}
                <button
                  onClick={() => { onDeleteCoding(c.id); setClickedCoding(null); }}
                  className="text-gray-300 group-hover:text-red-500 p-0.5"
                  title="Remove coding"
                >
                  <Trash2 size={12} />
                </button>
              </div>
              {(fromRun || acceptedFrom) && (
                <p
                  className="pl-6 pr-2 pb-1 text-[11px] leading-snug text-gray-500 flex items-center gap-1"
                  title={runModel ? `Model: ${runModel}` : undefined}
                  data-provenance={fromRun ? 'suggested' : 'accepted'}
                >
                  {acceptedFrom && <Check size={11} className="shrink-0 text-green-600" />}
                  <span>
                    {fromRun
                      ? <>Suggested by model run “{runName}”, not accepted</>
                      : <>Accepted{c.coder ? ` by ${c.coder}` : ''}; suggested by model run “{runName}”</>}
                  </span>
                </p>
              )}
              {acceptError?.id === c.id && (
                <p className="px-2 text-[11px] text-red-600" role="alert">{acceptError.message}</p>
              )}
              {/* Field answers (none in a project without coding fields) */}
              <div className="pl-6 pr-2 max-w-[300px]" onMouseDown={(e) => { e.stopPropagation(); popupClickRef.current = true; }}>
                <CodingAnswers coding={c} fields={codingFields} projectId={doc.project_id} />
              </div>
              </div>
              );
            })}
            {/* Memos written on the clicked passage; a click opens the memo. */}
            {clickedCoding.memos.length > 0 && (
              <div className={clickedCoding.codings.length > 0 ? 'mt-1 pt-1 border-t border-gray-100' : ''} data-passage-memos>
                {clickedCoding.codings.length > 0 && (
                  <p className="px-2 py-1 text-[10px] text-gray-400 uppercase tracking-wider">Memo on this passage</p>
                )}
                {clickedCoding.memos.map((clicked) => {
                  // The memo as it is now (renamed meanwhile); a deleted memo leaves the list.
                  const memo = passageMemos.find((m) => m.id === clicked.id);
                  if (!memo) return null;
                  return (
                    <button
                      key={memo.id}
                      onClick={() => { onOpenMemo?.(memo.id); setClickedCoding(null); }}
                      className="w-full flex items-center gap-2 px-2 py-1.5 rounded text-left hover:bg-amber-50"
                      title="Open this memo"
                    >
                      <StickyNote size={12} className="text-amber-500 shrink-0" />
                      <span className="text-sm text-gray-700 truncate">{memo.title || 'Untitled memo'}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        )}

      </div>
    </div>
  );
}
