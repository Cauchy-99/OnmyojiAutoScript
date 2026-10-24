import type { OasClient } from './client.js';
import type { EscalationStore } from './escalate.js';
import { decide, type Action } from './plan.js';
import {
  ApiError,
  type Briefing,
  type TaskView,
} from './types.js';

export interface TickDeps {
  client: OasClient;
  store: EscalationStore;
  instance: string;
  now?: Date;
  log?: (message: string) => void;
}

export interface TickResult {
  instance: string;
  actions: Action[];
  applied: string[];
  errors: string[];
  skipped: boolean;
}

function oneLine(message: string): string {
  return message.replace(/\s*\n\s*/g, ' ');
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function actionTask(action: Action): string | undefined {
  switch (action.kind) {
    case 'enable_task':
    case 'run_task':
      return action.task;
    case 'escalate':
      return action.task;
    default:
      return undefined;
  }
}

function detailTask(error: ApiError): string | undefined {
  const detail = error.detail;
  if (
    detail !== null
    && typeof detail === 'object'
    && typeof (detail as { task?: unknown }).task === 'string'
  ) {
    return (detail as { task: string }).task;
  }
  return undefined;
}

function recordProtected(
  deps: TickDeps,
  error: ApiError,
  action?: Action,
): void {
  const task = (action === undefined ? undefined : actionTask(action))
    ?? detailTask(error);
  const reason = action === undefined
    ? oneLine(error.message)
    : `${action.reason}; ${oneLine(error.message)}`;

  const input: {
    code: 'protected_task';
    instance: string;
    task?: string;
    reason: string;
  } = {
    code: 'protected_task',
    instance: deps.instance,
    reason,
  };
  if (task !== undefined) input.task = task;

  deps.store.write(input);
}

async function applyAction(deps: TickDeps, action: Action): Promise<string> {
  switch (action.kind) {
    case 'start_instance':
      await deps.client.setRunning(deps.instance, true);
      return `started ${deps.instance}: ${action.reason}`;

    case 'enable_task':
      await deps.client.setTaskEnabled(deps.instance, action.task, true);
      return `enabled ${action.task} on ${deps.instance}: ${action.reason}`;

    case 'run_task':
      await deps.client.runTaskNow(
        deps.instance,
        action.task,
        action.position,
      );
      return `ran ${action.task} on ${deps.instance} (${action.position}): ${action.reason}`;

    case 'emergency_stop':
      await deps.client.emergencyStop();
      return `emergency stop requested: ${action.reason}`;

    case 'escalate': {
      const escalation = deps.store.write({
        code: action.code,
        instance: deps.instance,
        task: action.task,
        reason: action.reason,
      });

      const label = `${action.code}${action.task ? ` (${action.task})` : ''}`;
      try {
        await deps.client.journal(
          `[dsh-oas] ${deps.instance}: ${label}: ${action.reason}`,
        );
      } catch (error) {
        // The escalation file is the durable channel; journaling is a mirror.
        deps.log?.(`journal write failed for ${label}: ${errorMessage(error)}`);
      }

      return escalation === null
        ? `escalation ${label} was already open`
        : `escalated ${label} as ${escalation.id}`;
    }
  }
}

function failureLine(action: Action, error: ApiError): string {
  const task = actionTask(action);
  const subject = task === undefined ? action.kind : `${action.kind} ${task}`;
  return oneLine(`${subject} failed: ${error.message}`);
}

/**
 * Run one decision/execution pass for one instance.
 *
 * Only an unexpected (non-API) failure escapes: the supervisor treats it as a
 * broken tick, while every expected OAS rejection is reported in the result.
 */
export async function runTick(deps: TickDeps): Promise<TickResult> {
  const result: TickResult = {
    instance: deps.instance,
    actions: [],
    applied: [],
    errors: [],
    skipped: false,
  };

  let briefing: Briefing;
  let tasks: TaskView[];
  try {
    briefing = await deps.client.briefing(deps.instance);
    tasks = await deps.client.listTasks(deps.instance);
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    result.errors.push(oneLine(`briefing failed: ${error.message}`));
    if (error.status === 423) {
      result.skipped = true;
    } else if (error.status === 403) {
      recordProtected(deps, error);
    }
    return result;
  }

  const actions = decide({
    instance: deps.instance,
    briefing,
    tasks,
    now: deps.now ?? new Date(),
  });
  result.actions = actions;

  for (const action of actions) {
    try {
      result.applied.push(await applyAction(deps, action));
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;

      result.errors.push(failureLine(action, error));

      if (error.status === 423) {
        result.skipped = true;
        break;
      }
      if (error.status === 403) {
        recordProtected(deps, error, action);
        break;
      }
    }
  }

  return result;
}
