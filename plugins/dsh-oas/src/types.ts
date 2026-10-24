/**
 * Local, dependency-free copies of the harness contracts this plugin uses.
 *
 * The plugin deliberately imports no `@deepseek-ai/*` package: these shapes
 * follow the raw `ToolDefinition` accepted by `ctx.tools.register()`.
 */

export interface PluginConfig {
  baseUrl: string;
  instances: string[];
  tickSeconds: number;
}

export type JsonSchema = Record<string, unknown>;

export interface ToolContentBlock {
  type: 'text';
  text: string;
}

export interface ToolDefinition {
  name: string;
  description: string;
  parameters: JsonSchema;
  output: {
    schema: JsonSchema;
    render(args: unknown, value: unknown): ToolContentBlock[];
    presentationMeta?: unknown;
  };
  execute(
    args: Record<string, unknown>,
    execution?: unknown,
  ): unknown | Promise<unknown>;
  timeoutMs?: number;
}

export interface PluginContext {
  tools: { register(tool: ToolDefinition): void };
  effect(execute: () => (() => void) | void): void;
  logger?: { info(message: string): void; warn(message: string): void };
}

/** The OAS hold gate shared by every write endpoint. */
export interface HoldState {
  active: boolean;
  until: string | null;
  reason: string;
}

/** One OAS instance as reported by `GET /api/agent/instances`. */
export interface InstanceView {
  name: string;
  state: string;
  pid_alive: boolean;
  running_task: string;
  frame_age_s: number | null;
  hold_active: boolean;
}

/** One schedulable task as reported by `GET /api/agent/{instance}/tasks`. */
export interface TaskView {
  task: string;
  enable: boolean;
  next_run: string | null;
  priority: string | number | null;
  success_interval: string | null;
  failure_interval: string | null;
}

/** A scheduled task entry inside a snapshot. */
export interface ScheduledTask {
  name: string;
  next_run: string | null;
}

/** The scheduler portion of an instance snapshot. */
export interface SnapshotSchedule {
  running?: Partial<ScheduledTask>;
  pending?: ScheduledTask[];
  waiting?: ScheduledTask[];
  error?: string;
  [key: string]: unknown;
}

/** One instance snapshot from `GET /api/agent/{instance}/snapshot`. */
export interface Snapshot {
  name: string;
  state: string;
  running_task: string;
  schedule: SnapshotSchedule;
  hold: HoldState;
}

/** The full agent briefing from `GET /api/agent/briefing`. */
export interface Briefing {
  now: string;
  instances: InstanceView[];
  plan: Record<string, unknown>;
  state: Record<string, unknown>;
  hold: HoldState;
  playbook: string;
  [key: string]: unknown;
}

/** The common payload for a successful OAS write. */
export interface WriteResult {
  ok: boolean;
  applied_at: string;
  effective: string;
  detail: Record<string, unknown>;
}

function describe(detail: unknown): string {
  if (typeof detail === 'string') return detail;
  if (detail === undefined) return '';
  try {
    return JSON.stringify(detail);
  } catch {
    return String(detail);
  }
}

/** An HTTP failure from the OAS agent API, preserving FastAPI's `detail`. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    const description = describe(detail);
    super(
      description
        ? `OAS API error ${status}: ${description}`
        : `OAS API error ${status}`,
    );
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}
