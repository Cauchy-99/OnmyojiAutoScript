import type { OasClient } from './client.js';
import type { EscalationStore } from './escalate.js';
import { runTick } from './tick.js';
import type { PluginContext } from './types.js';

export interface SupervisorDeps {
  client: OasClient;
  store: EscalationStore;
  instances: string[];
  tickSeconds: number;
  effect: PluginContext['effect'];
  log?: (message: string) => void;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Start the resident loop.
 *
 * The effect owns the interval and returns its clear function. Cycles never
 * overlap: an interval firing while the previous cycle is still awaiting OAS
 * returns immediately instead of queueing work.
 */
export function startSupervisor(deps: SupervisorDeps): void {
  const log = deps.log ?? (() => {});
  let inFlight = false;

  const runCycle = async (): Promise<void> => {
    if (inFlight) return;
    inFlight = true;
    try {
      for (const instance of deps.instances) {
        try {
          await runTick({
            client: deps.client,
            store: deps.store,
            instance,
            log: deps.log,
          });
        } catch (error) {
          // One broken instance must not stop the others, nor the interval.
          log(`[dsh-oas] tick for ${instance} failed: ${errorMessage(error)}`);
        }
      }
    } finally {
      inFlight = false;
    }
  };

  deps.effect(() => {
    void runCycle();
    const timer = setInterval(() => {
      void runCycle();
    }, deps.tickSeconds * 1_000);

    return () => clearInterval(timer);
  });
}
