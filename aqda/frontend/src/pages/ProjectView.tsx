import { useState, useCallback, useRef, useMemo, useEffect } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  ArrowLeft, Upload, FileText, Tags, StickyNote, Search,
  Download, ChevronDown, Sparkles, Plus, Trash2, Settings,
  Filter, LayoutList, CheckSquare, Square, Tag, BookMarked, Cloud, AlertTriangle, Pencil,
  FolderOpen, X,
} from 'lucide-react';
import { projects, documents, codes, codings, memos, shared, type Document as Doc } from '../api';
import { CodeTree } from '../components/CodeTree';
import { DocumentViewer } from '../components/DocumentViewer';
import { MemoPanel } from '../components/MemoPanel';
import { SegmentsBrowser } from '../components/SegmentsBrowser';
import { AiPanel } from '../components/AiPanel';
import type { MentionCandidate } from '../components/mentions';
import { CloseAqdaButton } from '../components/CloseAqdaButton';

type Tab = 'codes' | 'documents' | 'memos' | 'segments' | 'ai';

export function ProjectView() {
  const { projectId: pid } = useParams();
  const projectId = Number(pid);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [activeTab, setActiveTab] = useState<Tab>('documents');
  const [selectedDocId, setSelectedDocId] = useState<number | null>(null);
  const [selectedCodeId, setSelectedCodeId] = useState<number | null>(null);
  const [selectedMemoId, setSelectedMemoId] = useState<number | null>(null);
  const [showExportMenu, setShowExportMenu] = useState(false);
  const [renamingProject, setRenamingProject] = useState(false);
  const [projectNameDraft, setProjectNameDraft] = useState('');
  const [collaborationNotice, setCollaborationNotice] = useState<string | null>(null);
  const [showCollaborationPicker, setShowCollaborationPicker] = useState(false);
  const [collaborationPickerError, setCollaborationPickerError] = useState<string | null>(null);
  const [sidebarWidth, setSidebarWidth] = useState(280);
  const isResizing = useRef(false);
  const lastProjectRevision = useRef<number | undefined>(undefined);

  // Document list controls
  const [docSort, setDocSort] = useState<'name' | 'date' | 'type'>('name');
  const [docFilter, setDocFilter] = useState<'all' | 'text' | 'pdf' | 'image' | 'audio'>('all');
  const [showDocVars, setShowDocVars] = useState(false);
  const [showDocControls, setShowDocControls] = useState(false);
  const [highlightRange, setHighlightRange] = useState<{ start: number; end: number } | null>(null);
  const [uploadProgress, setUploadProgress] = useState<{ completed: number; total: number } | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadNotice, setUploadNotice] = useState<string | null>(null);
  const [selectedDocIds, setSelectedDocIds] = useState<Set<number>>(new Set());
  const [selectionMode, setSelectionMode] = useState(false);
  const [renamingDocId, setRenamingDocId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [taggingDocId, setTaggingDocId] = useState<number | null>(null);
  const [tagValue, setTagValue] = useState('');
  const [docSearch, setDocSearch] = useState('');

  // Drag-to-resize sidebar
  const startResize = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isResizing.current = true;
    const startX = e.clientX;
    const startWidth = sidebarWidth;

    const onMove = (ev: MouseEvent) => {
      if (!isResizing.current) return;
      const newWidth = Math.max(200, Math.min(600, startWidth + ev.clientX - startX));
      setSidebarWidth(newWidth);
    };
    const onUp = () => {
      isResizing.current = false;
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }, [sidebarWidth]);

  // Queries
  const { data: project } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => projects.get(projectId),
    refetchInterval: 3000,
  });

  useEffect(() => {
    if (project?.revision === undefined) return;
    if (
      lastProjectRevision.current !== undefined &&
      lastProjectRevision.current !== project.revision
    ) {
      queryClient.invalidateQueries({ queryKey: ['documents', projectId] });
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
      queryClient.invalidateQueries({ queryKey: ['codings'] });
      queryClient.invalidateQueries({ queryKey: ['memos', projectId] });
    }
    lastProjectRevision.current = project.revision;
  }, [project?.revision, projectId, queryClient]);

  const { data: sharedStatus, isLoading: sharedStatusLoading } = useQuery({
    queryKey: ['shared-status'],
    queryFn: shared.status,
    enabled: showCollaborationPicker,
  });

  const shareMut = useMutation({
    mutationFn: (root: string) => shared.shareProject(projectId, root),
    onSuccess: () => {
      setShowCollaborationPicker(false);
      setCollaborationPickerError(null);
      queryClient.invalidateQueries({ queryKey: ['project', projectId] });
      queryClient.invalidateQueries({ queryKey: ['shared-status'] });
      alert('Collaboration is on. AQDA will save complete snapshots automatically.');
    },
    onError: (error: Error) => setCollaborationPickerError(error.message),
  });

  const chooseCollaborationFolderMut = useMutation({
    mutationFn: shared.chooseFolder,
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['shared-status'] });
      shareMut.mutate(result.path);
    },
    onError: (error: Error) => setCollaborationPickerError(error.message),
  });

  const renameProjectMut = useMutation({
    mutationFn: (name: string) => projects.update(projectId, { name }),
    onSuccess: () => {
      setRenamingProject(false);
      queryClient.invalidateQueries({ queryKey: ['project', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
    onError: (error: Error) => alert(error.message),
  });

  const unlinkMut = useMutation({
    mutationFn: () => shared.unlinkProject(projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['project', projectId] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['shared-status'] });
      alert('Collaboration stopped. The project and all of your work remain safely on this computer.');
    },
    onError: (error: Error) => alert(error.message),
  });

  const resolveConflictMut = useMutation({
    mutationFn: (choice: 'use_reference' | 'keep_current') =>
      shared.resolveConflict(projectId, choice),
    onSuccess: (result) => {
      setSelectedDocId(null);
      setSelectedCodeId(null);
      setSelectedMemoId(null);
      lastProjectRevision.current = undefined;
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['projects-trash'] });
      queryClient.invalidateQueries({ queryKey: ['shared-status'] });
      queryClient.invalidateQueries({ queryKey: ['project', result.project_id] });
      queryClient.invalidateQueries({ queryKey: ['documents', result.project_id] });
      queryClient.invalidateQueries({ queryKey: ['codes', result.project_id] });
      queryClient.invalidateQueries({ queryKey: ['codings'] });
      queryClient.invalidateQueries({ queryKey: ['memos', result.project_id] });
      alert(result.message);
      navigate(`/project/${result.project_id}`);
    },
    onError: (error: Error) => setCollaborationNotice(error.message),
  });

  const { data: docList = [] } = useQuery({
    queryKey: ['documents', projectId],
    queryFn: () => documents.list(projectId),
  });

  // Filtered + sorted documents
  const filteredDocs = useMemo(() => {
    let docs = [...docList];
    if (docSearch.trim()) {
      const q = docSearch.toLowerCase();
      docs = docs.filter((d) => d.name.toLowerCase().includes(q));
    }
    if (docFilter !== 'all') {
      docs = docs.filter((d) => d.source_type === (docFilter === 'text' ? 'text' : docFilter));
    }
    docs.sort((a, b) => {
      if (docSort === 'name') return a.name.localeCompare(b.name);
      if (docSort === 'date') return (b.modified_at ?? '').localeCompare(a.modified_at ?? '');
      if (docSort === 'type') return (a.source_type ?? '').localeCompare(b.source_type ?? '');
      return 0;
    });
    return docs;
  }, [docList, docFilter, docSort, docSearch]);

  const { data: codeList = [] } = useQuery({
    queryKey: ['codes', projectId],
    queryFn: () => codes.list(projectId),
  });

  // Shared with MemoPanel (same query key) — used for @-mention candidates.
  const { data: memoList = [] } = useQuery({
    queryKey: ['memos', projectId],
    queryFn: () => memos.list({ project_id: projectId }),
  });

  const { data: selectedDoc } = useQuery({
    queryKey: ['document', selectedDocId],
    queryFn: () => documents.get(selectedDocId!),
    enabled: !!selectedDocId,
  });

  const { data: docCodings = [] } = useQuery({
    queryKey: ['codings', 'doc', selectedDocId],
    queryFn: () => codings.list({ document_id: selectedDocId! }),
    enabled: !!selectedDocId,
  });

  // Mutations
  const uploadMut = useMutation({
    mutationFn: async (files: File[]) => {
      setUploadError(null);
      setUploadNotice(null);
      if (files.length === 1) {
        const d = await documents.upload(projectId, files[0]);
        return { documents: [d], skipped: [] };
      }
      setUploadProgress({ completed: 0, total: files.length });
      return documents.uploadBulk(projectId, files, (completed, total) => {
        setUploadProgress({ completed, total });
      });
    },
    onSuccess: (res) => {
      setUploadProgress(null);
      queryClient.invalidateQueries({ queryKey: ['documents', projectId] });
      if (res.documents.length === 1 && res.skipped.length === 0) setSelectedDocId(res.documents[0].id);
      if (res.skipped.length > 0) {
        const names = res.skipped.slice(0, 3).map((s) => s.name).join(', ');
        setUploadNotice(
          `Skipped ${res.skipped.length} file${res.skipped.length !== 1 ? 's' : ''}` +
          `${names ? ` (${names}${res.skipped.length > 3 ? ', …' : ''})` : ''} — empty, unreadable, or too large.`
        );
      }
    },
    onError: (err) => {
      setUploadProgress(null);
      setUploadError(err instanceof Error ? err.message : 'Upload failed');
    },
  });

  const deleteDocMut = useMutation({
    mutationFn: documents.delete,
    onSuccess: (_, deletedId) => {
      queryClient.invalidateQueries({ queryKey: ['documents', projectId] });
      if (selectedDocId === deletedId) setSelectedDocId(null);
    },
  });

  const bulkDeleteMut = useMutation({
    mutationFn: (ids: number[]) => documents.deleteBulk(ids),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents', projectId] });
      if (selectedDocId && selectedDocIds.has(selectedDocId)) setSelectedDocId(null);
      setSelectedDocIds(new Set());
      setSelectionMode(false);
    },
  });

  const renameDocMut = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) =>
      documents.update(id, { name }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents', projectId] });
      setRenamingDocId(null);
    },
  });

  const setDocLabelMut = useMutation({
    mutationFn: ({ id, label }: { id: number; label: string }) =>
      documents.update(id, { label }),
    onSuccess: (_, vars) => {
      queryClient.invalidateQueries({ queryKey: ['documents', projectId] });
      queryClient.invalidateQueries({ queryKey: ['document', vars.id] });
      setTaggingDocId(null);
    },
  });

  const parseVarsMut = useMutation({
    mutationFn: () => documents.parseVariables(projectId),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['documents', projectId] });
      alert(`Parsed variables for ${result.updated} of ${result.total} documents.`);
    },
  });

  const showInFlightSaveError = (error: unknown, item: 'coding' | 'memo') => {
    const details = error instanceof Error ? error.message : String(error);
    const likelyCollaboratorRefresh = Boolean(
      project?.shared_folder && /(?:^|\s)(400|404|422):/.test(details)
    );
    if (likelyCollaboratorRefresh) {
      setCollaborationNotice(
        item === 'coding'
          ? 'This project changed while you were selecting text, probably because a collaborator\'s version arrived. Nothing was saved from that selection; please select the passage again.'
          : 'This project changed while you were writing that passage memo, probably because a collaborator\'s version arrived. The memo was not saved; please reselect the passage and try again.'
      );
    } else {
      setCollaborationNotice(`AQDA could not save the ${item}. ${details}`);
    }
    queryClient.invalidateQueries({ queryKey: ['documents', projectId] });
    queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
  };

  const createCodingMut = useMutation({
    mutationFn: codings.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['codings'] });
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
    },
    onError: (error) => showInFlightSaveError(error, 'coding'),
  });

  const deleteCodingMut = useMutation({
    mutationFn: codings.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['codings'] });
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
    },
  });

  const createMemoMut = useMutation({
    mutationFn: memos.create,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['memos', projectId] }),
    onError: (error) => {
      showInFlightSaveError(error, 'memo');
      queryClient.invalidateQueries({ queryKey: ['memos', projectId] });
    },
  });

  // Create a memo anchored to the current document + selected passage.
  const handleAddMemo = useCallback(
    (startPos: number, endPos: number, text: string, title?: string, content?: string) => {
      if (!selectedDocId) return;
      const trimmed = text.trim();
      const snippet = trimmed.length > 50 ? trimmed.slice(0, 50) + '…' : trimmed;
      const note = (content ?? '').trim();
      createMemoMut.mutate({
        project_id: projectId,
        document_id: selectedDocId,
        start_pos: startPos,
        end_pos: endPos,
        title: (title && title.trim()) || snippet || 'Memo',
        content: note || `“${trimmed}”`,
      });
    },
    [selectedDocId, projectId, createMemoMut]
  );

  const handleFileUpload = useCallback(() => {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.accept = '.txt,.pdf,.text,.jpg,.jpeg,.png,.gif,.bmp,.webp,.mp3,.wav,.m4a,.ogg,.flac,.webm,.aac';
    input.onchange = () => {
      if (input.files?.length) {
        uploadMut.mutate(Array.from(input.files));
      }
    };
    input.click();
  }, [uploadMut]);

  const handleApplyCode = useCallback(
    (codeId: number, startPos: number, endPos: number, text: string) => {
      if (!selectedDocId) return;
      createCodingMut.mutate({
        document_id: selectedDocId,
        code_id: codeId,
        start_pos: startPos,
        end_pos: endPos,
        selected_text: text,
      });
    },
    [selectedDocId, createCodingMut]
  );

  // Jump to a code or memo referenced via an @mention
  const handleJumpToMention = useCallback((c: MentionCandidate) => {
    const numId = Number(c.id.slice(1));
    if (Number.isNaN(numId)) return;
    if (c.kind === 'code') {
      setSelectedCodeId(numId);
      setActiveTab('codes');
    } else {
      setSelectedMemoId(numId);
      setActiveTab('memos');
    }
  }, []);

  const tabs: { key: Tab; label: string; icon: React.ReactNode }[] = [
    { key: 'documents', label: 'Docs', icon: <FileText size={20} /> },
    { key: 'codes', label: 'Codes', icon: <Tags size={20} /> },
    { key: 'memos', label: 'Memos', icon: <StickyNote size={20} /> },
    { key: 'segments', label: 'Segments', icon: <Search size={20} /> },
    { key: 'ai', label: 'AI', icon: <Sparkles size={20} /> },
  ];

  return (
    <div className="h-screen flex flex-col bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 px-4 py-2 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3">
          <Link to="/" className="p-1.5 text-gray-500 hover:text-gray-700 rounded hover:bg-gray-100">
            <ArrowLeft size={18} />
          </Link>
          {renamingProject ? (
            <form
              className="flex items-center gap-1.5"
              onSubmit={(event) => {
                event.preventDefault();
                const name = projectNameDraft.trim();
                if (name && name !== project?.name) renameProjectMut.mutate(name);
                else setRenamingProject(false);
              }}
            >
              <input
                autoFocus
                value={projectNameDraft}
                onChange={(event) => setProjectNameDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Escape') setRenamingProject(false);
                }}
                className="w-72 px-2 py-1 text-sm border border-indigo-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
                aria-label="Project name"
              />
              <button
                type="submit"
                disabled={!projectNameDraft.trim() || renameProjectMut.isPending}
                className="px-2 py-1 text-xs font-medium text-indigo-700 hover:bg-indigo-50 rounded disabled:opacity-50"
              >
                Save
              </button>
              <button
                type="button"
                onClick={() => setRenamingProject(false)}
                className="px-2 py-1 text-xs text-gray-500 hover:bg-gray-100 rounded"
              >
                Cancel
              </button>
            </form>
          ) : (
            <div className="flex items-center gap-1.5">
              <h1 className="font-semibold text-gray-900">{project?.name ?? '...'}</h1>
              {project && !project.is_conflict_mirror && (
                <button
                  onClick={() => {
                    setProjectNameDraft(project.name);
                    setRenamingProject(true);
                  }}
                  className="p-1 text-gray-300 hover:text-indigo-600 hover:bg-indigo-50 rounded"
                  title={project.shared_folder ? 'Rename project (the managed collaboration folder stays unchanged)' : 'Rename project'}
                >
                  <Pencil size={13} />
                </button>
              )}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          {project?.is_conflict_mirror ? (
            <span className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-amber-50 text-amber-800 text-xs font-medium" title="This reference follows the collaborator's concurrent branch">
              <AlertTriangle size={14} /> Collaborator reference
            </span>
          ) : project?.shared_folder ? (
            <div className="flex items-center gap-1">
              <span className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium ${
                project.shared_sync_error
                  ? 'bg-amber-50 text-amber-800'
                  : 'bg-green-50 text-green-700'
              }`} title={project.shared_folder}>
                <Cloud size={14} /> {project.shared_sync_error ? 'Shared · attention' : project.revision === project.shared_last_published_revision ? 'Shared · saved' : 'Shared · saving'}
              </span>
              <button
                onClick={() => {
                  if (confirm('Stop collaboration for this project on this computer? Your local project and all its work will remain safe.')) {
                    unlinkMut.mutate();
                  }
                }}
                disabled={unlinkMut.isPending}
                className="px-2 py-1.5 text-xs text-gray-500 hover:text-red-700 hover:bg-red-50 rounded-md disabled:opacity-50"
                title="Stop syncing this project and remove this computer's shared snapshot"
              >
                Stop sharing
              </button>
            </div>
          ) : (
            <button
              onClick={() => {
                setCollaborationPickerError(null);
                setShowCollaborationPicker(true);
              }}
              disabled={shareMut.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-blue-50 hover:bg-blue-100 rounded-md text-blue-700 disabled:opacity-50"
              title="Choose where this project should collaborate"
            >
              <Cloud size={15} /> {shareMut.isPending ? 'Starting…' : 'Collaborate'}
            </button>
          )}
          <Link
            to="/settings"
            className="p-1.5 text-gray-400 hover:text-gray-600 rounded hover:bg-gray-100"
            title="Settings"
          >
            <Settings size={16} />
          </Link>
          <button
            onClick={handleFileUpload}
            disabled={uploadMut.isPending}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-gray-100 hover:bg-gray-200 rounded-md text-gray-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Upload size={16} /> {uploadMut.isPending ? 'Importing...' : 'Import'}
          </button>
          <div className="relative">
            <button
              onClick={() => setShowExportMenu(!showExportMenu)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-gray-100 hover:bg-gray-200 rounded-md text-gray-700"
            >
              <Download size={16} /> Export <ChevronDown size={12} />
            </button>
            {showExportMenu && (
              <div className="absolute right-0 top-full mt-1 bg-white border border-gray-200 rounded-lg shadow-lg py-1 z-50 min-w-[160px]">
                {[
                  { label: 'Save a copy to send (.aqda)', path: 'aqda' },
                  { label: 'REFI-QDA (.qdpx)', path: 'qdpx' },
                  { label: 'Codebook (.qdc)', path: 'qdc' },
                  { label: 'Codings (.csv)', path: 'csv' },
                  { label: 'Analysis Data (.json)', path: 'json' },
                ].map((fmt) => (
                  <a
                    key={fmt.path}
                    href={`/api/export/${projectId}/${fmt.path}`}
                    className="block px-4 py-2 text-sm text-gray-700 hover:bg-gray-50"
                    onClick={() => setShowExportMenu(false)}
                  >
                    {fmt.label}
                  </a>
                ))}
              </div>
            )}
          </div>
          <CloseAqdaButton />
        </div>
      </header>

      {showCollaborationPicker && (
        <div
          className="fixed inset-0 z-[70] flex items-center justify-center bg-gray-950/35 p-4"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !shareMut.isPending) {
              setShowCollaborationPicker(false);
            }
          }}
        >
          <div className="w-full max-w-xl rounded-xl border border-gray-200 bg-white shadow-2xl">
            <div className="flex items-start gap-3 border-b border-gray-100 px-5 py-4">
              <div className="rounded-lg bg-blue-50 p-2 text-blue-700">
                <Cloud size={20} />
              </div>
              <div className="min-w-0 flex-1">
                <h2 className="font-semibold text-gray-900">Where should this project collaborate?</h2>
                <p className="mt-1 text-xs text-gray-500">
                  Choose the shared folder used by this project’s co-authors. AQDA remembers different locations for different teams.
                </p>
              </div>
              <button
                onClick={() => setShowCollaborationPicker(false)}
                disabled={shareMut.isPending || chooseCollaborationFolderMut.isPending}
                className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 disabled:opacity-40"
                aria-label="Close collaboration location chooser"
              >
                <X size={18} />
              </button>
            </div>

            <div className="max-h-[55vh] space-y-3 overflow-auto p-5">
              {sharedStatusLoading ? (
                <p className="py-4 text-center text-sm text-gray-400">Loading saved locations…</p>
              ) : sharedStatus?.roots.length ? (
                <div className="space-y-2">
                  {sharedStatus.roots.map((root) => (
                    <button
                      key={root.path}
                      onClick={() => {
                        setCollaborationPickerError(null);
                        shareMut.mutate(root.path);
                      }}
                      disabled={!root.available || shareMut.isPending || chooseCollaborationFolderMut.isPending}
                      className="flex w-full items-start gap-3 rounded-lg border border-gray-200 p-3 text-left hover:border-blue-300 hover:bg-blue-50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <FolderOpen size={18} className="mt-0.5 shrink-0 text-blue-600" />
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm font-medium text-gray-800">{root.name}</span>
                        <span className="mt-0.5 block break-all text-[11px] text-gray-500">{root.path}</span>
                        <span className="mt-1 block text-xs text-gray-400">
                          {root.available
                            ? `${root.project_count} collaborative project${root.project_count === 1 ? '' : 's'} here`
                            : 'This folder is currently unavailable'}
                        </span>
                      </span>
                      <span className="mt-1 text-xs font-medium text-blue-700">Use this folder</span>
                    </button>
                  ))}
                </div>
              ) : (
                <div className="rounded-lg bg-gray-50 px-4 py-3 text-sm text-gray-600">
                  No collaboration locations are saved yet. Choose this project’s shared folder below.
                </div>
              )}

              {collaborationPickerError && (
                <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-800">
                  <AlertTriangle size={15} className="mt-0.5 shrink-0" />
                  <span>{collaborationPickerError}</span>
                </div>
              )}

              <button
                onClick={() => {
                  setCollaborationPickerError(null);
                  chooseCollaborationFolderMut.mutate();
                }}
                disabled={shareMut.isPending || chooseCollaborationFolderMut.isPending}
                className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-gray-300 px-3 py-3 text-sm font-medium text-gray-700 hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700 disabled:opacity-50"
              >
                <Plus size={16} />
                {chooseCollaborationFolderMut.isPending ? 'Opening folder chooser…' : 'Choose another shared folder…'}
              </button>
              <p className="text-center text-[11px] text-gray-400">
                Selecting a new folder also saves it for future projects. You can manage saved locations in Settings.
              </p>
            </div>
          </div>
        </div>
      )}

      {project?.shared_sync_error && (
        <div className="shrink-0 flex items-start gap-3 border-b border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-900">
          <AlertTriangle size={17} className="mt-0.5 shrink-0" />
          <span>{project.shared_sync_error}</span>
        </div>
      )}

      {Boolean(project?.is_conflict_mirror) && (
        <div className="shrink-0 flex flex-wrap items-center gap-3 border-b border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-950">
          <div className="flex-1 min-w-64">
            <p className="font-medium">Which version should collaboration continue from?</p>
            <p className="text-xs text-blue-800 mt-0.5">
              Agree with your collaborator first. AQDA preserves the version you do not choose.
            </p>
          </div>
          <button
            onClick={() => {
              if (confirm('Use this collaborator version as the shared project on this computer? AQDA will create a full backup and keep your current version as a clearly named local archive.')) {
                resolveConflictMut.mutate('use_reference');
              }
            }}
            disabled={resolveConflictMut.isPending}
            className="px-3 py-1.5 rounded-md bg-blue-700 text-white text-xs font-medium hover:bg-blue-800 disabled:opacity-50"
          >
            Use this version for collaboration
          </button>
          <button
            onClick={() => {
              if (confirm('Keep your current shared version and move this collaborator reference to Trash? If the other branch keeps changing, AQDA will show it again.')) {
                resolveConflictMut.mutate('keep_current');
              }
            }}
            disabled={resolveConflictMut.isPending}
            className="px-3 py-1.5 rounded-md bg-white border border-blue-300 text-blue-800 text-xs font-medium hover:bg-blue-100 disabled:opacity-50"
          >
            Keep my current shared version
          </button>
        </div>
      )}

      {collaborationNotice && (
        <div className="shrink-0 flex items-start gap-3 border-b border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-900">
          <AlertTriangle size={17} className="mt-0.5 shrink-0" />
          <span className="flex-1">{collaborationNotice}</span>
          <button
            onClick={() => setCollaborationNotice(null)}
            className="text-xs font-medium text-amber-800 hover:text-amber-950 underline"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Main workspace */}
      <div className="flex-1 flex overflow-hidden">
        {/* Icon tab strip */}
        <div className="w-16 border-r border-gray-200 bg-gray-50 flex flex-col items-center py-2 gap-0.5 shrink-0">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={`w-14 py-1.5 flex flex-col items-center gap-0.5 rounded-lg transition-colors ${
                activeTab === tab.key
                  ? 'bg-indigo-100 text-indigo-700'
                  : 'text-gray-400 hover:text-gray-600 hover:bg-gray-200'
              }`}
            >
              {tab.icon}
              <span className="text-[10px] leading-tight font-medium">{tab.label}</span>
            </button>
          ))}
        </div>

        {/* Sidebar panel content */}
        <div
          className="border-r border-gray-200 bg-white flex flex-col shrink-0 relative"
          style={{ width: sidebarWidth }}
        >
          {/* Panel header */}
          <div className="px-3 py-2 border-b border-gray-100 flex items-center justify-between shrink-0">
            <span className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
              {tabs.find((t) => t.key === activeTab)?.label}
            </span>
            {activeTab === 'documents' && (
              <div className="flex items-center gap-1">
                {docList.length > 0 && !selectionMode && (
                  <button
                    onClick={() => setSelectionMode(true)}
                    className="p-1 text-gray-400 hover:text-indigo-600 rounded hover:bg-gray-100"
                    title="Select documents"
                  >
                    <CheckSquare size={16} />
                  </button>
                )}
                <button
                  onClick={handleFileUpload}
                  className="p-1 text-gray-400 hover:text-indigo-600 rounded hover:bg-gray-100"
                  title="Add documents"
                >
                  <Plus size={16} />
                </button>
              </div>
            )}
          </div>

          {/* Upload progress */}
          {uploadProgress && (
            <div className="px-3 py-2 border-b border-gray-100 bg-indigo-50 shrink-0">
              <div className="flex items-center justify-between text-xs text-indigo-700 mb-1">
                <span>Importing documents...</span>
                <span>{uploadProgress.completed} / {uploadProgress.total}</span>
              </div>
              <div className="w-full bg-indigo-100 rounded-full h-1.5">
                <div
                  className="bg-indigo-500 h-1.5 rounded-full transition-all"
                  style={{ width: `${(uploadProgress.completed / uploadProgress.total) * 100}%` }}
                />
              </div>
            </div>
          )}
          {uploadError && (
            <div className="px-3 py-2 border-b border-gray-100 bg-red-50 text-xs text-red-700 flex items-center justify-between shrink-0">
              <span>Import failed: {uploadError}</span>
              <button onClick={() => setUploadError(null)} className="text-red-400 hover:text-red-600 ml-2">dismiss</button>
            </div>
          )}
          {uploadNotice && (
            <div className="px-3 py-2 border-b border-gray-100 bg-amber-50 text-xs text-amber-700 flex items-center justify-between shrink-0">
              <span>{uploadNotice}</span>
              <button onClick={() => setUploadNotice(null)} className="text-amber-400 hover:text-amber-600 ml-2">dismiss</button>
            </div>
          )}

          {/* Sidebar content */}
          <div className="flex-1 overflow-auto">
            {activeTab === 'codes' && (
              <CodeTree
                projectId={projectId}
                codes={codeList}
                memos={memoList}
                selectedCodeId={selectedCodeId}
                onSelectCode={setSelectedCodeId}
                onJumpToMention={handleJumpToMention}
              />
            )}
            {activeTab === 'documents' && (
              <div className="p-2">
                {docList.length === 0 ? (
                  <div className="text-center py-8">
                    <p className="text-sm text-gray-400 mb-3">No documents yet</p>
                    <button
                      onClick={handleFileUpload}
                      className="text-sm text-indigo-600 hover:text-indigo-700 flex items-center gap-1.5 mx-auto"
                    >
                      <Upload size={16} /> Import files
                    </button>
                  </div>
                ) : (<>
                  {/* Search */}
                  <div className="relative mb-2">
                    <Search size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400" />
                    <input
                      type="text"
                      placeholder="Search documents..."
                      value={docSearch}
                      onChange={(e) => setDocSearch(e.target.value)}
                      className="w-full text-xs pl-7 pr-2 py-1.5 rounded-md border border-gray-200 bg-gray-50 focus:bg-white focus:border-indigo-300 focus:outline-none"
                    />
                  </div>

                  {/* Filter/Sort/Display controls */}
                  <div className="flex items-center gap-1 px-1 mb-2">
                    <button
                      onClick={() => setShowDocControls(!showDocControls)}
                      className={`p-1 rounded text-gray-400 hover:text-gray-600 hover:bg-gray-100 ${showDocControls ? 'bg-gray-100 text-gray-600' : ''}`}
                      title="Filter & sort"
                    >
                      <Filter size={13} />
                    </button>
                    <button
                      onClick={() => setShowDocVars(!showDocVars)}
                      className={`p-1 rounded text-gray-400 hover:text-gray-600 hover:bg-gray-100 ${showDocVars ? 'bg-indigo-100 text-indigo-600' : ''}`}
                      title={showDocVars ? 'Hide variables' : 'Show variables'}
                    >
                      <LayoutList size={13} />
                    </button>
                    <span className="text-[10px] text-gray-400 ml-auto">{filteredDocs.length}/{docList.length}</span>
                  </div>

                  {selectionMode && (
                    <div className="flex items-center gap-2 px-2 pb-2 mb-1 border-b border-gray-100">
                      <button
                        onClick={() => {
                          if (selectedDocIds.size === filteredDocs.length) {
                            setSelectedDocIds(new Set());
                          } else {
                            setSelectedDocIds(new Set(filteredDocs.map((d: Doc) => d.id)));
                          }
                        }}
                        className="text-[11px] text-indigo-600 hover:text-indigo-800"
                      >
                        {selectedDocIds.size === filteredDocs.length ? 'Deselect all' : 'Select all'}
                      </button>
                      {selectedDocIds.size > 0 && (
                        <button
                          onClick={() => {
                            if (confirm(`Delete ${selectedDocIds.size} documents?`)) {
                              bulkDeleteMut.mutate(Array.from(selectedDocIds));
                            }
                          }}
                          disabled={bulkDeleteMut.isPending}
                          className="text-[11px] text-red-600 hover:text-red-800 flex items-center gap-1"
                        >
                          <Trash2 size={11} /> Delete {selectedDocIds.size}
                        </button>
                      )}
                      <button
                        onClick={() => { setSelectionMode(false); setSelectedDocIds(new Set()); }}
                        className="text-[11px] text-gray-500 hover:text-gray-700 ml-auto"
                      >
                        Cancel
                      </button>
                    </div>
                  )}

                  {showDocControls && (
                    <div className="px-1 pb-2 mb-2 border-b border-gray-100 space-y-1.5">
                      <div className="flex items-center gap-2">
                        <span className="text-[10px] text-gray-400 uppercase w-10 shrink-0">Sort</span>
                        <div className="flex gap-1">
                          {(['name', 'date', 'type'] as const).map((s) => (
                            <button
                              key={s}
                              onClick={() => setDocSort(s)}
                              className={`text-[10px] px-2 py-0.5 rounded ${docSort === s ? 'bg-indigo-100 text-indigo-700' : 'bg-gray-50 text-gray-500 hover:bg-gray-100'}`}
                            >
                              {s}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-[10px] text-gray-400 uppercase w-10 shrink-0">Type</span>
                        <div className="flex gap-1">
                          {(['all', 'text', 'pdf', 'image', 'audio'] as const).map((f) => (
                            <button
                              key={f}
                              onClick={() => setDocFilter(f)}
                              className={`text-[10px] px-2 py-0.5 rounded ${docFilter === f ? 'bg-indigo-100 text-indigo-700' : 'bg-gray-50 text-gray-500 hover:bg-gray-100'}`}
                            >
                              {f}
                            </button>
                          ))}
                        </div>
                      </div>
                      <button
                        onClick={() => parseVarsMut.mutate()}
                        disabled={parseVarsMut.isPending}
                        className="text-[10px] px-2 py-1 rounded bg-gray-50 text-gray-600 hover:bg-indigo-50 hover:text-indigo-700 w-full text-left"
                      >
                        {parseVarsMut.isPending ? 'Parsing...' : 'Parse variables from filenames'}
                      </button>
                    </div>
                  )}

                  <div className="space-y-0.5">
                    {filteredDocs.map((doc: Doc) => (
                      <div
                        key={doc.id}
                        className={`rounded-md text-sm cursor-pointer group ${
                          selectedDocId === doc.id
                            ? 'bg-indigo-50 text-indigo-700'
                            : 'text-gray-700 hover:bg-gray-50'
                        }`}
                        onClick={() => {
                          if (selectionMode) {
                            setSelectedDocIds((prev) => {
                              const next = new Set(prev);
                              if (next.has(doc.id)) next.delete(doc.id);
                              else next.add(doc.id);
                              return next;
                            });
                          } else {
                            setSelectedDocId(doc.id);
                          }
                        }}
                      >
                        <div className="flex items-center gap-2 px-3 py-2">
                          {selectionMode ? (
                            selectedDocIds.has(doc.id)
                              ? <CheckSquare size={16} className="shrink-0 text-indigo-600" />
                              : <Square size={16} className="shrink-0 text-gray-400" />
                          ) : (
                            <FileText size={16} className="shrink-0" />
                          )}
                          {renamingDocId === doc.id ? (
                            <input
                              autoFocus
                              className="flex-1 text-sm bg-white border border-indigo-300 rounded px-1 py-0 outline-none min-w-0"
                              value={renameValue}
                              onChange={(e) => setRenameValue(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter' && renameValue.trim()) {
                                  renameDocMut.mutate({ id: doc.id, name: renameValue.trim() });
                                } else if (e.key === 'Escape') {
                                  setRenamingDocId(null);
                                }
                              }}
                              onBlur={() => {
                                if (renameValue.trim() && renameValue.trim() !== doc.name) {
                                  renameDocMut.mutate({ id: doc.id, name: renameValue.trim() });
                                } else {
                                  setRenamingDocId(null);
                                }
                              }}
                              onClick={(e) => e.stopPropagation()}
                            />
                          ) : (
                            <span
                              className="truncate flex-1"
                              onDoubleClick={(e) => {
                                e.stopPropagation();
                                setRenamingDocId(doc.id);
                                setRenameValue(doc.name);
                              }}
                            >
                              {doc.name}
                            </span>
                          )}
                          {/* Reference (excluded from AI) indicator */}
                          {doc.exclude_from_ai ? (
                            <span title="Reference — excluded from AI suggestions" className="shrink-0 text-amber-500">
                              <BookMarked size={12} />
                            </span>
                          ) : null}
                          {/* User-set short tag */}
                          {taggingDocId === doc.id ? (
                            <input
                              autoFocus
                              value={tagValue}
                              maxLength={6}
                              placeholder="TAG"
                              onClick={(e) => e.stopPropagation()}
                              onChange={(e) => setTagValue(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') setDocLabelMut.mutate({ id: doc.id, label: tagValue.trim() });
                                else if (e.key === 'Escape') setTaggingDocId(null);
                              }}
                              onBlur={() => setDocLabelMut.mutate({ id: doc.id, label: tagValue.trim() })}
                              className="w-12 text-[10px] uppercase px-1 py-0.5 border border-indigo-300 rounded bg-white outline-none shrink-0"
                            />
                          ) : doc.label ? (
                            <span
                              onClick={(e) => { e.stopPropagation(); setTaggingDocId(doc.id); setTagValue(doc.label ?? ''); }}
                              className="text-[10px] px-1.5 py-0.5 rounded font-semibold shrink-0 bg-indigo-100 text-indigo-700 uppercase cursor-pointer hover:bg-indigo-200"
                              title="Edit tag"
                            >
                              {doc.label}
                            </span>
                          ) : (
                            <button
                              onClick={(e) => { e.stopPropagation(); setTaggingDocId(doc.id); setTagValue(''); }}
                              className="hidden group-hover:flex items-center text-gray-300 hover:text-indigo-600 shrink-0"
                              title="Add tag"
                            >
                              <Tag size={12} />
                            </button>
                          )}
                          <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium shrink-0 ${
                            doc.source_type === 'pdf'
                              ? 'bg-red-100 text-red-600'
                              : doc.source_type === 'image'
                              ? 'bg-green-100 text-green-600'
                              : doc.source_type === 'audio'
                              ? 'bg-amber-100 text-amber-600'
                              : 'bg-blue-100 text-blue-600'
                          }`}>
                            {doc.source_type === 'pdf' ? 'PDF' : doc.source_type === 'image' ? 'IMG' : doc.source_type === 'audio' ? 'AUD' : 'TXT'}
                          </span>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              if (confirm(`Delete "${doc.name}"?`)) deleteDocMut.mutate(doc.id);
                            }}
                            className="hidden group-hover:block p-0.5 text-gray-400 hover:text-red-500"
                          >
                            <Trash2 size={14} />
                          </button>
                        </div>
                        {/* Inline variables display */}
                        {showDocVars && doc.variables && Object.keys(doc.variables).length > 0 && (
                          <div className="px-3 pb-1.5 flex flex-wrap gap-1">
                            {Object.entries(doc.variables).map(([k, v]) => (
                              <span key={k} className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500">
                                <span className="font-medium">{k}:</span> {v}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </>)}
              </div>
            )}
            {activeTab === 'memos' && (
              <MemoPanel
                projectId={projectId}
                codes={codeList}
                selectedMemoId={selectedMemoId}
                onSelectMemo={setSelectedMemoId}
                onJumpToMention={handleJumpToMention}
                selectedDocId={selectedDocId}
                selectedDocName={selectedDoc?.name}
                onNavigate={(docId, startPos, endPos) => {
                  setSelectedDocId(docId);
                  if (startPos !== undefined && endPos !== undefined) {
                    setHighlightRange({ start: startPos, end: endPos });
                  }
                }}
              />
            )}
            {activeTab === 'segments' && (
              <SegmentsBrowser
                projectId={projectId}
                codes={codeList}
                onNavigate={(docId, startPos, endPos) => {
                  setSelectedDocId(docId);
                  if (startPos !== undefined && endPos !== undefined) {
                    setHighlightRange({ start: startPos, end: endPos });
                  }
                }}
              />
            )}
            {activeTab === 'ai' && (
              <AiPanel
                projectId={projectId}
                codes={codeList}
                onNavigate={(docId, startPos, endPos) => {
                  setSelectedDocId(docId);
                  if (startPos !== undefined && endPos !== undefined) {
                    setHighlightRange({ start: startPos, end: endPos });
                  }
                }}
              />
            )}
          </div>

          {/* Resize handle — positioned to straddle the border, not overlap scrollbar */}
          <div
            className="absolute top-0 -right-1 w-2 h-full cursor-col-resize hover:bg-indigo-200 active:bg-indigo-300 z-10"
            onMouseDown={startResize}
          />
        </div>

        {/* Main document area */}
        <div className="flex-1 overflow-hidden">
          {selectedDoc ? (
            <DocumentViewer
              document={selectedDoc}
              codings={docCodings}
              codes={codeList}
              memos={memoList}
              selectedCodeId={selectedCodeId}
              onApplyCode={handleApplyCode}
              onDeleteCoding={(id) => deleteCodingMut.mutate(id)}
              onSelectCode={(id) => { setSelectedCodeId(id); setActiveTab('codes'); }}
              onAddMemo={handleAddMemo}
              highlightRange={highlightRange}
              onHighlightClear={() => setHighlightRange(null)}
            />
          ) : (
            <div className="h-full flex items-center justify-center text-gray-400">
              <div className="text-center">
                <FileText size={48} className="mx-auto mb-3 text-gray-300" />
                <p>Select a document to start coding</p>
                {docList.length === 0 && (
                  <button
                    onClick={handleFileUpload}
                    className="mt-3 text-sm text-indigo-600 hover:text-indigo-700"
                  >
                    Or import documents first
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
