import { useEffect, useRef, useState } from 'react';
import { ChevronDown, EyeOff, Users } from 'lucide-react';
import type { CoderLayer } from './coderLayers';

interface Props {
  layers: CoderLayer[];
  hidden: Set<string>;
  onToggle: (key: string) => void;
  onShowAll: () => void;
  /** Codings of the open document that are hidden right now. */
  hiddenInDocument: number;
}

/** A small sample of how a coder's passages are drawn in the text: a solid underline and
 *  fuller fill for hand codings, a dashed underline and lighter fill for a model run. It is
 *  grey because the colour in the text belongs to the code, not to the coder. */
function LineSample({ kind }: { kind: CoderLayer['kind'] }) {
  const model = kind === 'run';
  return (
    <span
      aria-hidden
      className="shrink-0 px-1 text-[11px] leading-4 text-gray-700"
      style={{
        backgroundColor: model ? 'rgba(107, 114, 128, 0.08)' : 'rgba(107, 114, 128, 0.19)',
        borderBottom: `2px ${model ? 'dashed' : 'solid'} #6b7280`,
      }}
      data-line-sample={kind}
    >
      Aa
    </span>
  );
}

/** Show or hide the codings of each hand coder and each model run in the document view.
 *  Everything is shown by default; while anything is hidden, the button says so in amber. */
export function CoderVisibilityControl({ layers, hidden, onToggle, onShowAll, hiddenInDocument }: Props) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const hiddenCount = layers.filter((layer) => hidden.has(layer.key)).length;

  // Close the list on a click elsewhere.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('mousedown', onDown);
    return () => window.removeEventListener('mousedown', onDown);
  }, [open]);

  const hand = layers.filter((layer) => layer.kind === 'hand');
  const models = layers.filter((layer) => layer.kind === 'run');
  // A project coded by one person only, with nothing hidden, looks as it always did. Any
  // model run brings the control, so its coded segments can be hidden before coding blind.
  if (layers.length < 2 && models.length === 0 && hiddenCount === 0) return null;
  const row = (layer: CoderLayer) => (
    <label
      key={layer.key}
      className="flex items-center gap-2 px-2 py-1 rounded text-xs text-gray-700 hover:bg-gray-50 cursor-pointer"
      title={layer.kind === 'run' && layer.model ? `Model: ${layer.model}` : undefined}
    >
      <input type="checkbox" checked={!hidden.has(layer.key)} onChange={() => onToggle(layer.key)} />
      <LineSample kind={layer.kind} />
      <span className={`truncate flex-1 ${hidden.has(layer.key) ? 'text-gray-400' : ''}`}>
        {layer.label}
      </span>
      <span className="text-[10px] text-gray-400 shrink-0">{layer.count}</span>
    </label>
  );

  return (
    <div className="relative" ref={boxRef}>
      <button
        onClick={() => setOpen(!open)}
        className={`text-xs flex items-center gap-1 rounded px-1.5 py-0.5 ${
          hiddenCount ? 'bg-amber-50 text-amber-800 hover:bg-amber-100' : 'text-gray-500 hover:text-gray-700'
        }`}
        title={hiddenCount
          ? `Some coded segments are hidden in this view${hiddenInDocument ? ` (${hiddenInDocument} in this document)` : ''}`
          : 'Show or hide coded segments by coder or model run'}
        data-coder-control
      >
        {hiddenCount ? <EyeOff size={12} /> : <Users size={12} />}
        Coders
        {hiddenCount > 0 && <span className="font-medium">· {hiddenCount} hidden</span>}
        <ChevronDown size={11} />
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 z-50 w-64 bg-white border border-gray-200 rounded-lg shadow-lg p-1.5">
          <p className="px-2 pt-0.5 pb-1 text-[11px] leading-snug text-gray-500">
            Show coded segments by coder. The colour is the code; the line shows who coded:
            solid by a person (coded or accepted), dashed by a model run and not yet accepted.
          </p>
          {hand.length > 0 && (
            <div>
              <p className="px-2 pt-1 text-[10px] text-gray-400 uppercase tracking-wider">Coded by hand</p>
              {hand.map(row)}
            </div>
          )}
          {models.length > 0 && (
            <div>
              <p className="px-2 pt-1.5 text-[10px] text-gray-400 uppercase tracking-wider">Model runs</p>
              {models.map(row)}
            </div>
          )}
          {hiddenCount > 0 && (
            <div className="mt-1 pt-1.5 border-t border-gray-100 px-2 pb-0.5 flex items-center justify-between gap-2">
              <span className="text-[11px] text-amber-700">
                {hiddenInDocument
                  ? `${hiddenInDocument} coded segment${hiddenInDocument === 1 ? '' : 's'} hidden in this document`
                  : 'Hidden coded segments are not drawn'}
              </span>
              <button onClick={onShowAll} className="text-[11px] text-indigo-600 hover:text-indigo-700 shrink-0">
                Show all
              </button>
            </div>
          )}
          <p className="px-2 pt-1 text-[10px] leading-snug text-gray-400">
            Hidden coded segments are left out of the text and its popups only; they stay in
            the project, the Segments tab, and exports.
          </p>
        </div>
      )}
    </div>
  );
}
