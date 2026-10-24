import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { OasClient } from '../src/client.js';
import { EscalationStore } from '../src/escalate.js';
import { runTick } from '../src/tick.js';
import { ApiError } from '../src/types.js';
import type {
  Briefing,
  InstanceView,
  TaskView,
  WriteResult,
} from '../src/types.js';

const INSTANCE = 'oas1';
const NOW = new Date('2026-10-24T10:00:00+08:00');

function instanceView(overrides: Partial<InstanceView> = {}): InstanceView {
  return {
    name: INSTANCE,
    state: 'ACTIVE',
    pid_alive: true,
    running_task: '',
    frame_age_s: null,
    hold_active: false,
    ...overrides,
  };
}

function taskView(task: string, enable = true): TaskView {
  return {
    task,
    enable,
    next_run: null,
    priority: null,
    success_interval: null,
    failure_interval: null,
  };
}

function briefing(
  options: {
    state?: string;
    plan?: Record<string, unknown>;
    attempts?: Record<string, number>;
  } = {},
): Briefing {
  return {
    now: '2026-10-24T10:00:00',
    instances: [instanceView({ state: options.state ?? 'ACTIVE' })],
    plan: options.plan ?? {},
    state: {
      date: '2026-10-24',
      instances: {
        [INSTANCE]: {
          done: [],
          skipped: [],
          attempts: options.attempts ?? {},
        },
      },
      weekly: {},
    },
    hold: { active: false, until: null, reason: '' },
    playbook: '',
  };
}

function writeResult(): WriteResult {
  return {
    ok: true,
    applied_at: NOW.toISOString(),
    effective: 'immediate',
    detail: {},
  };
}

interface FakeClient {
  briefing: ReturnType<typeof vi.fn>;
  listTasks: ReturnType<typeof vi.fn>;
  setRunning: ReturnType<typeof vi.fn>;
  runTaskNow: ReturnType<typeof vi.fn>;
  setTaskEnabled: ReturnType<typeof vi.fn>;
  emergencyStop: ReturnType<typeof vi.fn>;
  journal: ReturnType<typeof vi.fn>;
}

function makeClient(): FakeClient {
  return {
    briefing: vi.fn(),
    listTasks: vi.fn(),
    setRunning: vi.fn(),
    runTaskNow: vi.fn(),
    setTaskEnabled: vi.fn(),
    emergencyStop: vi.fn(),
    journal: vi.fn(),
  };
}

function asClient(client: FakeClient): OasClient {
  return client as unknown as OasClient;
}

describe('runTick', () => {
  let stateDir: string;
  let store: EscalationStore;
  let client: FakeClient;

  beforeEach(() => {
    stateDir = mkdtempSync(join(tmpdir(), 'dsh-oas-tick-'));
    store = new EscalationStore(stateDir);
    client = makeClient();
    client.briefing.mockResolvedValue(briefing());
    client.listTasks.mockResolvedValue([]);
    client.setRunning.mockResolvedValue(writeResult());
    client.runTaskNow.mockResolvedValue(writeResult());
    client.setTaskEnabled.mockResolvedValue(writeResult());
    client.emergencyStop.mockResolvedValue({
      ok: true,
      stopped: [],
      failed: [],
      hold: { active: false, until: null, reason: '' },
    });
    client.journal.mockResolvedValue({ path: 'journal.md' });
  });

  afterEach(() => {
    rmSync(stateDir, { recursive: true, force: true });
  });

  it('starts an INACTIVE instance without trying anything else', async () => {
    client.briefing.mockResolvedValue(briefing({ state: 'INACTIVE' }));

    const result = await runTick({
      client: asClient(client),
      store,
      instance: INSTANCE,
      now: NOW,
    });

    expect(client.setRunning).toHaveBeenCalledTimes(1);
    expect(client.setRunning).toHaveBeenCalledWith(INSTANCE, true);
    expect(client.runTaskNow).not.toHaveBeenCalled();
    expect(client.setTaskEnabled).not.toHaveBeenCalled();
    expect(client.emergencyStop).not.toHaveBeenCalled();
    expect(result.actions).toEqual([
      expect.objectContaining({ kind: 'start_instance' }),
    ]);
    expect(result.skipped).toBe(false);
    expect(result.errors).toEqual([]);
  });

  it('runs an enabled must task at the front of the queue', async () => {
    client.briefing.mockResolvedValue(
      briefing({ plan: { must: ['Exploration'] } }),
    );
    client.listTasks.mockResolvedValue([taskView('Exploration', true)]);

    const result = await runTick({
      client: asClient(client),
      store,
      instance: INSTANCE,
      now: NOW,
    });

    expect(client.runTaskNow).toHaveBeenCalledTimes(1);
    expect(client.runTaskNow).toHaveBeenCalledWith(
      INSTANCE,
      'Exploration',
      'front',
    );
    expect(result.applied).toHaveLength(1);
    expect(result.errors).toEqual([]);
  });

  it('stops on a 423 hold and reports skipped', async () => {
    client.briefing.mockResolvedValue(briefing({ state: 'INACTIVE' }));
    client.setRunning.mockRejectedValue(
      new ApiError(423, { error: 'hold_active', reason: 'playing' }),
    );

    const result = await runTick({
      client: asClient(client),
      store,
      instance: INSTANCE,
      now: NOW,
    });

    expect(result.skipped).toBe(true);
    expect(result.errors).toHaveLength(1);
    expect(result.applied).toEqual([]);
    expect(client.runTaskNow).not.toHaveBeenCalled();
  });

  it('turns a 403 into a protected_task escalation', async () => {
    client.briefing.mockResolvedValue(
      briefing({ plan: { must: ['Exploration'] } }),
    );
    client.listTasks.mockResolvedValue([taskView('Exploration', true)]);
    client.runTaskNow.mockRejectedValue(
      new ApiError(403, {
        error: 'needs_approval',
        task: 'Exploration',
        approval_token: 'approval-123',
      }),
    );

    const result = await runTick({
      client: asClient(client),
      store,
      instance: INSTANCE,
      now: NOW,
    });

    expect(result.errors).toHaveLength(1);
    expect(result.skipped).toBe(false);

    const open = store.list('open');
    expect(open).toHaveLength(1);
    expect(open[0]).toMatchObject({
      code: 'protected_task',
      instance: INSTANCE,
      task: 'Exploration',
    });
  });

  it('emergency-stops when several tasks have failed', async () => {
    client.briefing.mockResolvedValue(
      briefing({ attempts: { Exploration: 1, WantedQuests: 1, SoulsTidy: 1 } }),
    );

    const result = await runTick({
      client: asClient(client),
      store,
      instance: INSTANCE,
      now: NOW,
    });

    expect(client.emergencyStop).toHaveBeenCalledTimes(1);
    expect(result.actions).toEqual([
      expect.objectContaining({ kind: 'emergency_stop' }),
    ]);
  });

  it('records an escalation even when its journal write fails', async () => {
    client.briefing.mockResolvedValue(briefing({ state: 'WARNING' }));
    client.journal.mockRejectedValue(new Error('journal unavailable'));

    const result = await runTick({
      client: asClient(client),
      store,
      instance: INSTANCE,
      now: NOW,
    });

    expect(result.errors).toEqual([]);
    expect(store.list('open')).toHaveLength(1);
    expect(store.list('open')[0]!.code).toBe('instance_warning');
  });
});
