import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Loader2, Table, X } from 'lucide-react';
import { documents, type TableImportResult, type TablePreview } from '../api';

interface Props {
  projectId: number;
  file: File;
  onClose: () => void;
  onImported: (result: TableImportResult) => void;
}

/** Import a spreadsheet: one document per row, with a chosen text column and variables. */
export function TableImportDialog({ projectId, file, onClose, onImported }: Props) {
  const [sheet, setSheet] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const preview = useQuery({
    queryKey: ['table-preview', file.name, file.size, file.lastModified, sheet],
    queryFn: () => documents.previewTable(file, sheet),
    retry: false,
    staleTime: Infinity,
  });

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-gray-950/35 p-4"
      onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}
    >
      <div className="w-full max-w-2xl rounded-xl border border-gray-200 bg-white shadow-2xl">
        <div className="flex items-start gap-3 border-b border-gray-100 px-5 py-4">
          <div className="rounded-lg bg-indigo-50 p-2 text-indigo-700"><Table size={20} /></div>
          <div className="min-w-0 flex-1">
            <h2 className="truncate font-semibold text-gray-900">Import table: {file.name}</h2>
            <p className="mt-1 text-xs text-gray-500">
              Each row becomes a document. Choose the column with the text to code; other columns
              can be kept as document variables.
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 disabled:opacity-40"
            aria-label="Close table import"
          >
            <X size={18} />
          </button>
        </div>

        {preview.isPending && (
          <p className="flex items-center justify-center gap-2 py-10 text-sm text-gray-400">
            <Loader2 size={16} className="animate-spin" /> Reading the table…
          </p>
        )}
        {preview.isError && (
          <div className="p-5">
            <p className="rounded-md bg-red-50 p-3 text-sm text-red-700">{preview.error.message}</p>
            {/* A sheet without usable rows should not trap the researcher on that sheet. */}
            {sheet !== null && (
              <button onClick={() => setSheet(null)} className="mt-3 text-sm text-indigo-600 hover:underline">
                Back to the first sheet
              </button>
            )}
          </div>
        )}
        {preview.data && (
          <TableChoices
            key={preview.data.sheet ?? ''}
            projectId={projectId}
            file={file}
            preview={preview.data}
            onSheetChange={setSheet}
            onBusyChange={setBusy}
            onClose={onClose}
            onImported={onImported}
          />
        )}
      </div>
    </div>
  );
}

/** Column choices for one sheet; remounted (and reset) when the sheet changes. */
function TableChoices({ projectId, file, preview, onSheetChange, onBusyChange, onClose, onImported }: {
  projectId: number;
  file: File;
  preview: TablePreview;
  onSheetChange: (sheet: string) => void;
  onBusyChange: (busy: boolean) => void;
  onClose: () => void;
  onImported: (result: TableImportResult) => void;
}) {
  const [textColumn, setTextColumn] = useState(preview.suggested_text_column);
  const [nameColumn, setNameColumn] = useState('');
  // Columns the researcher unticked; every other column is kept as a variable.
  const [dropped, setDropped] = useState<Set<string>>(new Set());
  const variableOptions = preview.columns.filter((c) => c !== textColumn);
  const variableColumns = variableOptions.filter((c) => !dropped.has(c));

  const importMut = useMutation({
    mutationFn: () => {
      onBusyChange(true);
      return documents.importTable(projectId, file, {
        sheet: preview.sheet, textColumn, nameColumn: nameColumn || null, variableColumns,
      });
    },
    onSuccess: (result) => { onBusyChange(false); onImported(result); },
    onError: () => onBusyChange(false),
  });

  const toggle = (column: string) => setDropped((prev) => {
    const next = new Set(prev);
    if (next.has(column)) next.delete(column); else next.add(column);
    return next;
  });
  const shorten = (cell: string) => (cell.length > 80 ? `${cell.slice(0, 80)}…` : cell);
  const selectClass = 'mt-1 w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500';

  return (
    <>
      <div className="max-h-[60vh] space-y-4 overflow-auto p-5">
        {preview.sheets.length > 1 && (
          <label className="block text-xs font-medium text-gray-600">
            Sheet
            <select value={preview.sheet ?? ''} onChange={(e) => onSheetChange(e.target.value)} className={selectClass}>
              {preview.sheets.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
        )}
        <div className="grid grid-cols-2 gap-3">
          <label className="block text-xs font-medium text-gray-600">
            Text to code
            <select
              value={textColumn}
              onChange={(e) => {
                setTextColumn(e.target.value);
                if (e.target.value === nameColumn) setNameColumn('');
              }}
              className={selectClass}
            >
              {preview.columns.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
          <label className="block text-xs font-medium text-gray-600">
            Document names
            <select value={nameColumn} onChange={(e) => setNameColumn(e.target.value)} className={selectClass}>
              <option value="">File name and row number</option>
              {preview.columns.filter((c) => c !== textColumn).map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
        </div>

        {variableOptions.length > 0 && (
          <div>
            <p className="text-xs font-medium text-gray-600">Keep as document variables</p>
            <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
              {variableOptions.map((c) => (
                <label key={c} className="flex items-center gap-1.5 text-sm text-gray-700">
                  <input
                    type="checkbox"
                    checked={!dropped.has(c)}
                    onChange={() => toggle(c)}
                    className="h-3.5 w-3.5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                  />
                  {c}
                </label>
              ))}
            </div>
          </div>
        )}

        <div>
          <p className="text-xs font-medium text-gray-600">
            First rows ({preview.row_count} row{preview.row_count !== 1 ? 's' : ''} in total)
          </p>
          <div className="mt-1.5 overflow-x-auto rounded-md border border-gray-200">
            <table className="min-w-full text-xs">
              <thead className="bg-gray-50 text-left text-gray-600">
                <tr>
                  {preview.columns.map((c) => (
                    <th key={c} className={`whitespace-nowrap px-2 py-1.5 font-medium ${c === textColumn ? 'bg-indigo-50 text-indigo-700' : ''}`}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((row, i) => (
                  <tr key={i} className="border-t border-gray-100 align-top">
                    {row.map((cell, j) => (
                      <td key={j} className={`px-2 py-1.5 text-gray-700 ${preview.columns[j] === textColumn ? 'min-w-[16rem] bg-indigo-50/50' : 'whitespace-nowrap'}`}>
                        {shorten(cell)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        {importMut.isError && (
          <p className="rounded-md bg-red-50 p-3 text-sm text-red-700">{importMut.error.message}</p>
        )}
      </div>

      <div className="flex items-center justify-between gap-3 border-t border-gray-100 px-5 py-3">
        <span className="text-xs text-gray-400">Rows without text are skipped.</span>
        <div className="flex gap-2">
          <button
            onClick={onClose}
            disabled={importMut.isPending}
            className="rounded-md px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={() => importMut.mutate()}
            disabled={importMut.isPending}
            className="flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {importMut.isPending && <Loader2 size={14} className="animate-spin" />}
            Import {preview.row_count} row{preview.row_count !== 1 ? 's' : ''}
          </button>
        </div>
      </div>
    </>
  );
}
