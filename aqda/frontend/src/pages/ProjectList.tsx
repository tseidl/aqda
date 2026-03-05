import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate, Link } from 'react-router-dom';
import { Plus, FolderOpen, Trash2, Settings, FileText, Tags, Upload, RotateCcw, ChevronDown } from 'lucide-react';
import { projects, type Project } from '../api';

export function ProjectList() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [showNew, setShowNew] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [showTrash, setShowTrash] = useState(false);

  const { data: projectList = [], isLoading } = useQuery({
    queryKey: ['projects'],
    queryFn: projects.list,
  });

  const { data: trashList = [] } = useQuery({
    queryKey: ['projects-trash'],
    queryFn: projects.trash,
    enabled: showTrash,
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
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      if (result.count === 1) navigate(`/project/${result.imported[0].id}`);
    },
  });

  const handleImportDb = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.db,.sqlite,.sqlite3,.aqda';
    input.onchange = () => {
      if (input.files?.length) importMut.mutate(input.files[0]);
    };
    input.click();
  };

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b border-gray-200">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
          <h1 className="text-xl font-semibold text-gray-900">AQDA</h1>
          <Link
            to="/settings"
            className="p-2 text-gray-500 hover:text-gray-700 rounded-lg hover:bg-gray-100"
          >
            <Settings size={20} />
          </Link>
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
              title="Import projects from another AQDA database file"
            >
              <Upload size={16} /> {importMut.isPending ? 'Importing...' : 'Import DB'}
            </button>
            <button
              onClick={() => setShowNew(true)}
              className="flex items-center gap-2 px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 text-sm font-medium"
            >
              <Plus size={16} /> New Project
            </button>
          </div>
        </div>

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
                  <h3 className="font-medium text-gray-900">{p.name}</h3>
                  {p.description && (
                    <p className="text-sm text-gray-500 mt-0.5">{p.description}</p>
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
