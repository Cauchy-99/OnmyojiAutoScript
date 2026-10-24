import {
  ApiError,
  type Briefing,
  type HoldState,
  type InstanceView,
  type Snapshot,
  type TaskView,
  type WriteResult,
} from './types.js';

type HttpMethod = 'GET' | 'POST' | 'DELETE';
type QueryValue = string | undefined;

const API_PREFIX = '/api/agent';

function withQuery(path: string, query: Record<string, QueryValue>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, value);
  }
  const serialised = params.toString();
  return serialised ? `${path}?${serialised}` : path;
}

function instancePath(instance: string): string {
  return `${API_PREFIX}/${encodeURIComponent(instance)}`;
}

function taskPath(instance: string, task: string): string {
  return `${instancePath(instance)}/tasks/${encodeURIComponent(task)}`;
}

function extractDetail(payload: unknown): unknown {
  if (
    payload !== null
    && typeof payload === 'object'
    && Object.prototype.hasOwnProperty.call(payload, 'detail')
  ) {
    return (payload as { detail?: unknown }).detail;
  }
  return payload;
}

/**
 * A typed, dependency-free client for the OAS `/api/agent/*` REST surface.
 *
 * Every write goes through the same hold/approval guards as the MCP tools;
 * this client has no side channel into the OAS configuration files.
 */
export class OasClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;

  constructor(baseUrl: string, fetchImpl: typeof fetch = fetch) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.fetchImpl = fetchImpl;
  }

  private async request<T>(
    method: HttpMethod,
    path: string,
    body?: unknown,
  ): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' };
    const init: RequestInit = { method, headers };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    const response = await this.fetchImpl(`${this.baseUrl}${path}`, init);
    const text = await response.text();

    if (!response.ok) {
      let payload: unknown;
      if (text !== '') {
        try {
          payload = JSON.parse(text);
        } catch {
          payload = text;
        }
      }
      throw new ApiError(response.status, extractDetail(payload));
    }

    if (text === '') return undefined as T;
    try {
      return JSON.parse(text) as T;
    } catch (cause) {
      const reason = cause instanceof Error ? cause.message : String(cause);
      throw new SyntaxError(
        `OAS API returned invalid JSON for ${method} ${path}: ${reason}`,
      );
    }
  }

  briefing(instance?: string): Promise<Briefing> {
    return this.request<Briefing>(
      'GET',
      withQuery(`${API_PREFIX}/briefing`, { instance }),
    );
  }

  listInstances(): Promise<InstanceView[]> {
    return this.request<InstanceView[]>('GET', `${API_PREFIX}/instances`);
  }

  snapshot(instance: string): Promise<Snapshot> {
    return this.request<Snapshot>('GET', `${instancePath(instance)}/snapshot`);
  }

  listTasks(instance: string): Promise<TaskView[]> {
    return this.request<TaskView[]>('GET', `${instancePath(instance)}/tasks`);
  }

  setRunning(instance: string, running: boolean): Promise<WriteResult> {
    return this.request<WriteResult>(
      'POST',
      `${instancePath(instance)}/running`,
      { running },
    );
  }

  runTaskNow(
    instance: string,
    task: string,
    position = 'front',
  ): Promise<WriteResult> {
    return this.request<WriteResult>('POST', `${taskPath(instance, task)}/run_now`, {
      position,
    });
  }

  setNextRun(instance: string, task: string, at: string): Promise<WriteResult> {
    return this.request<WriteResult>('POST', `${taskPath(instance, task)}/next_run`, {
      at,
    });
  }

  setTaskEnabled(
    instance: string,
    task: string,
    enabled: boolean,
    approvalToken?: string,
  ): Promise<WriteResult> {
    const body: { enabled: boolean; approval_token?: string } = { enabled };
    if (approvalToken !== undefined) body.approval_token = approvalToken;
    return this.request<WriteResult>('POST', `${taskPath(instance, task)}/enable`, body);
  }

  setTaskArgs(
    instance: string,
    task: string,
    group: string,
    argument: string,
    value: unknown,
    approvalToken?: string,
  ): Promise<WriteResult> {
    const body: {
      group: string;
      argument: string;
      value: unknown;
      approval_token?: string;
    } = { group, argument, value };
    if (approvalToken !== undefined) body.approval_token = approvalToken;
    return this.request<WriteResult>('POST', `${taskPath(instance, task)}/args`, body);
  }

  emergencyStop(
    minutes = 120,
  ): Promise<{
    ok: boolean;
    stopped: string[];
    failed: string[];
    hold: HoldState;
  }> {
    return this.request('POST', `${API_PREFIX}/emergency_stop`, { minutes });
  }

  hold(
    minutes: number,
    reason = '',
  ): Promise<{ hold: HoldState }> {
    return this.request('POST', `${API_PREFIX}/hold`, { minutes, reason });
  }

  releaseHold(): Promise<{ hold: HoldState }> {
    return this.request('DELETE', `${API_PREFIX}/hold`);
  }

  updateState(
    patch: Record<string, unknown>,
    instance?: string,
  ): Promise<Record<string, unknown>> {
    const body: { patch: Record<string, unknown>; instance?: string } = { patch };
    if (instance !== undefined) body.instance = instance;
    return this.request('POST', `${API_PREFIX}/daily_state`, body);
  }

  journal(text: string): Promise<{ path: string }> {
    return this.request('POST', `${API_PREFIX}/journal`, { text });
  }

  fileIssue(
    task: string,
    symptom: string,
    suggestion = '',
    instance?: string,
  ): Promise<Record<string, unknown>> {
    const body: {
      task: string;
      symptom: string;
      suggestion: string;
      instance?: string;
    } = { task, symptom, suggestion };
    if (instance !== undefined) body.instance = instance;
    return this.request('POST', `${API_PREFIX}/issues`, body);
  }

  listIssues(status?: string): Promise<Record<string, unknown>[]> {
    return this.request<Record<string, unknown>[]>(
      'GET',
      withQuery(`${API_PREFIX}/issues`, { status }),
    );
  }
}
