import { describe, expect, it } from 'vitest';

import {
  decide,
  hourInZone,
  inQuietHours,
  isoWeek,
  toBucket,
  toSnake,
  weekdayInZone,
} from '../src/plan.js';
import type { Briefing, InstanceView, TaskView } from '../src/types.js';

const INSTANCE = 'oas1';

/** Saturday 2026-10-24, 10:00 in the OAS default timezone (ISO week 43). */
const SATURDAY = new Date('2026-10-24T10:00:00+08:00');

interface BucketOptions {
  done?: string[];
  skipped?: string[];
  attempts?: Record<string, number>;
}

interface BriefingOptions extends BucketOptions {
  hold?: boolean;
  instances?: InstanceView[];
  plan?: Record<string, unknown>;
  state?: Record<string, unknown>;
}

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

function stateDocument(options: BucketOptions = {}): Record<string, unknown> {
  return {
    date: '2026-10-24',
    instances: {
      [INSTANCE]: {
        done: options.done ?? [],
        skipped: options.skipped ?? [],
        attempts: options.attempts ?? {},
      },
    },
    weekly: {},
  };
}

function briefing(options: BriefingOptions = {}): Briefing {
  const state = options.state ?? stateDocument(options);
  return {
    now: '2026-10-24T10:00:00',
    instances: options.instances ?? [instanceView()],
    plan: options.plan ?? {},
    state,
    hold: { active: options.hold ?? false, until: null, reason: '' },
    playbook: '',
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

describe('decide', () => {
  it('emits nothing while the hold gate is active', () => {
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({ hold: true, plan: { must: ['Exploration'] } }),
      tasks: [taskView('Exploration')],
      now: SATURDAY,
    });

    expect(actions).toEqual([]);
  });

  it('escalates when the instance is missing from the briefing', () => {
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({ instances: [] }),
      tasks: [],
      now: SATURDAY,
    });

    expect(actions).toEqual([
      expect.objectContaining({ kind: 'escalate', code: 'state_unreadable' }),
    ]);
  });

  it('escalates on a WARNING instance', () => {
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({ instances: [instanceView({ state: 'WARNING' })] }),
      tasks: [],
      now: SATURDAY,
    });

    expect(actions).toEqual([
      expect.objectContaining({ kind: 'escalate', code: 'instance_warning' }),
    ]);
  });

  it('starts an INACTIVE instance outside quiet hours', () => {
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({
        instances: [instanceView({ state: 'INACTIVE' })],
        plan: { budget: { quiet_hours: ['23:30-07:00'] } },
      }),
      tasks: [],
      now: SATURDAY,
    });

    expect(actions).toEqual([
      expect.objectContaining({ kind: 'start_instance' }),
    ]);
    expect(actions[0]!.reason).toBeTruthy();
  });

  it('does not start an INACTIVE instance during quiet hours', () => {
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({
        instances: [instanceView({ state: 'INACTIVE' })],
        plan: { budget: { quiet_hours: ['23:30-07:00'] } },
      }),
      tasks: [],
      now: new Date('2026-10-24T02:00:00+08:00'),
    });

    expect(actions).toEqual([]);
  });

  it('escalates a task that has exhausted its retry budget instead of running it', () => {
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({
        plan: { must: ['Exploration'], on_failure: { retry: 2 } },
        attempts: { Exploration: 2 },
      }),
      tasks: [taskView('Exploration')],
      now: SATURDAY,
    });

    expect(actions).toEqual([
      expect.objectContaining({
        kind: 'escalate',
        code: 'retry_exhausted',
        task: 'Exploration',
      }),
    ]);
    expect(actions.some((action) => action.kind === 'run_task')).toBe(false);
  });

  it('escalates a due protected task and never runs it', () => {
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({
        plan: {
          must: ['Exploration', 'MysteryShop'],
          protected_tasks: ['MysteryShop'],
        },
      }),
      tasks: [taskView('Exploration'), taskView('MysteryShop')],
      now: SATURDAY,
    });

    expect(actions).toEqual([
      expect.objectContaining({
        kind: 'escalate',
        code: 'protected_task',
        task: 'MysteryShop',
      }),
    ]);
    expect(actions.some((action) => action.kind === 'run_task')).toBe(false);
  });

  it('runs a weekly task on its weekday when this ISO week has no record', () => {
    // 2026-10-24 is a Saturday: ISO weekday 6.
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({ plan: { weekly: [{ task: 'Duel', days: [6] }] } }),
      tasks: [taskView('Duel')],
      now: SATURDAY,
    });

    expect(actions).toEqual([
      expect.objectContaining({
        kind: 'run_task',
        task: 'Duel',
        position: 'front',
      }),
    ]);
  });

  it('skips a weekly task already recorded for this ISO week', () => {
    const state = stateDocument();
    state.weekly = { [INSTANCE]: { Duel: isoWeek(SATURDAY) } };

    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({
        plan: { weekly: [{ task: 'Duel', days: [6] }] },
        state,
      }),
      tasks: [taskView('Duel')],
      now: SATURDAY,
    });

    expect(actions).toEqual([]);
  });

  it('skips a weekly task on a non-matching weekday', () => {
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({ plan: { weekly: [{ task: 'Duel', days: [1, 3] }] } }),
      tasks: [taskView('Duel')],
      now: SATURDAY,
    });

    expect(actions).toEqual([]);
  });

  it('enables a due but disabled must task before running it', () => {
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({ plan: { must: ['MysteryShop'] } }),
      tasks: [taskView('mystery_shop', false)],
      now: SATURDAY,
    });

    expect(actions).toEqual([
      expect.objectContaining({ kind: 'enable_task', task: 'MysteryShop' }),
    ]);
  });

  it('runs a due and enabled must task', () => {
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({ plan: { must: ['Exploration'] } }),
      tasks: [taskView('Exploration', true)],
      now: SATURDAY,
    });

    expect(actions).toEqual([
      expect.objectContaining({
        kind: 'run_task',
        task: 'Exploration',
        position: 'front',
      }),
    ]);
  });

  it('never re-runs a must task that is done or skipped', () => {
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({
        plan: { must: ['Exploration', 'WantedQuests', 'SoulsTidy'] },
        done: ['Exploration'],
        skipped: ['WantedQuests'],
      }),
      tasks: [
        taskView('Exploration'),
        taskView('WantedQuests'),
        taskView('SoulsTidy'),
      ],
      now: SATURDAY,
    });

    expect(actions).toEqual([
      expect.objectContaining({ kind: 'run_task', task: 'SoulsTidy' }),
    ]);
  });

  it('emergency-stops after three distinct tasks have failed', () => {
    const actions = decide({
      instance: INSTANCE,
      briefing: briefing({
        attempts: { Exploration: 1, WantedQuests: 1, SoulsTidy: 1 },
      }),
      tasks: [],
      now: SATURDAY,
    });

    expect(actions).toEqual([
      expect.objectContaining({ kind: 'emergency_stop' }),
    ]);
  });

  it('does not mutate its inputs', () => {
    const plan = { must: ['Exploration'], on_failure: { retry: 2 } };
    const state = stateDocument({ attempts: { Exploration: 1 } });
    const tasks = [taskView('Exploration')];
    const snapshot = JSON.stringify({ plan, state, tasks });

    decide({
      instance: INSTANCE,
      briefing: briefing({ plan, state }),
      tasks,
      now: SATURDAY,
    });

    expect(JSON.stringify({ plan, state, tasks })).toBe(snapshot);
  });
});

describe('time helpers', () => {
  it('formats an ISO week for a date', () => {
    expect(isoWeek(new Date('2026-10-24T00:00:00+08:00'))).toMatch(/^2026-W/);
    expect(isoWeek(new Date('2026-10-24T00:00:00+08:00'))).toBe('2026-W43');
  });

  it('maps a date to an ISO weekday in a timezone', () => {
    expect(weekdayInZone(SATURDAY, 'Asia/Shanghai')).toBe(6);
    expect(weekdayInZone(new Date('2026-10-25T00:30:00+08:00'), 'Asia/Shanghai')).toBe(7);
    expect(hourInZone(SATURDAY, 'Asia/Shanghai')).toBe(10);
  });

  it('treats a quiet-hours window as an exclusive-end range', () => {
    expect(inQuietHours(2, ['23:30-07:00'])).toBe(true);
    expect(inQuietHours(12, ['23:30-07:00'])).toBe(false);
    expect(inQuietHours(23, ['23:30-07:00'])).toBe(false);
    expect(inQuietHours(7, ['23:30-07:00'])).toBe(false);
    expect(inQuietHours(2, [])).toBe(false);
  });
});

describe('toSnake', () => {
  it('normalises the plan CamelCase used for comparison', () => {
    expect(toSnake('MysteryShop')).toBe('mystery_shop');
    expect(toSnake('TrueOrochi')).toBe('true_orochi');
    expect(toSnake('mystery_shop')).toBe('mystery_shop');
  });
});

describe('toBucket', () => {
  it('returns an empty bucket when the instance has no state', () => {
    expect(toBucket({}, INSTANCE)).toEqual({
      done: [],
      skipped: [],
      attempts: {},
    });
    expect(toBucket(null, INSTANCE)).toEqual({
      done: [],
      skipped: [],
      attempts: {},
    });
  });

  it('reads the per-instance bucket without mutating it', () => {
    const state = stateDocument({ done: ['Exploration'], attempts: { Exploration: 1 } });
    const bucket = toBucket(state, INSTANCE);

    expect(bucket).toEqual({
      done: ['Exploration'],
      skipped: [],
      attempts: { Exploration: 1 },
    });
    expect(bucket.done).not.toBe(
      (state.instances as Record<string, { done: string[] }>)[INSTANCE]!.done,
    );
  });
});
