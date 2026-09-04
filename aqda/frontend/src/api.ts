const BASE = '/api';

/** The API's human-readable `detail` when the error body carries one, else the raw body. */
async function errorDetail(res: Response): Promise<string> {
  const text = await res.text();
  try {
    const parsed = JSON.parse(text);
    if (typeof parsed?.detail === 'string') return parsed.detail;
  } catch {
    // not JSON, keep raw text
  }
  return text;
}

async function request<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    throw new Error(`${res.status}: ${await errorDetail(res)}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// --- Types ---
export interface Project {
  id: number;
  name: string;
  description: string;
  created_at: string;
  modified_at: string;
  doc_count?: number;
  code_count?: number;
  lineage_id?: string;
  revision?: number;
  head_snapshot_id?: string | null;
  shared_folder?: string | null;
  shared_last_published_revision?: number | null;
  shared_last_snapshot_id?: string | null;
  shared_last_sync_at?: string | null;
  shared_sync_error?: string | null;
  is_conflict_mirror?: number;
}

export interface SharedDiscoveredProject {
  root: string;
  folder: string;
  name: string;
  lineage_id: string;
  revision: number;
  updated_at: string;
  updated_by: string;
  head_count: number;
  local_project_id?: number | null;
  linked_project_id?: number | null;
}

export interface SharedRoot {
  path: string;
  name: string;
  available: boolean;
  project_count: number;
  standalone_aqda_count: number;
  linked_project_count: number;
}

export interface SharedStatus {
  root: string;
  roots: SharedRoot[];
  linked: Project[];
  discovered: SharedDiscoveredProject[];
  standalone_aqda_count: number;
  backup_folder: string;
  backup_count: number;
  latest_backup?: string | null;
  sync_error?: string | null;
  last_checked_at?: string | null;
}

export interface ConflictResolutionResult {
  project_id: number;
  choice: 'use_reference' | 'keep_current';
  archived_project_id?: number | null;
  archived_project_name?: string | null;
  backup_path?: string | null;
  message: string;
}

export interface OpenSharedProjectResult {
  project_id: number;
  name: string;
  needs_local_newer_choice?: boolean;
  local_relation?: 'newer' | 'divergent';
  shared_name?: string;
  folder?: string;
  backup_path?: string | null;
}

export interface ProjectImportConflict {
  id: number;
  name: string;
  incoming_name: string;
  lineage_id: string;
  local_revision: number;
  incoming_revision: number;
  incoming_coder?: string;
  trashed?: boolean;
}

export interface ProjectImportResult {
  imported: { id: number; name: string; action: 'create' | 'update' | 'copy'; lineage_id: string }[];
  conflicts: ProjectImportConflict[];
  unchanged: { id: number; name: string; lineage_id: string; reason: 'unchanged' | 'local_newer' }[];
  count: number;
  backup_path?: string | null;
}

export interface Document {
  id: number;
  project_id: number;
  name: string;
  content?: string;
  source_type: string;
  transcript?: string | null;
  label?: string;
  exclude_from_ai?: number;
  created_at: string;
  modified_at: string;
  content_length?: number;
  variables?: Record<string, string>;
}

export interface Code {
  id: number;
  project_id: number;
  parent_id: number | null;
  name: string;
  description: string;
  color: string;
  sort_order: number;
  created_at: string;
  coding_count?: number;
  children?: Code[];
}

export interface Coding {
  id: number;
  document_id: number;
  code_id: number;
  start_pos: number;
  end_pos: number;
  selected_text: string;
  coder?: string;
  offset_unit?: 'codepoint' | 'legacy_utf16';
  repair_status?: string | null;
  created_at: string;
  code_name?: string;
  code_color?: string;
  document_name?: string;
}

export interface Memo {
  id: number;
  project_id: number;
  document_id: number | null;
  code_id: number | null;
  coding_id: number | null;
  start_pos?: number | null;
  end_pos?: number | null;
  title: string;
  content: string;
  created_at: string;
  modified_at: string;
  document_name?: string;
  code_name?: string;
}

export interface Settings {
  [key: string]: string;
}

export interface SkippedUpload {
  name: string;
  reason: string;
}

export interface BulkUploadResult {
  documents: Document[];
  skipped: SkippedUpload[];
}

export interface SimilarResult {
  document_id: number;
  document_name: string;
  text: string;
  start_pos: number;
  end_pos: number;
  similarity: number;
}

export interface ConsistencyOutlier {
  coding_id: number;
  document_id: number;
  document_name: string;
  selected_text: string;
  start_pos: number;
  end_pos: number;
  similarity: number;
}

export interface ConsistencyResult {
  code_id: number;
  code_name: string;
  segment_count: number;
  avg_similarity: number;
  /** Adaptive per-code cutoff; null when the code has too few segments to judge. */
  outlier_cutoff: number | null;
  outliers: ConsistencyOutlier[];
}

export interface HierarchyGroup {
  suggested_parent: string;
  description: string;
  children: string[];
}

export interface HierarchySuggestion {
  groups?: HierarchyGroup[];
  standalone?: string[];
  error?: string;
  raw_response?: string;
}

// --- API functions ---

export const projects = {
  list: () => request<Project[]>('/projects'),
  get: (id: number) => request<Project>(`/projects/${id}`),
  create: (data: { name: string; description?: string }) =>
    request<Project>('/projects', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: Partial<Project>) =>
    request<Project>(`/projects/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number) =>
    request<void>(`/projects/${id}`, { method: 'DELETE' }),
  trash: () => request<Project[]>('/projects/trash/list'),
  restore: (id: number) =>
    request<Project>(`/projects/${id}/restore`, { method: 'POST' }),
  deletePermanent: (id: number) =>
    request<void>(`/projects/${id}/permanent`, { method: 'DELETE' }),
  importDb: async ({
    file,
    mode = 'auto',
    targetLineageId,
  }: {
    file: File;
    mode?: 'auto' | 'copy' | 'replace';
    targetLineageId?: string;
  }): Promise<ProjectImportResult> => {
    const form = new FormData();
    form.append('file', file);
    form.append('mode', mode);
    if (targetLineageId) form.append('target_lineage_id', targetLineageId);
    const res = await fetch(`${BASE}/projects/import-db`, { method: 'POST', body: form });
    if (!res.ok) throw new Error(await errorDetail(res));
    return res.json();
  },
};

export const shared = {
  status: () => request<SharedStatus>('/shared'),
  chooseFolder: () => request<{ path: string; discovered: SharedDiscoveredProject[] }>(
    '/shared/folder/pick', { method: 'POST' }
  ),
  setFolder: (path: string) =>
    request<{ path: string; discovered: SharedDiscoveredProject[] }>('/shared/folder', {
      method: 'PUT', body: JSON.stringify({ path }),
    }),
  removeFolder: (path: string) =>
    request<void>('/shared/folder', {
      method: 'DELETE', body: JSON.stringify({ path }),
    }),
  shareProject: (projectId: number, root?: string) =>
    request<{ project_id: number; folder: string; published: boolean }>(
      `/shared/projects/${projectId}/share`, {
        method: 'POST', body: JSON.stringify({ root }),
      }
    ),
  syncProject: (projectId: number) =>
    request(`/shared/projects/${projectId}/sync`, { method: 'POST' }),
  syncAll: () => request('/shared/sync', { method: 'POST' }),
  openProject: (
    folder: string,
    localNewerChoice?: 'use_shared' | 'use_local',
  ) => request<OpenSharedProjectResult>('/shared/open', {
      method: 'POST', body: JSON.stringify({
        folder,
        local_newer_choice: localNewerChoice,
      }),
    }),
  unlinkProject: (projectId: number) =>
    request<void>(`/shared/projects/${projectId}/link`, { method: 'DELETE' }),
  resolveConflict: (
    conflictProjectId: number,
    choice: 'use_reference' | 'keep_current',
  ) => request<ConflictResolutionResult>(`/shared/conflicts/${conflictProjectId}/resolve`, {
    method: 'POST', body: JSON.stringify({ choice }),
  }),
};

export const system = {
  shutdown: () => request<{ closing: boolean; message: string }>('/system/shutdown', {
    method: 'POST',
  }),
};

export const documents = {
  list: (projectId: number) => request<Document[]>(`/documents?project_id=${projectId}`),
  get: (id: number) => request<Document>(`/documents/${id}`),
  upload: async (projectId: number, file: File): Promise<Document> => {
    const form = new FormData();
    form.append('project_id', String(projectId));
    form.append('file', file);
    const res = await fetch(`${BASE}/documents`, { method: 'POST', body: form });
    if (!res.ok) throw new Error(await errorDetail(res));
    return res.json();
  },
  uploadBulk: async (
    projectId: number,
    files: File[],
    onProgress?: (completed: number, total: number) => void,
  ): Promise<BulkUploadResult> => {
    const BATCH_SIZE = 20;
    const documents: Document[] = [];
    const skipped: SkippedUpload[] = [];
    for (let i = 0; i < files.length; i += BATCH_SIZE) {
      const batch = files.slice(i, i + BATCH_SIZE);
      const form = new FormData();
      form.append('project_id', String(projectId));
      for (const f of batch) form.append('files', f);
      const res = await fetch(`${BASE}/documents/bulk`, { method: 'POST', body: form });
      if (!res.ok) throw new Error(await errorDetail(res));
      const data: BulkUploadResult = await res.json();
      documents.push(...data.documents);
      skipped.push(...data.skipped);
      onProgress?.(Math.min(i + BATCH_SIZE, files.length), files.length);
    }
    return { documents, skipped };
  },
  update: (id: number, data: { name?: string; label?: string; exclude_from_ai?: boolean }) =>
    request<Document>(`/documents/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number) =>
    request<void>(`/documents/${id}`, { method: 'DELETE' }),
  deleteBulk: (ids: number[]) =>
    request<void>('/documents/bulk-delete', { method: 'POST', body: JSON.stringify({ ids }) }),
  parseVariables: (projectId: number) =>
    request<{ updated: number; total: number }>(`/documents/parse-variables?project_id=${projectId}`, { method: 'POST' }),
  getVariables: (id: number) =>
    request<Record<string, string>>(`/documents/${id}/variables`),
  setVariables: (id: number, items: { key: string; value: string }[]) =>
    request<Record<string, string>>(`/documents/${id}/variables`, {
      method: 'PUT',
      body: JSON.stringify(items),
    }),
  deleteVariable: (id: number, key: string) =>
    request<Record<string, string>>(`/documents/${id}/variables/${encodeURIComponent(key)}`, {
      method: 'DELETE',
    }),
  transcribe: (id: number) =>
    request<Document>(`/documents/${id}/transcribe`, { method: 'POST' }),
};

export const codes = {
  list: (projectId: number) => request<Code[]>(`/codes?project_id=${projectId}`),
  create: (data: { project_id: number; name: string; parent_id?: number; description?: string; color?: string }) =>
    request<Code>('/codes', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: Partial<Code>) =>
    request<Code>(`/codes/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number) =>
    request<void>(`/codes/${id}`, { method: 'DELETE' }),
  deleteImpact: (id: number) =>
    request<{ name: string; code_count: number; child_count: number; coding_count: number }>(
      `/codes/${id}/delete-impact`
    ),
};

export const codings = {
  list: (params: { document_id?: number; code_id?: number; project_id?: number }) => {
    const qs = new URLSearchParams();
    if (params.document_id) qs.set('document_id', String(params.document_id));
    if (params.code_id) qs.set('code_id', String(params.code_id));
    if (params.project_id) qs.set('project_id', String(params.project_id));
    return request<Coding[]>(`/codings?${qs}`);
  },
  create: (data: { document_id: number; code_id: number; start_pos: number; end_pos: number; selected_text: string }) =>
    request<Coding>('/codings', { method: 'POST', body: JSON.stringify(data) }),
  delete: (id: number) =>
    request<void>(`/codings/${id}`, { method: 'DELETE' }),
};

export const memos = {
  list: (params: { project_id?: number; document_id?: number; code_id?: number }) => {
    const qs = new URLSearchParams();
    if (params.project_id) qs.set('project_id', String(params.project_id));
    if (params.document_id !== undefined) qs.set('document_id', String(params.document_id));
    if (params.code_id !== undefined) qs.set('code_id', String(params.code_id));
    return request<Memo[]>(`/memos?${qs}`);
  },
  create: (data: Partial<Memo> & { project_id: number }) =>
    request<Memo>('/memos', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: { title?: string; content?: string }) =>
    request<Memo>(`/memos/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number) =>
    request<void>(`/memos/${id}`, { method: 'DELETE' }),
};

export const settings = {
  get: () => request<Settings>('/settings'),
  update: (items: { key: string; value: string }[]) =>
    request<Settings>('/settings', { method: 'PUT', body: JSON.stringify(items) }),
  ollamaModels: () => request<{ available: boolean; models: string[] }>('/settings/ollama/models'),
  ollamaStatus: () => request<{ running: boolean }>('/settings/ollama/status'),
  ollamaStart: () => request<{ started: boolean; message: string }>('/settings/ollama/start', { method: 'POST' }),
  dataDir: () => request<{ path: string; db_file: string }>('/settings/data-dir'),
};

export const ai = {
  findSimilar: (data: { project_id: number; query: string; code_id?: number; document_ids?: number[]; top_k?: number; embedding_model?: string }, signal?: AbortSignal) =>
    request<SimilarResult[]>('/ai/similar', { method: 'POST', body: JSON.stringify(data), signal }),
  autoCode: (data: { project_id: number; code_id: number; top_k?: number; embedding_model?: string }, signal?: AbortSignal) =>
    request<SimilarResult[]>('/ai/autocode', { method: 'POST', body: JSON.stringify(data), signal }),
  summarizeCode: (data: { project_id: number; code_id: number; llm_model?: string }, signal?: AbortSignal) =>
    request<{ summary: string; segment_count: number }>('/ai/summarize-code', { method: 'POST', body: JSON.stringify(data), signal }),
  consistencyCheck: (data: { project_id: number; code_id?: number; similarity_threshold?: number; embedding_model?: string }, signal?: AbortSignal) =>
    request<{ results: ConsistencyResult[] }>('/ai/consistency-check', { method: 'POST', body: JSON.stringify(data), signal }),
  suggestHierarchy: (data: { project_id: number; llm_model?: string }, signal?: AbortSignal) =>
    request<HierarchySuggestion>('/ai/suggest-hierarchy', { method: 'POST', body: JSON.stringify(data), signal }),
  generateDefinition: (data: { project_id: number; code_id: number; llm_model?: string }, signal?: AbortSignal) =>
    request<{ definition: string; segment_count: number }>('/ai/generate-definition', { method: 'POST', body: JSON.stringify(data), signal }),
  embeddingProgress: () =>
    request<{ active: boolean; current: number; total: number; doc_name: string }>('/ai/embedding-progress'),
  cancel: () => request<{ cancelled: boolean }>('/ai/cancel', { method: 'POST' }),
};
