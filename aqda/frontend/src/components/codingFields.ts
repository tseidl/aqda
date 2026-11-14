import { useQuery, type QueryClient } from '@tanstack/react-query';
import { codingFields as codingFieldsApi, type CodingField, type CodingFieldKind } from '../api';

/** How each kind of coding field is named to the researcher. */
export const FIELD_KIND_LABELS: Record<CodingFieldKind, string> = {
  yes_no: 'Yes / no',
  number: 'Whole number',
  text: 'Text',
};

// The project's coding fields in display order (empty while loading or when there are none).
export function useCodingFields(projectId: number): CodingField[] {
  return useCodingFieldsState(projectId).fields;
}

// The project's coding fields with whether the list is known yet: `ready` once it has
// loaded (also when it is empty); `error` when it could not be loaded, with `retry`. The
// list is cached per project, so it is known at once when the project is opened again.
export function useCodingFieldsState(projectId: number) {
  const { data, error, refetch } = useQuery({
    queryKey: ['coding-fields', projectId],
    queryFn: () => codingFieldsApi.list(projectId),
  });
  return {
    fields: data ?? NO_FIELDS,
    ready: data !== undefined,
    error: data === undefined && error ? readableError(error) : null,
    retry: refetch,
  };
}

// One shared empty list, so a project without fields does not get a new array each render.
const NO_FIELDS: CodingField[] = [];

/** A refusal shown with a button that helps, e.g. "Show all" when the coding that blocks
 *  a new one is hidden in the document view. */
export class ActionError extends Error {
  action: { label: string; run: () => void };
  constructor(message: string, action: { label: string; run: () => void }) {
    super(message);
    this.action = action;
  }
}

// Refresh everything that shows field answers or counts after answers or fields change;
// resolves once the visible lists are fetched again.
export function invalidateAnswers(queryClient: QueryClient, projectId: number) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ['codings'] }),
    queryClient.invalidateQueries({ queryKey: ['codes', projectId] }),
    queryClient.invalidateQueries({ queryKey: ['coding-fields', projectId] }),
    queryClient.invalidateQueries({ queryKey: ['coding-field-suggestions'] }),
  ]);
}

/** The answers given to text fields in this browser session, by field ID. They are offered
 *  as the first suggestion when coding the next passage, never filled in by themselves. */
export const lastTextAnswers = new Map<number, string>();

// A server error as a sentence for the researcher, without the HTTP status prefix.
export function readableError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/^\d{3}:\s*/, '');
}

/** One answer shown to the researcher: yes/no stored as "1"/"0" reads as yes/no. */
export function answerText(kind: CodingFieldKind | undefined, value: string): string {
  if (kind === 'yes_no') return value === '1' ? 'yes' : value === '0' ? 'no' : value;
  return value;
}

// The answers of one coding in field order, as "name: answer" pieces.
export function answerPieces(
  fields: CodingField[], answers: Record<string, string> | undefined,
): { name: string; text: string }[] {
  if (!answers) return [];
  const pieces: { name: string; text: string }[] = [];
  const seen = new Set<string>();
  for (const field of fields) {
    const value = answers[field.name];
    if (value === undefined) continue;
    seen.add(field.name);
    pieces.push({ name: field.name, text: answerText(field.kind, value) });
  }
  // Answers to a field this browser does not know yet (renamed elsewhere) still show.
  for (const [name, value] of Object.entries(answers)) {
    if (!seen.has(name)) pieces.push({ name, text: value });
  }
  return pieces;
}
