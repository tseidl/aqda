import type { Code, Memo } from '../api';

export interface MentionCandidate {
  id: string;
  label: string;
  kind: 'code' | 'memo';
  color?: string;
}

/** Build @-mention candidates from the project's codes and memos. */
export function buildMentionCandidates(
  codes: Code[],
  memos: Memo[],
  excludeMemoId?: number,
): MentionCandidate[] {
  return [
    ...codes.map((c) => ({ id: `c${c.id}`, label: c.name, kind: 'code' as const, color: c.color })),
    ...memos
      .filter((m) => m.id !== excludeMemoId && (m.title ?? '').trim())
      .map((m) => ({ id: `m${m.id}`, label: m.title, kind: 'memo' as const })),
  ].filter((x) => x.label.trim());
}
