import { consumeSSE } from "./chat-client";
import { apiPublicUrl } from "./config";

export type WorkflowTrigger = "manual" | "schedule" | "webhook";
export type WorkflowRunStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "succeeded"
  | "failed"
  | "rejected"
  | "canceled";
export type WorkflowStepRunStatus = "pending" | "running" | "waiting_approval" | "succeeded" | "failed" | "skipped";

export type WorkflowStep = {
  id: string;
  key: string;
  name: string;
  assistant_id: string | null;
  prompt_template: string;
  requires_approval: boolean;
};

export type Workflow = {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  default_model_id: string | null;
  steps: WorkflowStep[];
  created_by: string;
  created_at: string;
};

export type WorkflowStepInput = {
  key: string;
  name: string;
  assistant_id: string;
  prompt_template: string;
  requires_approval?: boolean;
};

export type WorkflowCreateInput = {
  name: string;
  description?: string;
  default_model_id?: string | null;
  steps: WorkflowStepInput[];
};

export type WorkflowUpdateInput = {
  name?: string;
  description?: string;
  enabled?: boolean;
  default_model_id?: string | null;
  steps?: WorkflowStepInput[];
};

export type ToolInvocationRecord = {
  tool_id: string | null;
  name: string;
  arguments: Record<string, unknown>;
  result: string;
  status: "success" | "error";
  error: string | null;
  latency_ms: number;
  children: ToolInvocationRecord[];
};

export type WorkflowSourceRecord = {
  ordinal: number;
  connector_id: string;
  connector_name: string;
  document_id: string;
  label: string;
  url: string | null;
  snippet: string;
  score: number;
};

export type WorkflowStepRun = {
  id: string;
  step_id: string | null;
  position: number;
  key: string;
  name: string;
  assistant_id: string | null;
  status: WorkflowStepRunStatus;
  prompt: string | null;
  output: string | null;
  error: string | null;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  latency_ms: number;
  invocations: ToolInvocationRecord[];
  sources: WorkflowSourceRecord[];
  approved_by: string | null;
  approved_at: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export type WorkflowRun = {
  id: string;
  workflow_id: string;
  trigger: WorkflowTrigger;
  status: WorkflowRunStatus;
  input: string;
  output: string | null;
  error: string | null;
  started_by: string | null;
  current_position: number;
  cost_usd: number;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
};

export type WorkflowRunDetail = WorkflowRun & { steps: WorkflowStepRun[] };

// The events a run's own SSE stream carries — see app/services/generations.py's _EventType.
export type RunEvent =
  | { event: "run_started"; data: Record<string, never> }
  | { event: "step_started"; data: { key: string; name: string } }
  | { event: "step_done"; data: { key: string; output: string } }
  | { event: "step_failed"; data: { key: string; error: string } }
  | { event: "run_waiting"; data: { key: string } }
  | { event: "run_done"; data: { output: string } }
  | { event: "run_failed"; data: { error: string } }
  | { event: "run_canceled"; data: Record<string, never> };

export class WorkflowError extends Error {}

async function api(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`${apiPublicUrl}${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init.headers },
  });
}

async function throwIfNotOk(res: Response): Promise<void> {
  if (res.ok) return;
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  throw new WorkflowError(body?.detail ?? "Something went wrong.");
}

export async function createWorkflow(workspaceId: string, input: WorkflowCreateInput): Promise<Workflow> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/workflows`, {
    method: "POST",
    body: JSON.stringify(input),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Workflow;
}

export async function listWorkflows(workspaceId: string): Promise<Workflow[]> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/workflows`);
  await throwIfNotOk(res);
  return (await res.json()) as Workflow[];
}

export async function getWorkflow(workspaceId: string, workflowId: string): Promise<Workflow> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/workflows/${workflowId}`);
  await throwIfNotOk(res);
  return (await res.json()) as Workflow;
}

export async function updateWorkflow(
  workspaceId: string,
  workflowId: string,
  changes: WorkflowUpdateInput
): Promise<Workflow> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/workflows/${workflowId}`, {
    method: "PUT",
    body: JSON.stringify(changes),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Workflow;
}

export async function deleteWorkflow(workspaceId: string, workflowId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/workflows/${workflowId}`, { method: "DELETE" });
  await throwIfNotOk(res);
}

// Starts a run — queued on the worker immediately; watch it via streamRunEvents.
export async function startRun(workspaceId: string, workflowId: string, input: string): Promise<WorkflowRun> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/workflows/${workflowId}/runs`, {
    method: "POST",
    body: JSON.stringify({ input }),
  });
  await throwIfNotOk(res);
  return (await res.json()) as WorkflowRun;
}

export async function listRuns(
  workspaceId: string,
  workflowId: string,
  options: { limit?: number; before?: string } = {}
): Promise<WorkflowRun[]> {
  const params = new URLSearchParams();
  if (options.limit) params.set("limit", String(options.limit));
  if (options.before) params.set("before", options.before);
  const query = params.toString() ? `?${params.toString()}` : "";
  const res = await api(`/api/v1/workspaces/${workspaceId}/workflows/${workflowId}/runs${query}`);
  await throwIfNotOk(res);
  return (await res.json()) as WorkflowRun[];
}

export async function getRun(workspaceId: string, runId: string): Promise<WorkflowRunDetail> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/runs/${runId}`);
  await throwIfNotOk(res);
  return (await res.json()) as WorkflowRunDetail;
}

export async function cancelRun(workspaceId: string, runId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/runs/${runId}/cancel`, { method: "POST" });
  await throwIfNotOk(res);
}

// Tails a run's event stream from the start — same resumable-replay shape resumeGeneration
// follows for a chat generation, since a run page has no per-tab Last-Event-ID surviving reload.
export async function* streamRunEvents(workspaceId: string, runId: string): AsyncGenerator<RunEvent> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/runs/${runId}/events`);
  await throwIfNotOk(res);
  yield* consumeSSE<RunEvent>(res);
}
