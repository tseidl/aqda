import { useMemo, useState, type ReactNode } from 'react';
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2 } from 'lucide-react';
import { codingFields as codingFieldsApi, codings as codingsApi, type Coding, type CodingField } from '../api';
import { ActionError, answerPieces, invalidateAnswers, lastTextAnswers, readableError } from './codingFields';

// Focus a field without scrolling the document to it (a stable ref callback, so it runs once
// when the field appears; the coding popup must not move the passage).
const focusWithoutScroll = (el: HTMLElement | null) => el?.focus({ preventScroll: true });

const INPUT_CLASS =
  'w-full px-2 py-1 border border-gray-200 rounded text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500';

/** Three states for a yes/no field: yes, no, or not answered (which differs from "no").
 *  Keys: y, n, and Backspace/Delete for "not answered"; Enter or Space chooses the focused
 *  answer (it does not save), and the arrow keys move between answers as in a radio group. */
function YesNoControl({ name, value, onChange, autoFocus }: {
  name: string; value: string; onChange: (value: string) => void; autoFocus?: boolean;
}) {
  const options: [string, string][] = [['1', 'Yes'], ['0', 'No'], ['', 'Not answered']];
  return (
    <div
      role="radiogroup"
      aria-label={name}
      className="flex rounded border border-gray-200 overflow-hidden"
      onKeyDown={(e) => {
        const key = e.key.toLowerCase();
        if (key === 'y') onChange('1');
        else if (key === 'n') onChange('0');
        else if (key === 'backspace' || key === 'delete') onChange('');
      }}
    >
      {options.map(([option, label], i) => (
        <button
          key={label}
          type="button"
          role="radio"
          aria-checked={value === option}
          ref={autoFocus && i === 0 ? focusWithoutScroll : undefined}
          onClick={() => onChange(option)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              // Choose this answer; preventDefault also keeps the form from saving.
              e.preventDefault();
              onChange(option);
            } else if (['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'].includes(e.key)) {
              e.preventDefault();
              const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1;
              const next = (i + step + options.length) % options.length;
              const buttons = e.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="radio"]');
              buttons?.[next]?.focus();
              onChange(options[next][0]);
            }
          }}
          // Space would otherwise click the button again on release.
          onKeyUp={(e) => { if (e.key === ' ') e.preventDefault(); }}
          className={`flex-1 whitespace-nowrap px-1.5 py-1 text-xs ${i > 0 ? 'border-l border-gray-200' : ''} ${
            value === option
              ? option === '' ? 'bg-gray-200 text-gray-700' : 'bg-indigo-600 text-white'
              : 'bg-white text-gray-600 hover:bg-gray-50'
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

/** A text field with earlier answers as suggestions. The answer given last in this session
 *  comes first; nothing is filled in until the researcher picks it. */
function TextFieldInput({ field, value, onChange, autoFocus }: {
  field: CodingField; value: string; onChange: (value: string) => void; autoFocus?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const needle = value.trim().toLowerCase();
  const { data: earlier = [] } = useQuery({
    queryKey: ['coding-field-suggestions', field.id, value.trim()],
    queryFn: () => codingFieldsApi.suggestions(field.id, value.trim()),
    enabled: open,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  });
  const last = lastTextAnswers.get(field.id);
  const items = useMemo(() => {
    const list: string[] = [];
    const fits = (s: string) => s.toLowerCase().includes(needle) && s !== value.trim();
    if (last && fits(last)) list.push(last);
    for (const s of earlier) if (fits(s) && !list.includes(s)) list.push(s);
    return list.slice(0, 8);
  }, [earlier, last, needle, value]);
  const shown = open && items.length > 0;

  // Take a suggestion and close the list.
  const pick = (item: string) => { onChange(item); setOpen(false); setActive(-1); };

  return (
    <div className="relative">
      <input
        ref={autoFocus ? focusWithoutScroll : undefined}
        value={value}
        onChange={(e) => { onChange(e.target.value); setOpen(true); setActive(-1); }}
        onFocus={() => setOpen(true)}
        onBlur={() => { setOpen(false); setActive(-1); }}
        onKeyDown={(e) => {
          if (!shown) return;
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            setActive((i) => (i + 1) % items.length);
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActive((i) => (i <= 0 ? items.length - 1 : i - 1));
          } else if (e.key === 'Enter' && active >= 0) {
            // Enter takes the highlighted suggestion; the form saves on the next Enter.
            e.preventDefault();
            pick(items[active]);
          } else if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            setOpen(false);
            setActive(-1);
          }
        }}
        role="combobox"
        aria-expanded={shown}
        aria-label={field.name}
        className={INPUT_CLASS}
      />
      {shown && (
        <ul
          role="listbox"
          className="absolute left-0 right-0 top-full mt-0.5 z-10 max-h-40 overflow-auto bg-white border border-gray-200 rounded shadow-lg py-0.5"
        >
          {items.map((item, i) => (
            <li
              key={item}
              role="option"
              aria-selected={i === active}
              // Keep the focus in the input while picking with the mouse.
              onMouseDown={(e) => { e.preventDefault(); pick(item); }}
              className={`px-2 py-1 text-sm cursor-pointer flex items-center gap-2 ${
                i === active ? 'bg-indigo-50 text-indigo-700' : 'text-gray-700 hover:bg-gray-50'
              }`}
            >
              <span className="truncate flex-1">{item}</span>
              {item === last && i === 0 && (
                <span className="text-[10px] text-gray-400 shrink-0">last used</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

interface FormProps {
  fields: CodingField[];
  /** Answers already given (by field name), when editing. */
  initial?: Record<string, string>;
  /** Shown above the fields, e.g. the code being applied. */
  heading?: ReactNode;
  saveLabel: string;
  backLabel: string;
  /** Saves the answers (by field name, unanswered ones left out); a rejection is shown. */
  onSave: (answers: Record<string, string>) => Promise<unknown>;
  onBack: () => void;
  autoFocus?: boolean;
}

/** One input per coding field, with Save and Back. Enter saves, Escape goes back. A refusal
 *  from the server (a duplicate, an answer of the wrong kind) is shown in the form. */
export function CodingFieldsForm({
  fields, initial, heading, saveLabel, backLabel, onSave, onBack, autoFocus,
}: FormProps) {
  const [values, setValues] = useState<Record<string, string>>(
    () => Object.fromEntries(fields.map((f) => [f.name, initial?.[f.name] ?? ''])),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A button offered with the refusal (see ActionError), e.g. "Show all".
  const [errorAction, setErrorAction] = useState<ActionError['action'] | null>(null);

  // Send the given answers; remember text answers for the next passage once saved.
  const save = async () => {
    if (saving) return;
    const answers: Record<string, string> = {};
    for (const field of fields) {
      const value = (values[field.name] ?? '').trim();
      if (value) answers[field.name] = value;
    }
    setSaving(true);
    setError(null);
    setErrorAction(null);
    try {
      await onSave(answers);
    } catch (err) {
      setError(readableError(err));
      setErrorAction(err instanceof ActionError ? err.action : null);
      setSaving(false);
      return;
    }
    for (const field of fields) {
      if (field.kind === 'text' && answers[field.name]) lastTextAnswers.set(field.id, answers[field.name]);
    }
    setSaving(false);
  };

  const setValue = (name: string, value: string) => {
    setValues((current) => ({ ...current, [name]: value }));
    setError(null);
    setErrorAction(null);
  };

  return (
    <div
      onKeyDown={(e) => {
        // The suggestion list handles its own Enter and Escape first.
        if (e.isDefaultPrevented() || e.nativeEvent.isComposing) return;
        if (e.key === 'Enter' && !(e.target as HTMLElement).closest('[data-own-enter]')) {
          e.preventDefault();
          // A held-down Enter must not save a second coding.
          if (!e.repeat) save();
        } else if (e.key === 'Escape') {
          e.preventDefault();
          e.stopPropagation();
          onBack();
        }
      }}
    >
      {heading}
      <div className="space-y-2">
        {fields.map((field, i) => (
          <div key={field.id}>
            <label className="block text-[11px] font-medium text-gray-500 mb-0.5 truncate" title={field.name}>
              {field.name}
            </label>
            {field.kind === 'yes_no' ? (
              <YesNoControl
                name={field.name}
                value={values[field.name] ?? ''}
                onChange={(v) => setValue(field.name, v)}
                autoFocus={autoFocus && i === 0}
              />
            ) : field.kind === 'number' ? (
              <input
                ref={autoFocus && i === 0 ? focusWithoutScroll : undefined}
                inputMode="numeric"
                value={values[field.name] ?? ''}
                onChange={(e) => setValue(field.name, e.target.value)}
                placeholder="Whole number"
                aria-label={field.name}
                className={INPUT_CLASS}
              />
            ) : (
              <TextFieldInput
                field={field}
                value={values[field.name] ?? ''}
                onChange={(v) => setValue(field.name, v)}
                autoFocus={autoFocus && i === 0}
              />
            )}
          </div>
        ))}
      </div>
      {error && (
        <p className="mt-2 text-xs text-red-600" role="alert">
          {error}
          {errorAction && (
            <button
              type="button"
              data-own-enter
              onClick={() => { errorAction.run(); setError(null); setErrorAction(null); }}
              className="ml-1 text-indigo-600 hover:text-indigo-700 font-medium"
            >
              {errorAction.label}
            </button>
          )}
        </p>
      )}
      <div className="flex gap-1 mt-2.5">
        <button
          type="button"
          onClick={save}
          disabled={saving}
          className="flex-1 py-1 text-sm font-medium rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 flex items-center justify-center gap-1"
        >
          {saving && <Loader2 size={12} className="animate-spin" />}
          {saveLabel}
        </button>
        <button
          type="button"
          data-own-enter
          onClick={onBack}
          className="px-2 py-1 text-sm rounded bg-gray-100 text-gray-500 hover:bg-gray-200"
        >
          {backLabel}
        </button>
      </div>
      <p className="mt-1 text-[10px] text-gray-400">Enter saves · Esc goes back</p>
    </div>
  );
}

/** The answers of one coding, compactly ("agreement: yes · person: …"). A hand coding's
 *  answers open an editor on click; a model coding's answers are shown as recorded. */
export function CodingAnswers({ coding, fields, projectId }: {
  coding: Coding; fields: CodingField[]; projectId: number;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const pieces = answerPieces(fields, coding.fields);
  const editable = coding.run_id == null && fields.length > 0;

  if (editing) {
    return (
      <div className="mt-1 mb-1 p-2 rounded border border-gray-200 bg-gray-50">
        <CodingFieldsForm
          fields={fields}
          initial={coding.fields}
          saveLabel="Save answers"
          backLabel="Cancel"
          autoFocus
          onSave={async (answers) => {
            await codingsApi.setFields(coding.id, answers);
            await invalidateAnswers(queryClient, projectId);
            setEditing(false);
          }}
          onBack={() => setEditing(false)}
        />
      </div>
    );
  }
  if (pieces.length === 0 && !editable) return null;
  const text = pieces.map((p) => `${p.name}: ${p.text}`).join(' · ');
  if (!editable) {
    return (
      <p className="text-[11px] text-gray-500 break-words" title="Answers recorded by the model run; they cannot be changed">
        {text}
      </p>
    );
  }
  return (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); setEditing(true); }}
      className={`block text-left text-[11px] break-words hover:text-indigo-600 ${text ? 'text-gray-500' : 'text-gray-400 italic'}`}
      title="Edit answers"
    >
      {text || 'Add answers'}
    </button>
  );
}
