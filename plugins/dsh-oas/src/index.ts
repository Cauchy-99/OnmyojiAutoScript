import { OasClient } from './client.js';
import { EscalationStore } from './escalate.js';
import { startSupervisor } from './supervisor.js';
import type {
  JsonSchema,
  PluginConfig,
  PluginContext,
  ToolDefinition,
} from './types.js';

export const name = 'oas';

const DEFAULT_BASE_URL = 'http://127.0.0.1:22267';
const DEFAULT_INSTANCES = ['oas1'];
const DEFAULT_TICK_SECONDS = 60;
const DEFAULT_ENABLE = true;
const MIN_TICK_SECONDS = 30;

function normaliseConfig(config: Partial<PluginConfig>): PluginConfig {
  const baseUrl = config.baseUrl?.trim() || DEFAULT_BASE_URL;
  const instances = config.instances ?? [...DEFAULT_INSTANCES];
  const tickSeconds = config.tickSeconds ?? DEFAULT_TICK_SECONDS;
  const stateDir =
    typeof config.stateDir === 'string' && config.stateDir.trim() !== ''
      ? config.stateDir.trim()
      : undefined;
  const enable = config.enable ?? DEFAULT_ENABLE;

  if (!Array.isArray(instances) || instances.length === 0) {
    throw new Error('dsh-oas: instances must contain at least one instance name');
  }
  if (!Number.isFinite(tickSeconds) || tickSeconds < MIN_TICK_SECONDS) {
    throw new Error('dsh-oas: tickSeconds must be a number >= 30');
  }
  if (typeof enable !== 'boolean') {
    throw new Error('dsh-oas: enable must be a boolean');
  }

  return { baseUrl, instances: [...instances], tickSeconds, stateDir, enable };
}

const escalationSchema: JsonSchema = {
  type: 'object',
  properties: {
    id: { type: 'string' },
    created_at: { type: 'string' },
    code: {
      type: 'string',
      enum: [
        'instance_warning',
        'retry_exhausted',
        'protected_task',
        'repeated_failures',
        'state_unreadable',
      ],
    },
    instance: { type: 'string' },
    task: { type: 'string' },
    reason: { type: 'string' },
    status: { type: 'string', enum: ['open', 'resolved'] },
    note: { type: 'string' },
    resolved_at: { type: 'string' },
  },
  required: ['id', 'created_at', 'code', 'instance', 'reason', 'status'],
  additionalProperties: false,
};

export function apply(
  ctx: PluginContext,
  config: Partial<PluginConfig> = {},
): void {
  const settings = normaliseConfig(config);

  const client = new OasClient(settings.baseUrl);
  const store = new EscalationStore(settings.stateDir);

  const statusTool: ToolDefinition = {
    name: 'oas_status',
    description:
      "Read the OAS agent briefing: instances, schedule, hold state and today's progress.",
    parameters: {
      type: 'object',
      properties: {
        instance: {
          type: 'string',
          description: 'OAS instance name, e.g. oas1',
        },
      },
      additionalProperties: false,
    },
    output: {
      schema: { type: 'string' },
      render(_args: unknown, value: unknown) {
        return [{ type: 'text', text: String(value) }];
      },
    },
    async execute(args: Record<string, unknown>) {
      const instance =
        typeof args.instance === 'string' && args.instance !== ''
          ? args.instance
          : undefined;
      return JSON.stringify(await client.briefing(instance), null, 2);
    },
  };

  const escalationsTool: ToolDefinition = {
    name: 'oas_escalations',
    description:
      'List escalations raised by the resident OAS supervisor, newest first.',
    parameters: {
      type: 'object',
      properties: {
        status: {
          type: 'string',
          enum: ['open', 'resolved'],
          description: 'Only return escalations in this state',
        },
      },
      additionalProperties: false,
    },
    output: {
      schema: { type: 'array', items: escalationSchema },
      render(_args: unknown, value: unknown) {
        return [{ type: 'text', text: JSON.stringify(value, null, 2) }];
      },
    },
    execute(args: Record<string, unknown>) {
      const status =
        args.status === 'open' || args.status === 'resolved'
          ? args.status
          : undefined;
      return store.list(status);
    },
  };

  const resolveTool: ToolDefinition = {
    name: 'oas_escalation_resolve',
    description:
      'Mark an OAS escalation resolved and optionally record a note.',
    parameters: {
      type: 'object',
      properties: {
        id: {
          type: 'string',
          description: 'The escalation id, e.g. ESC-20261024-100000-instance_warning',
        },
        note: {
          type: 'string',
          description: 'What was done about the escalation',
        },
      },
      required: ['id'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
        },
        required: ['ok'],
        additionalProperties: false,
      },
      render(_args: unknown, value: unknown) {
        return [{ type: 'text', text: JSON.stringify(value, null, 2) }];
      },
    },
    execute(args: Record<string, unknown>) {
      const id = typeof args.id === 'string' ? args.id : '';
      const note = typeof args.note === 'string' ? args.note : undefined;
      return { ok: id !== '' && store.resolve(id, note) };
    },
  };

  ctx.tools.register(statusTool);
  ctx.tools.register(escalationsTool);
  ctx.tools.register(resolveTool);

  if (settings.enable !== false) {
    startSupervisor({
      client,
      store,
      instances: settings.instances,
      tickSeconds: settings.tickSeconds,
      effect: (execute) => ctx.effect(execute),
      log(message) {
        if (ctx.logger !== undefined) ctx.logger.warn(message);
      },
    });
  }
}
