import { Check } from 'lucide-react';
import type { Coding } from '../api';

/** Who made a coding, as a small label in coding lists: the coder's name for a hand coding,
 *  or the run's label in a dashed outline (like the dashed underline in the text) for a
 *  model coding, with the model's name on hover. A model coding the researcher accepted
 *  shows the coder's name and, in a solid outline, the run it came from. */
export function CoderLabel({ coding }: { coding: Coding }) {
  if (coding.run_id != null) {
    const label = coding.run_label ?? coding.coder ?? 'Model run';
    const model = coding.run_model ? `Model: ${coding.run_model}` : 'Model coding';
    return (
      <span
        className="text-[10px] leading-4 px-1 rounded border border-dashed border-gray-300 text-gray-500 shrink-0 max-w-[9rem] truncate"
        title={`${model} (model run “${label}”)`}
        data-model-label
      >
        {label}
      </span>
    );
  }
  const coder = coding.coder
    ? <span className="text-[10px] text-gray-400 shrink-0" title="Coder">{coding.coder}</span>
    : null;
  if (coding.accepted_from_run_id == null) return coder;
  const run = coding.accepted_from_run_label ?? 'a model run';
  const model = coding.accepted_from_run_model ? `, model ${coding.accepted_from_run_model}` : '';
  return (
    <>
      {coder}
      <span
        className="text-[10px] leading-4 px-1 rounded border border-gray-200 text-gray-500 shrink-0 max-w-[8rem] truncate inline-flex items-center gap-0.5"
        title={`Suggested by model run “${run}”${model}; accepted${coding.coder ? ` by ${coding.coder}` : ''}`}
        data-accepted-label
      >
        <Check size={9} className="shrink-0 text-green-600" />
        <span className="truncate">{run}</span>
      </span>
    </>
  );
}
