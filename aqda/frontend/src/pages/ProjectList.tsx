import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate, Link } from 'react-router-dom';
import { Plus, FolderOpen, Trash2, Settings, FileText, Tags, Upload, RotateCcw, ChevronDown, X, AlertTriangle, Cloud, RefreshCw } from 'lucide-react';
import { projects, shared, type Project, type ProjectImportConflict } from '../api';
import { CloseAqdaButton } from '../components/CloseAqdaButton';

export function ProjectList() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [showNew, setShowNew] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [showTrash, setShowTrash] = useState(false);
  const [importMsg, setImportMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [pendingConflict, setPendingConflict] = useState<{
    file: File;
    conflicts: ProjectImportConflict[];
  } | null>(null);
  const [pendingLocalConnection, setPendingLocalConnection] = useState<{
    folder: string;
    name: string;
  } | null>(null);

  const { data: projectList = [], isLoading } = useQuery({
    queryKey: ['projects'],
    queryFn: projects.list,
    refetchInterval: 3000,
  });

  const { data: trashList = [] } = useQuery({
    queryKey: ['projects-trash'],
    queryFn: projects.trash,
    enabled: showTrash,
  });

  const { data: sharedStatus } = useQuery({
    queryKey: ['shared-status'],
    queryFn: shared.status,
    refetchInterval: 5000,
  });

  const openSharedMut = useMutation({
    mutationFn: ({
      folder,
      choice,
    }: {
      folder: string;
      choice?: 'use_shared' | 'use_local';
    }) => shared.openProject(folder, choice),
    onSuccess: (result, variables) => {
      if (result.needs_local_newer_choice) {
        setPendingLocalConnection({
          folder: result.folder ?? variables.folder,
          name: result.shared_name ?? result.name,
        });
        return;
      }
      setPendingLocalConnection(null);
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['shared-status'] });
      if (variables.choice === 'use_shared') {
        alert('Connected to the shared version. AQDA created a full safety backup of your previous local data.');
      } else if (variables.choice === 'use_local') {
        alert('Connected and published the changes from this computer.');
      }
      navigate(`/project/${result.project_id}`);
    },
    onError: (err: Error) => setImportMsg({ ok: false, text: `Could not open shared project: ${err.message}` }),
  });

  const syncMut = useMutation({
    mutationFn: shared.syncAll,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['shared-status'] });
    },
  });

  const createMut = useMutation({
    mutationFn: (data: { name: string; description: string }) => projects.create(data),
    onSuccess: (p) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      navigate(`/project/${p.id}`);
    },
  });

  const deleteMut = useMutation({
    mutationFn: projects.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['projects-trash'] });
    },
  });

  const restoreMut = useMutation({
    mutationFn: projects.restore,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['projects-trash'] });
    },
  });

  const deletePermanentMut = useMutation({
    mutationFn: projects.deletePermanent,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects-trash'] }),
  });

  const importMut = useMutation({
    mutationFn: projects.importDb,
    onSuccess: (result, variables) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      if (variables.mode === 'copy') {
        setPendingConflict((current) => {
          if (!current || !variables.targetLineageId) return null;
          const remaining = current.conflicts.filter(
            (item) => item.lineage_id !== variables.targetLineageId
          );
          return remaining.length ? { ...current, conflicts: remaining } : null;
        });
      } else if (result.conflicts.length) {
        setPendingConflict({ file: variables.file, conflicts: result.conflicts });
      } else {
        setPendingConflict(null);
      }

      const updated = result.imported.filter((item) => item.action === 'update');
      const copied = result.imported.filter((item) => item.action === 'copy');
      const created = result.imported.filter((item) => item.action === 'create');
      if (updated.length === 1) {
        setImportMsg({
          ok: true,
          text: `Updated "${updated[0].name}" from the shared snapshot. A safety backup was created.`,
        });
      } else if (copied.length === 1) {
        setImportMsg({ ok: true, text: `Kept both versions. The incoming work is "${copied[0].name}".` });
      } else if (created.length === 1) {
        setImportMsg({ ok: true, text: `Imported "${created[0].name}".` });
      } else if (result.count > 1) {
        setImportMsg({ ok: true, text: `Imported or updated ${result.count} projects.` });
      } else if (result.unchanged.some((item) => item.reason === 'local_newer')) {
        setImportMsg({ ok: true, text: 'Your local project is already newer than this snapshot; nothing was replaced.' });
      } else if (result.unchanged.length) {
        setImportMsg({ ok: true, text: 'This snapshot has already been imported; nothing changed.' });
      } else if (result.conflicts.length) {
        setImportMsg(null);
      } else if (result.count === 0) {
        setImportMsg({ ok: false, text: 'No projects found in that file.' });
      }
    },
    onError: (err: Error) => {
      setImportMsg({ ok: false, text: `Import failed: ${err.message}` });
    },
  });

  const handleImportDb = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.db,.sqlite,.sqlite3,.aqda';
    input.onchange = () => {
      if (input.files?.length) {
        setImportMsg(null);
        setPendingConflict(null);
        importMut.mutate({ file: input.files[0] });
      }
    };
    input.click();
  };

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b border-gray-200">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
          <h1 className="text-xl font-semibold text-gray-900">AQDA</h1>
          <div className="flex items-center gap-1">
            <CloseAqdaButton />
            <Link
              to="/settings"
              className="p-2 text-gray-500 hover:text-gray-700 rounded-lg hover:bg-gray-100"
            >
              <Settings size={20} />
            </Link>
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-8">
        <div className="flex items-center justify-between mb-6">
          <h2 className="text-lg font-medium text-gray-800">Projects</h2>
          <div className="flex items-center gap-2">
            <button
              onClick={handleImportDb}
              disabled={importMut.isPending}
              className="flex items-center gap-2 px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 text-sm font-medium"
              title="Import a project from an .aqda file (Export → Save a copy to send) or another AQDA database"
            >
              <Upload size={16} /> {importMut.isPending ? 'Importing...' : 'Import Project'}
            </button>
            <button
              onClick={() => setShowNew(true)}
              className="flex items-center gap-2 px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 text-sm font-medium"
            >
              <Plus size={16} /> New Project
            </button>
          </div>
        </div>

        {importMsg && (
          <div
            className={`flex items-start justify-between gap-3 rounded-lg border px-4 py-3 mb-4 text-sm ${
              importMsg.ok
                ? 'bg-green-50 border-green-200 text-green-800'
                : 'bg-red-50 border-red-200 text-red-700'
            }`}
          >
            <span>{importMsg.text}</span>
            <button
              onClick={() => setImportMsg(null)}
              className="opacity-60 hover:opacity-100"
              title="Dismiss"
            >
              <X size={14} />
            </button>
          </div>
        )}

        {sharedStatus?.sync_error && (
          <div className="flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 mb-4 text-sm text-red-800">
            <AlertTriangle size={18} className="mt-0.5 shrink-0" />
            <div className="flex-1">
              <p className="font-medium">Collaboration sync is temporarily unavailable</p>
              <p className="text-xs mt-1">
                Your work is still saved on this computer. AQDA is retrying automatically: {sharedStatus.sync_error}
              </p>
            </div>
            <button
              onClick={() => syncMut.mutate()}
              disabled={syncMut.isPending}
              className="text-xs font-medium underline disabled:opacity-50"
            >
              Retry now
            </button>
          </div>
        )}

        {pendingConflict && pendingConflict.conflicts.map((conflict) => (
          <div
            key={conflict.lineage_id}
            className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 mb-4 text-sm text-amber-900"
          >
            <div className="flex items-start gap-3">
              <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-600" />
              <div className="flex-1">
                <p className="font-medium">
                  {conflict.trashed ? `“${conflict.name}” is currently in your trash` : `Both copies of “${conflict.name}” were edited`}
                </p>
                <p className="text-xs text-amber-800 mt-1">
                  AQDA did not overwrite either version. Keep both, or use the incoming version after creating a full safety backup.
                </p>
                <div className="flex gap-2 mt-3">
                  <button
                    onClick={() => importMut.mutate({
                      file: pendingConflict.file,
                      mode: 'copy',
                      targetLineageId: conflict.lineage_id,
                    })}
                    disabled={importMut.isPending}
                    className="px-3 py-1.5 rounded-md bg-amber-600 text-white text-xs font-medium hover:bg-amber-700 disabled:opacity-50"
                  >
                    Keep both versions
                  </button>
                  <button
                    onClick={() => {
                      if (confirm('Replace your local version with the incoming one? AQDA will create a full safety backup first.')) {
                        importMut.mutate({
                          file: pendingConflict.file,
                          mode: 'replace',
                          targetLineageId: conflict.lineage_id,
                        });
                      }
                    }}
                    disabled={importMut.isPending}
                    className="px-3 py-1.5 rounded-md bg-white border border-amber-300 text-amber-800 text-xs font-medium hover:bg-amber-100 disabled:opacity-50"
                  >
                    Use incoming version
                  </button>
                  <button
                    onClick={() => setPendingConflict(null)}
                    className="px-3 py-1.5 rounded-md bg-white border border-amber-300 text-amber-800 text-xs hover:bg-amber-100"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          </div>
        ))}

        {pendingLocalConnection && (
          <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 mb-4 text-sm text-amber-950">
            <div className="flex items-start gap-3">
              <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-700" />
              <div className="flex-1">
                <p className="font-medium">This computer has additional local changes to “{pendingLocalConnection.name}”</p>
                <p className="text-xs text-amber-800 mt-1">
                  Choose which version should become the shared project. AQDA will not combine or discard them silently.
                </p>
                <div className="flex flex-wrap gap-2 mt-3">
                  <button
                    onClick={() => {
                      if (confirm('Replace this computer’s local project with the shared version? AQDA will create a full database backup first.')) {
                        openSharedMut.mutate({
                          folder: pendingLocalConnection.folder,
                          choice: 'use_shared',
                        });
                      }
                    }}
                    disabled={openSharedMut.isPending}
                    className="px-3 py-1.5 rounded-md bg-amber-700 text-white text-xs font-medium hover:bg-amber-800 disabled:opacity-50"
                  >
                    Use shared version
                  </button>
                  <button
                    onClick={() => {
                      if (confirm('Publish the changes currently saved on this computer to the collaboration folder?')) {
                        openSharedMut.mutate({
                          folder: pendingLocalConnection.folder,
                          choice: 'use_local',
                        });
                      }
                    }}
                    disabled={openSharedMut.isPending}
                    className="px-3 py-1.5 rounded-md bg-white border border-amber-300 text-amber-900 text-xs font-medium hover:bg-amber-100 disabled:opacity-50"
                  >
                    Publish my local changes
                  </button>
                  <button
                    onClick={() => setPendingLocalConnection(null)}
                    disabled={openSharedMut.isPending}
                    className="px-3 py-1.5 text-xs text-amber-800 hover:text-amber-950 disabled:opacity-50"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {sharedStatus?.root && sharedStatus.discovered.some((item) => !item.linked_project_id) && (
          <section className="rounded-lg border border-blue-200 bg-blue-50 p-4 mb-4">
            <div className="flex items-center justify-between mb-3">
              <div>
                <h3 className="text-sm font-medium text-blue-900 flex items-center gap-2">
                  <Cloud size={16} /> Shared projects available
                </h3>
                <p className="text-xs text-blue-700 mt-1">
                  Open once; AQDA will then save and sync these projects automatically.
                </p>
              </div>
              <button
                onClick={() => syncMut.mutate()}
                disabled={syncMut.isPending}
                className="p-2 text-blue-600 hover:bg-blue-100 rounded-md"
                title="Check for shared changes now"
              >
                <RefreshCw size={15} className={syncMut.isPending ? 'animate-spin' : ''} />
              </button>
            </div>
            <div className="space-y-2">
              {sharedStatus.discovered.filter((item) => !item.linked_project_id).map((item) => (
                <div key={`${item.folder}-${item.lineage_id}`} className="flex items-center justify-between bg-white rounded-md border border-blue-100 px-3 py-2">
                  <div>
                    <p className="text-sm font-medium text-gray-800">{item.name}</p>
                    <p className="text-xs text-gray-500">
                      {item.local_project_id
                        ? 'Your existing local project is ready to connect'
                        : item.updated_by
                          ? `Last saved by ${item.updated_by}`
                          : 'Shared AQDA project'}
                      {item.root
                        ? ` · ${item.root.split(/[/\\]/).filter(Boolean).pop() ?? item.root}`
                        : ''}
                      {item.head_count > 1 ? ' · concurrent versions detected' : ''}
                    </p>
                  </div>
                  <button
                    onClick={() => openSharedMut.mutate({ folder: item.folder })}
                    disabled={openSharedMut.isPending}
                    className="px-3 py-1.5 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 disabled:opacity-50"
                  >
                    {item.local_project_id ? 'Connect existing project' : 'Open project'}
                  </button>
                </div>
              ))}
            </div>
          </section>
        )}

        {showNew && (
          <div className="bg-white rounded-lg border border-gray-200 p-4 mb-4">
            <input
              autoFocus
              placeholder="Project name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm mb-2 focus:outline-none focus:ring-2 focus:ring-indigo-500"
              onKeyDown={(e) => {
                if (e.key === 'Enter' && newName.trim()) {
                  createMut.mutate({ name: newName.trim(), description: newDesc });
                }
              }}
            />
            <textarea
              placeholder="Description (optional)"
              value={newDesc}
              onChange={(e) => setNewDesc(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm mb-3 focus:outline-none focus:ring-2 focus:ring-indigo-500"
              rows={2}
            />
            <div className="flex gap-2">
              <button
                onClick={() => {
                  if (newName.trim()) createMut.mutate({ name: newName.trim(), description: newDesc });
                }}
                disabled={!newName.trim()}
                className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-700 disabled:opacity-50"
              >
                Create
              </button>
              <button
                onClick={() => { setShowNew(false); setNewName(''); setNewDesc(''); }}
                className="px-4 py-2 text-gray-600 bg-gray-100 rounded-md text-sm hover:bg-gray-200"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {isLoading ? (
          <p className="text-gray-500 text-sm">Loading...</p>
        ) : projectList.length === 0 ? (
          <div className="text-center py-16 text-gray-500">
            <FolderOpen size={48} className="mx-auto mb-4 text-gray-300" />
            <p className="text-lg mb-1">No projects yet</p>
            <p className="text-sm">Create your first project to start coding</p>
          </div>
        ) : (
          <div className="grid gap-3">
            {projectList.map((p: Project) => (
              <div
                key={p.id}
                className="bg-white rounded-lg border border-gray-200 p-4 hover:border-indigo-300 hover:shadow-sm cursor-pointer transition-all flex items-center justify-between"
                onClick={() => navigate(`/project/${p.id}`)}
              >
                <div>
                  <div className="flex items-center gap-2">
                    <h3 className="font-medium text-gray-900">{p.name}</h3>
                    {p.is_conflict_mirror ? (
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-100 text-amber-800">
                        <AlertTriangle size={11} /> Collaborator reference
                      </span>
                    ) : p.shared_folder && (
                      <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium ${
                        p.shared_sync_error
                          ? 'bg-amber-100 text-amber-800'
                          : p.revision === p.shared_last_published_revision
                            ? 'bg-green-100 text-green-700'
                            : 'bg-blue-100 text-blue-700'
                      }`}>
                        <Cloud size={11} />
                        {p.shared_sync_error ? 'Needs attention' : p.revision === p.shared_last_published_revision ? 'Shared · saved' : 'Shared · saving'}
                      </span>
                    )}
                  </div>
                  {p.description && (
                    <p className="text-sm text-gray-500 mt-0.5">{p.description}</p>
                  )}
                  {p.shared_sync_error && (
                    <p className="text-xs text-amber-700 mt-1">{p.shared_sync_error}</p>
                  )}
                  <div className="flex gap-4 mt-2 text-xs text-gray-400">
                    <span className="flex items-center gap-1"><FileText size={12} /> {p.doc_count ?? 0} docs</span>
                    <span className="flex items-center gap-1"><Tags size={12} /> {p.code_count ?? 0} codes</span>
                    <span>Modified {new Date(p.modified_at).toLocaleDateString()}</span>
                  </div>
                </div>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    if (confirm('Move this project to trash?')) {
                      deleteMut.mutate(p.id);
                    }
                  }}
                  className="p-2 text-gray-400 hover:text-red-500 rounded-lg hover:bg-red-50"
                  title="Move to trash"
                >
                  <Trash2 size={16} />
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Trash section */}
        <div className="mt-8">
          <button
            onClick={() => setShowTrash(!showTrash)}
            className="flex items-center gap-2 text-sm text-gray-400 hover:text-gray-600"
          >
            <Trash2 size={14} />
            <span>Trash</span>
            <ChevronDown size={12} className={`transition-transform ${showTrash ? 'rotate-180' : ''}`} />
          </button>
          {showTrash && trashList.length === 0 && (
            <p className="text-xs text-gray-400 mt-2 ml-6">Trash is empty</p>
          )}
          {showTrash && trashList.length > 0 && (
            <div className="grid gap-2 mt-3">
              {trashList.map((p: Project) => (
                <div
                  key={p.id}
                  className="bg-gray-50 rounded-lg border border-gray-200 p-3 flex items-center justify-between opacity-70"
                >
                  <div>
                    <h3 className="font-medium text-gray-600 text-sm">{p.name}</h3>
                    <div className="flex gap-4 mt-1 text-xs text-gray-400">
                      <span>{p.doc_count ?? 0} docs, {p.code_count ?? 0} codes</span>
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => restoreMut.mutate(p.id)}
                      className="p-2 text-gray-400 hover:text-indigo-600 rounded-lg hover:bg-indigo-50"
                      title="Restore project"
                    >
                      <RotateCcw size={14} />
                    </button>
                    <button
                      onClick={() => {
                        if (confirm('Permanently delete this project? This cannot be undone.')) {
                          deletePermanentMut.mutate(p.id);
                        }
                      }}
                      className="p-2 text-gray-400 hover:text-red-500 rounded-lg hover:bg-red-50"
                      title="Delete permanently"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
