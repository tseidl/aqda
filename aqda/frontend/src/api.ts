const BASE = '/api';

async function request<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
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
}

export interface Document {
  id: number;
  project_id: number;
  name: string;
  content?: string;
  source_type: string;
  transcript?: string | null;
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
  importDb: async (file: File): Promise<{ imported: { id: number; name: string }[]; count: number }> => {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`${BASE}/projects/import-db`, { method: 'POST', body: form });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  },
};

export const documents = {
  list: (projectId: number) => request<Document[]>(`/documents?project_id=${projectId}`),
  get: (id: number) => request<Document>(`/documents/${id}`),
  upload: async (projectId: number, file: File): Promise<Document> => {
    const form = new FormData();
    form.append('project_id', String(projectId));
    form.append('file', file);
    const res = await fetch(`${BASE}/documents`, { method: 'POST', body: form });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  },
  uploadBulk: async (
    projectId: number,
    files: File[],
    onProgress?: (completed: number, total: number) => void,
  ): Promise<Document[]> => {
    const BATCH_SIZE = 20;
    const results: Document[] = [];
    for (let i = 0; i < files.length; i += BATCH_SIZE) {
      const batch = files.slice(i, i + BATCH_SIZE);
      const form = new FormData();
      form.append('project_id', String(projectId));
      for (const f of batch) form.append('files', f);
      const res = await fetch(`${BASE}/documents/bulk`, { method: 'POST', body: form });
      if (!res.ok) throw new Error(await res.text());
      const docs: Document[] = await res.json();
      results.push(...docs);
      onProgress?.(Math.min(i + BATCH_SIZE, files.length), files.length);
    }
    return results;
  },
  update: (id: number, data: { name?: string }) =>
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
  trash: (projectId: number) => request<Code[]>(`/codes/trash?project_id=${projectId}`),
  restore: (id: number) =>
    request<Code>(`/codes/${id}/restore`, { method: 'POST' }),
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
  analyze: (data: { text: string; instruction?: string; llm_model?: string }, signal?: AbortSignal) =>
    request<{ analysis: string }>('/ai/analyze', { method: 'POST', body: JSON.stringify(data), signal }),
  autoCode: (data: { project_id: number; code_id: number; top_k?: number; embedding_model?: string }, signal?: AbortSignal) =>
    request<SimilarResult[]>('/ai/autocode', { method: 'POST', body: JSON.stringify(data), signal }),
  summarizeCode: (data: { project_id: number; code_id: number; llm_model?: string }, signal?: AbortSignal) =>
    request<{ summary: string; segment_count: number }>('/ai/summarize-code', { method: 'POST', body: JSON.stringify(data), signal }),
  consistencyCheck: (data: { project_id: number; code_id?: number; similarity_threshold?: number; embedding_model?: string }, signal?: AbortSignal) =>
    request<{ results: ConsistencyResult[] }>('/ai/consistency-check', { method: 'POST', body: JSON.stringify(data), signal }),
  negativeCases: (data: { project_id: number; code_id: number; top_k?: number; embedding_model?: string }, signal?: AbortSignal) =>
    request<SimilarResult[]>('/ai/negative-cases', { method: 'POST', body: JSON.stringify(data), signal }),
  suggestHierarchy: (data: { project_id: number; llm_model?: string }, signal?: AbortSignal) =>
    request<HierarchySuggestion>('/ai/suggest-hierarchy', { method: 'POST', body: JSON.stringify(data), signal }),
  generateDefinition: (data: { project_id: number; code_id: number; llm_model?: string }, signal?: AbortSignal) =>
    request<{ definition: string; segment_count: number }>('/ai/generate-definition', { method: 'POST', body: JSON.stringify(data), signal }),
  embeddingProgress: () =>
    request<{ active: boolean; current: number; total: number; doc_name: string }>('/ai/embedding-progress'),
  embeddingStatus: (projectId: number) =>
    request<{ documents: { id: number; name: string; embedded: boolean }[]; embedded_count: number; total_count: number }>(`/ai/embedding-status?project_id=${projectId}`),
};
