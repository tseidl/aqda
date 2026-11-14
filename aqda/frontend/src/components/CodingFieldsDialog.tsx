import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, ArrowUp, ListChecks, Pencil, Plus, Trash2, X } from 'lucide-react';
import { codingFields as codingFieldsApi, type CodingField, type CodingFieldKind } from '../api';
import { FIELD_KIND_LABELS, invalidateAnswers, readableError } from './codingFields';

interface Props {
  projectId: number;
  onClose: () => void;
}

const INPUT_CLASS =
  'px-2 py-1 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500';

/** One field in the list: rename in place, move up or down, delete after a confirmation
 *  that states how many codings lose their answer. */
function FieldRow({ field, isFirst, isLast, busy, onRename, onMove, onDelete }: {
  field: CodingField;
  isFirst: boolean;
  isLast: boolean;
  busy: boolean;
  onRename: (name: string) => Promise<unknown>;
  onMove: (direction: -1 | 1) => void;
  onDelete: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(field.name);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Save a new name; a refused name (already in use) stays in the box with the reason.
  const rename = async () => {
    const trimmed = name.trim();
    if (!trimmed || trimmed === field.name) { setRenaming(false); setName(field.name); return; }
    try {
      await onRename(trimmed);
      setRenaming(false);
      setError(null);
    } catch (err) {
      setError(readableError(err));
    }
  };

  const answered = field.value_count === 1 ? '1 coding has' : `${field.value_count} codings have`;
  return (
    <li className="py-1.5">
      <div className="flex items-center gap-2 group">
        {renaming ? (
          <input
            autoFocus
            value={name}
            onChange={(e) => { setName(e.target.value); setError(null); }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') rename();
              else if (e.key === 'Escape') { e.stopPropagation(); setRenaming(false); setName(field.name); setError(null); }
            }}
            className={`${INPUT_CLASS} flex-1 min-w-0`}
            aria-label="Field name"
          />
        ) : (
          <span className="flex-1 min-w-0 truncate text-sm text-gray-800" title={field.name}>{field.name}</span>
        )}
        <span className="text-xs text-gray-400 shrink-0 w-24">{FIELD_KIND_LABELS[field.kind]}</span>
        <span className="text-xs text-gray-400 shrink-0 w-20 text-right" title="Codings that answer this field">
          {field.value_count ? `${field.value_count} answered` : ''}
        </span>
        <div className="flex items-center gap-0.5 shrink-0">
          {renaming ? (
            <button onClick={rename} className="text-xs px-2 py-0.5 rounded bg-indigo-600 text-white hover:bg-indigo-700">
              Save
            </button>
          ) : (
            <button
              onClick={() => { setRenaming(true); setName(field.name); setConfirming(false); }}
              className="p-1 text-gray-400 hover:text-gray-600 rounded hover:bg-gray-100"
              title="Rename"
            >
              <Pencil size={12} />
            </button>
          )}
          <button
            onClick={() => onMove(-1)}
            disabled={isFirst || busy}
            className="p-1 text-gray-400 hover:text-gray-600 rounded hover:bg-gray-100 disabled:opacity-30"
            title="Move up"
          >
            <ArrowUp size={12} />
          </button>
          <button
            onClick={() => onMove(1)}
            disabled={isLast || busy}
            className="p-1 text-gray-400 hover:text-gray-600 rounded hover:bg-gray-100 disabled:opacity-30"
            title="Move down"
          >
            <ArrowDown size={12} />
          </button>
          <button
            onClick={() => { setConfirming(true); setRenaming(false); }}
            className="p-1 text-gray-400 hover:text-red-500 rounded hover:bg-gray-100"
            title="Delete field"
          >
            <Trash2 size={12} />
          </button>
        </div>
      </div>
      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
      {confirming && (
        <div className="mt-1.5 rounded-md border border-red-100 bg-red-50 p-2.5 text-xs text-red-800">
          <p>
            Delete the field “{field.name}”?{' '}
            {field.value_count
              ? `${answered} an answer to it; those answers are deleted too. This cannot be undone.`
              : 'No coding has an answer to it yet.'}
          </p>
          <div className="mt-2 flex gap-1.5">
            <button
              onClick={() => { setConfirming(false); onDelete(); }}
              className="px-2 py-1 rounded bg-red-600 text-white font-medium hover:bg-red-700"
            >
              Delete field
            </button>
            <button
              onClick={() => setConfirming(false)}
              className="px-2 py-1 rounded bg-white text-gray-600 border border-gray-200 hover:bg-gray-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

/** Define the project's coding fields: questions answered each time a code is applied. */
export function CodingFieldsDialog({ projectId, onClose }: Props) {
  const queryClient = useQueryClient();
  const [newName, setNewName] = useState('');
  const [newKind, setNewKind] = useState<CodingFieldKind>('yes_no');
  const [error, setError] = useState<string | null>(null);
  // Fetched fresh when the dialog opens, so the answer counts are current.
  const { data: fields = [], isPending } = useQuery({
    queryKey: ['coding-fields', projectId],
    queryFn: () => codingFieldsApi.list(projectId),
    refetchOnMount: 'always',
  });

  const refresh = () => invalidateAnswers(queryClient, projectId);

  const createMut = useMutation({
    mutationFn: () => codingFieldsApi.create({ project_id: projectId, name: newName.trim(), kind: newKind }),
    onSuccess: () => { setNewName(''); setError(null); refresh(); },
    onError: (err) => setError(readableError(err)),
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => codingFieldsApi.delete(id),
    onSuccess: () => { setError(null); refresh(); },
    onError: (err) => setError(readableError(err)),
  });

  // Store the new order; only fields whose position changed are sent.
  const moveMut = useMutation({
    mutationFn: async ({ index, direction }: { index: number; direction: -1 | 1 }) => {
      const ordered = [...fields];
      const [moved] = ordered.splice(index, 1);
      ordered.splice(index + direction, 0, moved);
      await Promise.all(
        ordered.map((f, i) => (f.sort_order === i ? null : codingFieldsApi.update(f.id, { sort_order: i }))),
      );
    },
    onSettled: () => refresh(),
    onError: (err) => setError(readableError(err)),
  });

  // Escape closes the dialog wherever the focus is; renaming a field handles its own Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const addField = () => {
    if (newName.trim() && !createMut.isPending) createMut.mutate();
  };

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-gray-950/35 p-4"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="w-full max-w-lg rounded-xl border border-gray-200 bg-white shadow-2xl">
        <div className="flex items-start gap-3 border-b border-gray-100 px-5 py-4">
          <div className="rounded-lg bg-indigo-50 p-2 text-indigo-700"><ListChecks size={20} /></div>
          <div className="min-w-0 flex-1">
            <h2 className="font-semibold text-gray-900">Coding fields</h2>
            <p className="mt-1 text-xs text-gray-500">
              Questions you answer each time you apply a code to a passage, for example whether
              the speaker agrees (yes / no) or which person or organization makes the statement
              (text). Answers are optional.
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
            aria-label="Close coding fields"
          >
            <X size={18} />
          </button>
        </div>

        <div className="px-5 py-3 max-h-[50vh] overflow-auto">
          {isPending ? (
            <p className="py-4 text-center text-sm text-gray-400">Loading…</p>
          ) : fields.length === 0 ? (
            <p className="py-4 text-center text-sm text-gray-400">
              No coding fields yet. Without fields, a code is applied with one click.
            </p>
          ) : (
            <ul className="divide-y divide-gray-100">
              {fields.map((field, index) => (
                <FieldRow
                  key={field.id}
                  field={field}
                  isFirst={index === 0}
                  isLast={index === fields.length - 1}
                  busy={moveMut.isPending}
                  onRename={async (name) => {
                    await codingFieldsApi.update(field.id, { name });
                    await refresh();
                  }}
                  onMove={(direction) => moveMut.mutate({ index, direction })}
                  onDelete={() => deleteMut.mutate(field.id)}
                />
              ))}
            </ul>
          )}
        </div>

        <div className="border-t border-gray-100 px-5 py-3">
          <div className="flex items-center gap-2">
            <input
              value={newName}
              onChange={(e) => { setNewName(e.target.value); setError(null); }}
              onKeyDown={(e) => { if (e.key === 'Enter') addField(); }}
              placeholder="New field, e.g. agreement"
              className={`${INPUT_CLASS} flex-1 min-w-0`}
              aria-label="New field name"
            />
            <select
              value={newKind}
              onChange={(e) => setNewKind(e.target.value as CodingFieldKind)}
              className={INPUT_CLASS}
              aria-label="Kind of answer"
            >
              {(Object.keys(FIELD_KIND_LABELS) as CodingFieldKind[]).map((kind) => (
                <option key={kind} value={kind}>{FIELD_KIND_LABELS[kind]}</option>
              ))}
            </select>
            <button
              onClick={addField}
              disabled={!newName.trim() || createMut.isPending}
              className="flex items-center gap-1 text-sm px-2.5 py-1 rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
            >
              <Plus size={14} /> Add
            </button>
          </div>
          {error && <p className="mt-2 text-xs text-red-600" role="alert">{error}</p>}
        </div>
      </div>
    </div>
  );
}
