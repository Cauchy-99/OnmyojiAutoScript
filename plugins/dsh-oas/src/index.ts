import { OasClient } from './client.js';
import type { PluginConfig, PluginContext, ToolDefinition } from './types.js';

export const name = 'oas';

const DEFAULT_BASE_URL = 'http://127.0.0.1:22267';
const DEFAULT_INSTANCES = ['oas1'];
const DEFAULT_TICK_SECONDS = 60;
const MIN_TICK_SECONDS = 30;

function normaliseConfig(config: Partial<PluginConfig>): PluginConfig {
  const baseUrl = config.baseUrl?.trim() || DEFAULT_BASE_URL;
  const instances = config.instances ?? [...DEFAULT_INSTANCES];
  const tickSeconds = config.tickSeconds ?? DEFAULT_TICK_SECONDS;

  if (!Array.isArray(instances) || instances.length === 0) {
    throw new Error('dsh-oas: instances must contain at least one instance name');
  }
  if (!Number.isFinite(tickSeconds) || tickSeconds < MIN_TICK_SECONDS) {
    throw new Error('dsh-oas: tickSeconds must be a number >= 30');
  }

  return { baseUrl, instances: [...instances], tickSeconds };
}

export function apply(
  ctx: PluginContext,
  config: Partial<PluginConfig> = {},
): void {
  const settings = normaliseConfig(config);

  // Built before registration so the supervisor added in a later task can
  // share the same client without moving any existing code.
  const client = new OasClient(settings.baseUrl);

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

  ctx.tools.register(statusTool);

  // The supervisor (a later task) will own a tick timer through ctx.effect:
  // ctx.effect(() => { const timer = setInterval(...); return () => clearInterval(timer); });
}
