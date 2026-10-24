import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { OasClient } from '../src/client.js';
import type { EscalationStore } from '../src/escalate.js';
import { startSupervisor } from '../src/supervisor.js';
import { runTick } from '../src/tick.js';
import type { TickResult } from '../src/tick.js';
import type { PluginContext } from '../src/types.js';

vi.mock('../src/tick.js', () => ({ runTick: vi.fn() }));

const runTickMock = vi.mocked(runTick);

function tickResult(instance = 'oas1'): TickResult {
  return {
    instance,
    actions: [],
    applied: [],
    errors: [],
    skipped: false,
  };
}

function makeEffect(): {
  effect: PluginContext['effect'];
  dispose(): void;
} {
  let dispose: (() => void) | undefined;
  const effect = vi.fn((execute: () => (() => void) | void) => {
    dispose = execute() ?? undefined;
  });
  return {
    effect: effect as unknown as PluginContext['effect'],
    dispose() {
      dispose?.();
    },
  };
}

function supervisorDeps(
  effect: PluginContext['effect'],
  instances: string[] = ['oas1'],
): Parameters<typeof startSupervisor>[0] {
  return {
    client: {} as OasClient,
    store: {} as EscalationStore,
    instances,
    tickSeconds: 5,
    effect,
  };
}

describe('startSupervisor', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    runTickMock.mockReset();
    runTickMock.mockResolvedValue(tickResult());
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('ticks immediately, repeats on the interval and stops on dispose', async () => {
    const { effect, dispose } = makeEffect();

    startSupervisor(supervisorDeps(effect));
    expect(runTickMock).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(5_000);
    expect(runTickMock).toHaveBeenCalledTimes(2);

    dispose();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(runTickMock).toHaveBeenCalledTimes(2);
  });

  it('does not start a second cycle while the first is unresolved', async () => {
    let finish!: (result: TickResult) => void;
    runTickMock.mockImplementationOnce(
      () =>
        new Promise<TickResult>((resolve) => {
          finish = resolve;
        }),
    );
    const { effect, dispose } = makeEffect();

    startSupervisor(supervisorDeps(effect));
    expect(runTickMock).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(5_000);
    expect(runTickMock).toHaveBeenCalledTimes(1);

    finish(tickResult());
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(runTickMock).toHaveBeenCalledTimes(2);

    dispose();
  });

  it('catches a failed tick, logs it and keeps the interval alive', async () => {
    runTickMock
      .mockRejectedValueOnce(new Error('OAS is down'))
      .mockResolvedValue(tickResult());
    const log = vi.fn();
    const { effect, dispose } = makeEffect();
    const deps = supervisorDeps(effect);
    deps.log = log;

    startSupervisor(deps);
    await vi.advanceTimersByTimeAsync(0);

    expect(log).toHaveBeenCalledWith(expect.stringContaining('OAS is down'));

    await vi.advanceTimersByTimeAsync(5_000);
    expect(runTickMock).toHaveBeenCalledTimes(2);

    dispose();
  });

  it('ticks instances sequentially', async () => {
    let finishFirst!: (result: TickResult) => void;
    runTickMock
      .mockImplementationOnce(
        () =>
          new Promise<TickResult>((resolve) => {
            finishFirst = resolve;
          }),
      )
      .mockResolvedValue(tickResult('oas2'));
    const { effect, dispose } = makeEffect();

    startSupervisor(supervisorDeps(effect, ['oas1', 'oas2']));
    expect(runTickMock).toHaveBeenCalledTimes(1);
    expect(runTickMock.mock.calls[0]![0].instance).toBe('oas1');

    await vi.advanceTimersByTimeAsync(5_000);
    expect(runTickMock).toHaveBeenCalledTimes(1);

    finishFirst(tickResult('oas1'));
    await vi.advanceTimersByTimeAsync(0);
    expect(runTickMock).toHaveBeenCalledTimes(2);
    expect(runTickMock.mock.calls[1]![0].instance).toBe('oas2');

    dispose();
  });
});
