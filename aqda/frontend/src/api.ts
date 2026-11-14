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
  /** The same backups by purpose; "other" covers code merges and database format updates. */
  backup_counts?: { daily: number; before_collaborator_update: number; other: number };
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

/** A code deletion in the trash: its root code plus what restoring brings back. */
export interface DeletedCode extends Code {
  deleted_at: string;
  parent_name: string | null;
  child_count: number;
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
  /** Answers to the project's coding fields, by field name (yes/no as "1"/"0"); a field
   *  left out is not answered. */
  fields?: Record<string, string>;
  /** The model coding run that made this coding; null for a coding made by hand. */
  run_id?: number | null;
  run_label?: string | null;
  run_model?: string | null;
  /** For a model coding the researcher accepted (a hand coding from then on): the run it
   *  came from. */
  accepted_from_run_id?: number | null;
  accepted_from_run_label?: string | null;
  accepted_from_run_model?: string | null;
}

/** The kinds of answer a coding field takes. */
export type CodingFieldKind = 'yes_no' | 'number' | 'text';

/** A per-project question answered for each coding, e.g. "agreement" (yes/no). */
export interface CodingField {
  id: number;
  project_id: number;
  name: string;
  kind: CodingFieldKind;
  sort_order: number;
  created_at?: string;
  /** How many active codings answer this field. */
  value_count: number;
}

/** The statuses of a model run: still coding, done, or ended early. */
export type ModelRunStatus = 'running' | 'finished' | 'stopped';

/** A model coding run: a program on this computer let a language model code passages. */
export interface ModelRun {
  id: number;
  project_id: number;
  /** Stable across collaborator updates (record IDs are re-created then). */
  uuid: string;
  label: string;
  model: string;
  instructions: string;
  settings: Record<string, unknown>;
  allow_new_codes: boolean;
  status: ModelRunStatus;
  note: string;
  started_at: string;
  last_activity_at: string | null;
  finished_at: string | null;
  deleted_at: string | null;
  removed: boolean;
  documents_read: number;
  /** Active codings of this run that nobody accepted yet. */
  codings: number;
  /** Codings of this run that a researcher accepted; they are hand codings now. */
  codings_accepted: number;
  /** Quotes the run sent that AQDA could not place or accept. */
  refused: number;
  passages_submitted: number;
  /** Codings that restoring a removed run brings back. */
  codings_removed_with_run: number;
}

/** A quote a run sent for a document that AQDA did not accept, with the reason. */
export interface RefusedQuote {
  index: number;
  quote: string | null;
  reason: string;
}

/** One document a run read. */
export interface ModelRunDocument {
  document_id: number;
  document_name: string;
  processed_at: string;
  submitted: number;
  accepted: number;
  refused: RefusedQuote[];
  codings: number;
}

export interface ModelRunDetails extends ModelRun {
  documents: ModelRunDocument[];
}

/** What merging one code into another would do (and, after the merge, did). */
export interface CodeMergeImpact {
  source: { id: number; name: string; description: string };
  target: { id: number; name: string; description: string };
  codings_moved: number;
  trashed_codings_moved: number;
  duplicates_dropped: number;
  memos_moved: number;
  passage_memos_relinked: number;
  children_moved: number;
  trashed_children_moved: number;
  /** What happens to the merged code's definition. */
  description: 'none' | 'carried_over' | 'same' | 'saved_as_memo';
}

export interface CodeMergeResult extends Omit<CodeMergeImpact, 'target'> {
  /** The code merged into, with its new coding count. */
  code: Code;
  description_memo_id: number | null;
  /** File name of the backup taken before the merge. */
  backup: string;
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

/** A spreadsheet's columns and first rows, for choosing what to import. */
export interface TablePreview {
  columns: string[];
  rows: string[][];
  row_count: number;
  sheets: string[];
  sheet: string | null;
  suggested_text_column: string;
  /** Rows below the header left out of the preview and the import. */
  skip_rows: number;
  /** Whether AQDA chose skip_rows itself (a recognized survey export). */
  skip_rows_suggested: boolean;
}

export interface TableImportResult {
  imported: number;
  skipped_empty: number;
  first_document_id: number;
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

/** The running model answer: waiting for the first word, reasoning, or writing. */
export interface GenerationProgress {
  active: boolean;
  phase: '' | 'waiting' | 'thinking' | 'writing';
  words: number;
  seconds: number;
}

/** Where the time of a finished model answer went, as reported by Ollama (seconds). */
export interface GenerationTiming {
  load_seconds?: number;
  prompt_seconds?: number;
  answer_seconds?: number;
  total_seconds?: number;
  /** The model reasoned before answering; that time is part of answer_seconds. */
  reasoned?: boolean;
}

/** A model Ollama has in memory; cpu_share is the part running on the processor (0 to 1). */
export interface LoadedModel {
  name: string;
  cpu_share: number;
  context_length?: number | null;
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
  /** How many codes the model saw; fewer than code_count when the codebook was too long. */
  codes_included?: number;
  code_count?: number;
  /** False when example passages were dropped to fit the codebook into the window. */
  examples_included?: boolean;
  /** True when the model's answer stopped at the length limit. */
  answer_truncated?: boolean;
  timing?: GenerationTiming;
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
  /** `force` also deletes a shared project whose collaboration folder cannot be reached. */
  deletePermanent: (id: number, force = false) =>
    request<void>(`/projects/${id}/permanent${force ? '?force=true' : ''}`, { method: 'DELETE' }),
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
  /** `force` stops sharing also when the collaboration folder cannot be reached. */
  unlinkProject: (projectId: number, force = false) =>
    request<void>(`/shared/projects/${projectId}/link${force ? '?force=true' : ''}`, { method: 'DELETE' }),
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
  previewTable: async (file: File, sheet: string | null, skipRows: number | null): Promise<TablePreview> => {
    const form = new FormData();
    form.append('file', file);
    if (sheet !== null) form.append('sheet', sheet);
    if (skipRows !== null) form.append('skip_rows', String(skipRows));
    const res = await fetch(`${BASE}/documents/table-preview`, { method: 'POST', body: form });
    if (!res.ok) throw new Error(await errorDetail(res));
    return res.json();
  },
  importTable: async (
    projectId: number,
    file: File,
    choice: {
      sheet: string | null; skipRows: number; textColumn: string; nameColumn: string | null; variableColumns: string[];
    },
  ): Promise<TableImportResult> => {
    const form = new FormData();
    form.append('project_id', String(projectId));
    form.append('file', file);
    form.append('text_column', choice.textColumn);
    if (choice.nameColumn) form.append('name_column', choice.nameColumn);
    form.append('variable_columns', JSON.stringify(choice.variableColumns));
    if (choice.sheet !== null) form.append('sheet', choice.sheet);
    form.append('skip_rows', String(choice.skipRows));
    const res = await fetch(`${BASE}/documents/table-import`, { method: 'POST', body: form });
    if (!res.ok) throw new Error(await errorDetail(res));
    return res.json();
  },
  update: (id: number, data: { name?: string; label?: string }) =>
    request<Document>(`/documents/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number) =>
    request<void>(`/documents/${id}`, { method: 'DELETE' }),
  deleteBulk: (ids: number[]) =>
    request<void>('/documents/bulk-delete', { method: 'POST', body: JSON.stringify({ ids }) }),
  deleteImpact: (ids: number[]) =>
    request<{ document_count: number; coding_count: number; memo_count: number }>(
      '/documents/delete-impact', { method: 'POST', body: JSON.stringify({ ids }) }
    ),
  setAiExclusions: (projectId: number, excludedIds: number[], listedIds: number[]) =>
    request<{ excluded_ids: number[] }>('/documents/ai-exclusions', {
      method: 'PUT',
      body: JSON.stringify({
        project_id: projectId, excluded_ids: excludedIds, listed_ids: listedIds,
      }),
    }),
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
  trash: (projectId: number) => request<DeletedCode[]>(`/codes/trash?project_id=${projectId}`),
  restore: (id: number) => request<Code>(`/codes/${id}/restore`, { method: 'POST' }),
  mergeImpact: (id: number, targetId: number) =>
    request<CodeMergeImpact>(`/codes/${id}/merge-impact?target_id=${targetId}`),
  /** Merge code `id` into `targetId`; the server takes a backup first. */
  merge: (id: number, targetId: number) =>
    request<CodeMergeResult>(`/codes/${id}/merge`, {
      method: 'POST', body: JSON.stringify({ target_id: targetId }),
    }),
};

/** Model runs, addressed by uuid; every call names the project, because a copy of a
 *  project in the same database shares its runs' uuids. */
export const runs = {
  list: (projectId: number, includeRemoved = true) =>
    request<ModelRun[]>(`/runs?project_id=${projectId}${includeRemoved ? '&include_removed=true' : ''}`),
  get: (uuid: string, projectId: number) =>
    request<ModelRunDetails>(`/runs/${encodeURIComponent(uuid)}?project_id=${projectId}`),
  /** Mark a running run as finished or stopped. */
  finish: (uuid: string, projectId: number, status: 'finished' | 'stopped') =>
    request<ModelRun>(`/runs/${encodeURIComponent(uuid)}/finish?project_id=${projectId}`, {
      method: 'POST', body: JSON.stringify({ status }),
    }),
  remove: (uuid: string, projectId: number) =>
    request<ModelRun>(`/runs/${encodeURIComponent(uuid)}?project_id=${projectId}`, { method: 'DELETE' }),
  restore: (uuid: string, projectId: number) =>
    request<ModelRun>(`/runs/${encodeURIComponent(uuid)}/restore?project_id=${projectId}`, { method: 'POST' }),
};

export const codings = {
  list: (params: { document_id?: number; code_id?: number; project_id?: number }) => {
    const qs = new URLSearchParams();
    if (params.document_id) qs.set('document_id', String(params.document_id));
    if (params.code_id) qs.set('code_id', String(params.code_id));
    if (params.project_id) qs.set('project_id', String(params.project_id));
    return request<Coding[]>(`/codings?${qs}`);
  },
  /** `fields`: answers by field name, saved together with the coding. */
  create: (data: {
    document_id: number; code_id: number; start_pos: number; end_pos: number; selected_text: string;
    fields?: Record<string, string>;
  }) =>
    request<Coding>('/codings', { method: 'POST', body: JSON.stringify(data) }),
  /** Replace the answers of a hand coding; fields left out become unanswered. */
  setFields: (id: number, fields: Record<string, string>) =>
    request<Coding>(`/codings/${id}/fields`, { method: 'PUT', body: JSON.stringify({ fields }) }),
  /** Accept a model coding: it becomes the researcher's own coding and leaves its run. */
  accept: (id: number) =>
    request<Coding>(`/codings/${id}/accept`, { method: 'POST' }),
  delete: (id: number) =>
    request<void>(`/codings/${id}`, { method: 'DELETE' }),
};

export const codingFields = {
  list: (projectId: number) => request<CodingField[]>(`/coding-fields?project_id=${projectId}`),
  create: (data: { project_id: number; name: string; kind: CodingFieldKind }) =>
    request<CodingField>('/coding-fields', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: { name?: string; kind?: CodingFieldKind; sort_order?: number }) =>
    request<CodingField>(`/coding-fields/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number) =>
    request<void>(`/coding-fields/${id}`, { method: 'DELETE' }),
  /** Earlier answers of a text field, most used first, optionally containing `q`. */
  suggestions: (id: number, q: string) =>
    request<string[]>(`/coding-fields/${id}/suggestions?q=${encodeURIComponent(q)}`),
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
  ollamaLoaded: () => request<{ models: LoadedModel[] }>('/settings/ollama/loaded'),
  ollamaStart: () => request<{ started: boolean; message: string }>('/settings/ollama/start', { method: 'POST' }),
  dataDir: () => request<{ path: string; db_file: string }>('/settings/data-dir'),
};

export const ai = {
  findSimilar: (data: { project_id: number; query: string; code_id?: number; document_ids?: number[]; top_k?: number; embedding_model?: string }, signal?: AbortSignal) =>
    request<SimilarResult[]>('/ai/similar', { method: 'POST', body: JSON.stringify(data), signal }),
  autoCode: (data: { project_id: number; code_id: number; top_k?: number; embedding_model?: string }, signal?: AbortSignal) =>
    request<SimilarResult[]>('/ai/autocode', { method: 'POST', body: JSON.stringify(data), signal }),
  summarizeCode: (data: { project_id: number; code_id: number; llm_model?: string }, signal?: AbortSignal) =>
    request<{ summary: string; segment_count: number; total_segments: number; timing?: GenerationTiming }>('/ai/summarize-code', { method: 'POST', body: JSON.stringify(data), signal }),
  consistencyCheck: (data: { project_id: number; code_id?: number; similarity_threshold?: number; embedding_model?: string }, signal?: AbortSignal) =>
    request<{ results: ConsistencyResult[] }>('/ai/consistency-check', { method: 'POST', body: JSON.stringify(data), signal }),
  suggestHierarchy: (data: { project_id: number; llm_model?: string }, signal?: AbortSignal) =>
    request<HierarchySuggestion>('/ai/suggest-hierarchy', { method: 'POST', body: JSON.stringify(data), signal }),
  generateDefinition: (data: { project_id: number; code_id: number; llm_model?: string }, signal?: AbortSignal) =>
    request<{ definition: string; segment_count: number; total_segments: number; timing?: GenerationTiming }>('/ai/generate-definition', { method: 'POST', body: JSON.stringify(data), signal }),
  embeddingProgress: () =>
    request<{ active: boolean; current: number; total: number; doc_name: string }>('/ai/embedding-progress'),
  generationProgress: () => request<GenerationProgress>('/ai/generation-progress'),
  cancel: () => request<{ cancelled: boolean }>('/ai/cancel', { method: 'POST' }),
};
