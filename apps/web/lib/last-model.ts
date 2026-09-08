const KEY_PREFIX = "recore:last-model:";

// Remembers which model a workspace last chatted with, so starting a new chat defaults to it
// instead of an arbitrary first entry in the enabled-models list. Best-effort: a private window
// or disabled storage just means no memory across sessions, not a broken app.
export function getLastModelId(workspaceId: string): string | null {
  try {
    return localStorage.getItem(`${KEY_PREFIX}${workspaceId}`);
  } catch {
    return null;
  }
}

export function setLastModelId(workspaceId: string, modelId: string): void {
  try {
    localStorage.setItem(`${KEY_PREFIX}${workspaceId}`, modelId);
  } catch {
    // ignore — see getLastModelId
  }
}
