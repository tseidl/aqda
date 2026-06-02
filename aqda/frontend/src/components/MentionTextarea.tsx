import { useMemo, useRef, useState, type ChangeEvent, type MouseEvent, type ReactNode } from 'react';
import type { MentionCandidate } from './mentions';

/** Render text with `@Label` mentions underlined/coloured. Mention styling only
 *  changes colour + underline (never font metrics), so this can sit behind a
 *  transparent textarea as a pixel-aligned highlight backdrop. */
function renderHighlighted(text: string, labels: string[]): ReactNode[] {
  const sorted = [...labels].sort((a, b) => b.length - a.length); // longest match wins
  const nodes: ReactNode[] = [];
  let i = 0;
  let plainStart = 0;
  let key = 0;
  const flush = (end: number) => { if (end > plainStart) nodes.push(text.slice(plainStart, end)); };
  while (i < text.length) {
    if (text[i] === '@' && (i === 0 || /\s/.test(text[i - 1]))) {
      const rest = text.slice(i + 1);
      const hit = sorted.find((l) => l && rest.startsWith(l));
      if (hit) {
        flush(i);
        nodes.push(
          <span key={key++} className="text-indigo-600 underline decoration-indigo-300">
            {'@' + hit}
          </span>,
        );
        i += 1 + hit.length;
        plainStart = i;
        continue;
      }
    }
    i++;
  }
  flush(text.length);
  nodes.push(String.fromCharCode(0x200b)); // zero-width space keeps backdrop height >= textarea on a trailing newline
  return nodes;
}

/** Render plain-text content with @Label mentions as clickable chips that jump. */
function renderMentionNodes(
  text: string,
  candidates: MentionCandidate[],
  onJump: (c: MentionCandidate) => void,
): ReactNode[] {
  const sorted = [...candidates].sort((a, b) => b.label.length - a.label.length);
  const nodes: ReactNode[] = [];
  let i = 0;
  let plainStart = 0;
  let key = 0;
  const flush = (end: number) => { if (end > plainStart) nodes.push(text.slice(plainStart, end)); };
  while (i < text.length) {
    if (text[i] === '@' && (i === 0 || /\s/.test(text[i - 1]))) {
      const rest = text.slice(i + 1);
      const hit = sorted.find((c) => c.label && rest.startsWith(c.label));
      if (hit) {
        flush(i);
        nodes.push(
          <span
            key={key++}
            role="link"
            tabIndex={0}
            onClick={(e) => { e.stopPropagation(); onJump(hit); }}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); onJump(hit); } }}
            className="text-indigo-600 underline decoration-indigo-300 cursor-pointer rounded-sm hover:bg-indigo-50"
            title={`Go to ${hit.kind}: ${hit.label}`}
          >
            {'@' + hit.label}
          </span>,
        );
        i += 1 + hit.label.length;
        plainStart = i;
        continue;
      }
    }
    i++;
  }
  flush(text.length);
  return nodes;
}

/** Read view of mention content: @Label chips are clickable; clicking elsewhere calls onEdit. */
export function MentionView({ value, candidates, onJump, onEdit, className = '', placeholder = '' }: {
  value: string;
  candidates: MentionCandidate[];
  onJump: (c: MentionCandidate) => void;
  onEdit: () => void;
  className?: string;
  placeholder?: string;
}) {
  return (
    <div onClick={onEdit} className={`cursor-text whitespace-pre-wrap break-words ${className}`}>
      {value.trim()
        ? renderMentionNodes(value, candidates, onJump)
        : <span className="text-gray-400">{placeholder}</span>}
    </div>
  );
}

interface Props {
  value: string;
  onChange: (value: string) => void;
  candidates: MentionCandidate[];
  placeholder?: string;
  rows?: number;
  autoFocus?: boolean;
  /** Fill the parent's height (for flex layouts) instead of sizing by rows. */
  fill?: boolean;
  /** Text-metric classes applied to BOTH the textarea and the highlight backdrop. */
  textClassName?: string;
  /** Container classes (border, background, sizing). */
  className?: string;
  /** Forwarded to the textarea (e.g. to preserve a document text selection). */
  onTextareaMouseDown?: (e: MouseEvent) => void;
  /** Called when the textarea loses focus (used to exit an edit mode). */
  onBlur?: () => void;
}

/** Textarea with @-mention autocomplete over codes + memos and a live
 *  highlight of inserted mentions. */
export function MentionTextarea({
  value, onChange, candidates, placeholder, rows, autoFocus, fill,
  textClassName = '', className = '', onTextareaMouseDown, onBlur,
}: Props) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const backdropRef = useRef<HTMLDivElement>(null);
  const [mention, setMention] = useState<{ query: string; start: number } | null>(null);
  const labels = useMemo(() => candidates.map((c) => c.label).filter(Boolean), [candidates]);

  const detect = (v: string, caret: number) => {
    const m = v.slice(0, caret).match(/(?:^|\s)@([^\s@]*)$/);
    setMention(m ? { query: m[1], start: caret - m[1].length - 1 } : null);
  };

  const handleChange = (e: ChangeEvent<HTMLTextAreaElement>) => {
    onChange(e.target.value);
    detect(e.target.value, e.target.selectionStart ?? e.target.value.length);
  };

  const insert = (c: MentionCandidate) => {
    if (!mention) return;
    const before = value.slice(0, mention.start);
    const after = value.slice(mention.start + 1 + mention.query.length);
    const next = `${before}@${c.label} ${after}`;
    const caretPos = (before + '@' + c.label + ' ').length;
    onChange(next);
    setMention(null);
    requestAnimationFrame(() => {
      const ta = taRef.current;
      if (ta) { ta.focus(); ta.setSelectionRange(caretPos, caretPos); }
    });
  };

  const matches = mention
    ? candidates.filter((c) => c.label.toLowerCase().includes(mention.query.toLowerCase())).slice(0, 7)
    : [];

  const syncScroll = () => {
    if (backdropRef.current && taRef.current) {
      backdropRef.current.scrollTop = taRef.current.scrollTop;
      backdropRef.current.scrollLeft = taRef.current.scrollLeft;
    }
  };

  return (
    <div className={`relative ${fill ? 'flex' : ''} ${className}`}>
      <div
        ref={backdropRef}
        aria-hidden
        className={`absolute inset-0 overflow-hidden whitespace-pre-wrap break-words pointer-events-none text-gray-800 ${textClassName}`}
        style={{ scrollbarGutter: 'stable' }}
      >
        {renderHighlighted(value, labels)}
      </div>
      <textarea
        ref={taRef}
        value={value}
        onChange={handleChange}
        onScroll={syncScroll}
        onMouseDown={onTextareaMouseDown}
        onBlur={onBlur}
        onKeyDown={(e) => { if (e.key === 'Escape' && mention) { e.stopPropagation(); setMention(null); } }}
        placeholder={placeholder}
        rows={fill ? undefined : rows}
        autoFocus={autoFocus}
        className={`relative ${fill ? 'flex-1' : 'w-full'} bg-transparent resize-none focus:outline-none whitespace-pre-wrap break-words ${textClassName}`}
        style={{ color: 'transparent', caretColor: '#111827', scrollbarGutter: 'stable' }}
      />
      {mention && matches.length > 0 && (
        <div className={`absolute left-1 right-1 z-50 max-h-44 overflow-auto bg-white border border-gray-200 rounded-md shadow-lg ${fill ? 'bottom-1' : 'top-full mt-1'}`}>
          {matches.map((c) => (
            <button
              key={c.id}
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => insert(c)}
              className="w-full text-left px-2 py-1.5 text-sm hover:bg-indigo-50 flex items-center gap-2"
            >
              {c.kind === 'code' ? (
                <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: c.color ?? '#6366f1' }} />
              ) : (
                <span className="w-2.5 h-2.5 rounded-sm shrink-0 bg-amber-400" />
              )}
              <span className="truncate">{c.label}</span>
              <span className="ml-auto text-[10px] text-gray-300 shrink-0">{c.kind}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
