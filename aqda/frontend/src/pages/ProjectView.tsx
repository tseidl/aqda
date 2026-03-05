import { useState, useCallback, useRef, useMemo } from 'react';
import { useParams, Link } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  ArrowLeft, Upload, FileText, Tags, StickyNote, Search,
  Download, ChevronDown, Sparkles, Plus, Trash2, Settings,
  Filter, LayoutList,
} from 'lucide-react';
import { projects, documents, codes, codings, type Document as Doc } from '../api';
import { CodeTree } from '../components/CodeTree';
import { DocumentViewer } from '../components/DocumentViewer';
import { MemoPanel } from '../components/MemoPanel';
import { SegmentsBrowser } from '../components/SegmentsBrowser';
import { AiPanel } from '../components/AiPanel';

type Tab = 'codes' | 'documents' | 'memos' | 'segments' | 'ai';

export function ProjectView() {
  const { projectId: pid } = useParams();
  const projectId = Number(pid);
  const queryClient = useQueryClient();

  const [activeTab, setActiveTab] = useState<Tab>('documents');
  const [selectedDocId, setSelectedDocId] = useState<number | null>(null);
  const [selectedCodeId, setSelectedCodeId] = useState<number | null>(null);
  const [showExportMenu, setShowExportMenu] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(280);
  const isResizing = useRef(false);

  // Document list controls
  const [docSort, setDocSort] = useState<'name' | 'date' | 'type'>('name');
  const [docFilter, setDocFilter] = useState<'all' | 'text' | 'pdf' | 'image' | 'audio'>('all');
  const [showDocVars, setShowDocVars] = useState(false);
  const [showDocControls, setShowDocControls] = useState(false);
  const [highlightRange, setHighlightRange] = useState<{ start: number; end: number } | null>(null);

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
  });

  const { data: docList = [] } = useQuery({
    queryKey: ['documents', projectId],
    queryFn: () => documents.list(projectId),
  });

  // Filtered + sorted documents
  const filteredDocs = useMemo(() => {
    let docs = [...docList];
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
  }, [docList, docFilter, docSort]);

  const { data: codeList = [] } = useQuery({
    queryKey: ['codes', projectId],
    queryFn: () => codes.list(projectId),
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
    mutationFn: (files: File[]) =>
      files.length === 1
        ? documents.upload(projectId, files[0]).then((d) => [d])
        : documents.uploadBulk(projectId, files),
    onSuccess: (docs) => {
      queryClient.invalidateQueries({ queryKey: ['documents', projectId] });
      if (docs.length === 1) setSelectedDocId(docs[0].id);
    },
  });

  const deleteDocMut = useMutation({
    mutationFn: documents.delete,
    onSuccess: (_, deletedId) => {
      queryClient.invalidateQueries({ queryKey: ['documents', projectId] });
      if (selectedDocId === deletedId) setSelectedDocId(null);
    },
  });

  const createCodingMut = useMutation({
    mutationFn: codings.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['codings'] });
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
    },
  });

  const deleteCodingMut = useMutation({
    mutationFn: codings.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['codings'] });
      queryClient.invalidateQueries({ queryKey: ['codes', projectId] });
    },
  });

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
          <h1 className="font-semibold text-gray-900">{project?.name ?? '...'}</h1>
        </div>
        <div className="flex items-center gap-2">
          <Link
            to="/settings"
            className="p-1.5 text-gray-400 hover:text-gray-600 rounded hover:bg-gray-100"
            title="Settings"
          >
            <Settings size={16} />
          </Link>
          <button
            onClick={handleFileUpload}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-gray-100 hover:bg-gray-200 rounded-md text-gray-700"
          >
            <Upload size={16} /> Import
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
                  { label: 'Share Project (.aqda)', path: 'aqda' },
                  { label: 'REFI-QDA (.qdpx)', path: 'qdpx' },
                  { label: 'Codebook (.qdc)', path: 'qdc' },
                  { label: 'Codings (.csv)', path: 'csv' },
                  { label: 'Full Project (.json)', path: 'json' },
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
        </div>
      </header>

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
              <button
                onClick={handleFileUpload}
                className="p-1 text-gray-400 hover:text-indigo-600 rounded hover:bg-gray-100"
                title="Add documents"
              >
                <Plus size={16} />
              </button>
            )}
          </div>

          {/* Sidebar content */}
          <div className="flex-1 overflow-auto">
            {activeTab === 'codes' && (
              <CodeTree
                projectId={projectId}
                codes={codeList}
                selectedCodeId={selectedCodeId}
                onSelectCode={setSelectedCodeId}
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
                        onClick={() => setSelectedDocId(doc.id)}
                      >
                        <div className="flex items-center gap-2 px-3 py-2">
                          <FileText size={16} className="shrink-0" />
                          <span className="truncate flex-1">{doc.name}</span>
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
              <MemoPanel projectId={projectId} />
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

          {/* Resize handle */}
          <div
            className="absolute top-0 right-0 w-1.5 h-full cursor-col-resize hover:bg-indigo-200 active:bg-indigo-300 z-10"
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
              selectedCodeId={selectedCodeId}
              onApplyCode={handleApplyCode}
              onDeleteCoding={(id) => deleteCodingMut.mutate(id)}
              onSelectCode={(id) => { setSelectedCodeId(id); setActiveTab('codes'); }}
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
