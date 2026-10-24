/**
 * The pure decision core: one OAS instance, one tick.
 *
 * `decide()` performs no IO, reads no clock and never mutates its input. The
 * caller is responsible for fetching the briefing/tasks, executing the
 * returned actions and recording their outcome in `state`.
 */

import type { Briefing, InstanceView, TaskView } from './types.js';

export interface DecideInput {
  instance: string;
  briefing: Briefing;
  tasks: TaskView[];
  now: Date;
}

export type Action =
  | { kind: 'start_instance'; reason: string }
  | { kind: 'enable_task'; task: string; reason: string }
  | { kind: 'run_task'; task: string; position: 'front' | 'end'; reason: string }
  | { kind: 'emergency_stop'; reason: string }
  | { kind: 'escalate'; code: EscalationCode; task?: string; reason: string };

export type EscalationCode =
  | 'instance_warning'
  | 'retry_exhausted'
  | 'protected_task'
  | 'repeated_failures'
  | 'state_unreadable';

/** OAS defaults for fields a `daily_plan.yaml` may leave out. */
const DEFAULT_RETRY = 2;
const DEFAULT_TIMEZONE = 'Asia/Shanghai';
const DEFAULT_QUIET_HOURS: string[] = [];

/** Three tasks that already failed once each is a bad day for the instance. */
const DISTINCT_FAILURES_FOR_STOP = 3;

const MINUTES_PER_HOUR = 60;
const HOURS_PER_DAY = 24;

const WEEKDAY_NAMES: Record<string, number> = {
  Mon: 1,
  Tue: 2,
  Wed: 3,
  Thu: 4,
  Fri: 5,
  Sat: 6,
  Sun: 7,
};

/** The per-instance daily bucket kept in `state.json`. */
export interface TaskBucket {
  done: string[];
  skipped: string[];
  attempts: Record<string, number>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string');
}

function numberMap(value: unknown): Record<string, number> {
  const result: Record<string, number> = {};
  if (!isRecord(value)) return result;
  for (const [key, raw] of Object.entries(value)) {
    const count = typeof raw === 'number' ? raw : Number(raw);
    if (Number.isFinite(count)) result[key] = count;
  }
  return result;
}

/**
 * Normalise a task name for comparison: `plan.must` uses CamelCase while
 * `TaskView.task` is snake_case, so both sides are reduced to one spelling.
 */
export function toSnake(name: string): string {
  return name
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1_$2')
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .replace(/[\s-]+/g, '_')
    .trim()
    .toLowerCase();
}

/**
 * The daily bucket for `instance`, defaulting every missing part.
 *
 * `state` is the full `state.json` document (`{date, instances, weekly}`). A
 * per-instance briefing (`state` is already the bucket) or the legacy flat
 * shape is accepted as well, so the helper never throws on an old file.
 */
export function toBucket(state: unknown, instance: string): TaskBucket {
  const document = isRecord(state) ? state : {};
  const instances = document.instances;
  const scoped = isRecord(instances) ? instances[instance] : undefined;
  const bucket = isRecord(scoped) ? scoped : document;

  return {
    done: [...stringList(bucket.done)],
    skipped: [...stringList(bucket.skipped)],
    attempts: { ...numberMap(bucket.attempts) },
  };
}

/** ISO-8601 week (`2026-W43`) computed in UTC, independent of the host zone. */
export function isoWeek(date: Date): string {
  const target = new Date(
    Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()),
  );
  const weekday = target.getUTCDay() || 7;
  target.setUTCDate(target.getUTCDate() + 4 - weekday);

  const year = target.getUTCFullYear();
  const yearStart = Date.UTC(year, 0, 1);
  const week = Math.ceil(((target.getTime() - yearStart) / 86_400_000 + 1) / 7);
  return `${year}-W${String(week).padStart(2, '0')}`;
}

/** ISO weekday (1=Mon..7=Sun) of an instant in `timeZone`. */
export function weekdayInZone(date: Date, timeZone: string): number {
  const label = new Intl.DateTimeFormat('en-US', {
    timeZone,
    weekday: 'short',
  }).format(date);
  return WEEKDAY_NAMES[label] ?? 1;
}

/** Hour of day (0..23) of an instant in `timeZone`. */
export function hourInZone(date: Date, timeZone: string): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hour: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date);
  const hour = parts.find((part) => part.type === 'hour');
  const value = hour ? Number.parseInt(hour.value, 10) : 0;
  return Number.isFinite(value) ? value % HOURS_PER_DAY : 0;
}

function toMinutes(value: string): number | null {
  const match = /^(\d{1,2}):(\d{2})$/.exec(value.trim());
  if (!match) return null;
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  if (hours > 23 || minutes > 59) return null;
  return hours * MINUTES_PER_HOUR + minutes;
}

/**
 * Whether `hour` falls inside any `"HH:MM-HH:MM"` window. Windows ending before
 * they start wrap midnight; the end is exclusive (`07:00` is already awake).
 */
export function inQuietHours(hour: number, quietHours: string[]): boolean {
  if (!Number.isFinite(hour)) return false;
  const minute = Math.floor(hour) * MINUTES_PER_HOUR;

  for (const window of quietHours ?? []) {
    if (typeof window !== 'string') continue;
    const [rawStart, rawEnd] = window.split('-');
    const start = toMinutes(rawStart ?? '');
    const end = toMinutes(rawEnd ?? '');
    if (start === null || end === null || start === end) continue;

    if (start < end) {
      if (minute >= start && minute < end) return true;
    } else if (minute >= start || minute < end) {
      return true;
    }
  }
  return false;
}

function planTimezone(plan: Record<string, unknown>): string {
  return typeof plan.timezone === 'string' && plan.timezone !== ''
    ? plan.timezone
    : DEFAULT_TIMEZONE;
}

function planRetry(plan: Record<string, unknown>): number {
  const onFailure = plan.on_failure;
  if (!isRecord(onFailure)) return DEFAULT_RETRY;
  const retry = onFailure.retry;
  return typeof retry === 'number' && Number.isFinite(retry) && retry >= 0
    ? Math.floor(retry)
    : DEFAULT_RETRY;
}

function planQuietHours(plan: Record<string, unknown>): string[] {
  const budget = plan.budget;
  if (!isRecord(budget)) return [...DEFAULT_QUIET_HOURS];
  return stringList(budget.quiet_hours);
}

interface WeeklyEntry {
  task: string;
  days: number[];
}

function planWeekly(plan: Record<string, unknown>): WeeklyEntry[] {
  if (!Array.isArray(plan.weekly)) return [];
  const entries: WeeklyEntry[] = [];
  for (const raw of plan.weekly) {
    if (!isRecord(raw) || typeof raw.task !== 'string' || raw.task === '') continue;
    const days = Array.isArray(raw.days)
      ? raw.days
          .filter((day): day is number => typeof day === 'number')
          .filter((day) => Number.isInteger(day) && day >= 1 && day <= 7)
      : [];
    entries.push({ task: raw.task, days });
  }
  return entries;
}

function weeklyMarks(state: unknown, instance: string): Record<string, unknown> {
  if (!isRecord(state) || !isRecord(state.weekly)) return {};
  const scoped = state.weekly[instance];
  return isRecord(scoped) ? scoped : {};
}

function attemptsFor(attempts: Record<string, number>, task: string): number {
  const direct = attempts[task];
  if (typeof direct === 'number') return direct;
  const key = toSnake(task);
  for (const [name, count] of Object.entries(attempts)) {
    if (toSnake(name) === key) return count;
  }
  return 0;
}

function instanceState(instances: InstanceView[], instance: string): InstanceView | undefined {
  return instances.find((view) => view?.name === instance);
}

/**
 * Pick at most one action for this tick, applying the rules in order:
 * hold, unreadable state, warning, inactive, retry budget, protected task,
 * weekly, daily `must`, repeated failures.
 */
export function decide(input: DecideInput): Action[] {
  const { instance, briefing, tasks, now } = input;

  // 1. Never write through an active hold; the server would reject it.
  if (briefing.hold?.active === true) return [];

  // 2. Without a briefing view for this instance every later rule is unsafe.
  const instances = Array.isArray(briefing.instances) ? briefing.instances : [];
  const view = instanceState(instances, instance);
  if (view === undefined) {
    return [
      {
        kind: 'escalate',
        code: 'state_unreadable',
        reason: `instance ${instance} is missing from the briefing`,
      },
    ];
  }

  const plan = isRecord(briefing.plan) ? briefing.plan : {};
  const bucket = toBucket(briefing.state, instance);
  const timezone = planTimezone(plan);
  const retry = planRetry(plan);

  // 3. A warning instance is degraded; a human should look before we write.
  if (view.state === 'WARNING') {
    return [
      {
        kind: 'escalate',
        code: 'instance_warning',
        reason: `instance ${instance} reports WARNING`,
      },
    ];
  }

  // 4. An inactive instance is not running; start it unless the budget says
  //    this is quiet time.
  if (view.state === 'INACTIVE') {
    if (inQuietHours(hourInZone(now, timezone), planQuietHours(plan))) return [];
    return [
      {
        kind: 'start_instance',
        reason: `instance ${instance} is INACTIVE outside quiet hours`,
      },
    ];
  }

  const skipped = new Set(bucket.skipped.map(toSnake));
  const done = new Set(bucket.done.map(toSnake));

  // 5. The retry budget is spent: escalate instead of retrying forever, unless
  //    the caller already decided to skip the task.
  for (const [task, count] of Object.entries(bucket.attempts)) {
    if (count >= retry && !skipped.has(toSnake(task))) {
      return [
        {
          kind: 'escalate',
          code: 'retry_exhausted',
          task,
          reason: `${task} failed ${count} time(s), retry budget is ${retry}`,
        },
      ];
    }
  }

  const must = stringList(plan.must);
  const due = must.filter(
    (task) => !done.has(toSnake(task)) && !skipped.has(toSnake(task)),
  );

  // 6. A protected task always needs a human go-ahead, even when due.
  for (const task of stringList(plan.protected_tasks)) {
    const key = toSnake(task);
    if (due.some((entry) => toSnake(entry) === key)) {
      return [
        {
          kind: 'escalate',
          code: 'protected_task',
          task,
          reason: `${task} is due but protected; it needs human approval`,
        },
      ];
    }
  }

  // 7. One weekly task per tick, only on its weekday and only once per ISO
  //    week (the weekly bucket survives the daily rollover).
  const weekday = weekdayInZone(now, timezone);
  const week = isoWeek(now);
  const marks = weeklyMarks(briefing.state, instance);
  for (const entry of planWeekly(plan)) {
    if (!entry.days.includes(weekday)) continue;
    if (marks[entry.task] === week) continue;
    return [
      {
        kind: 'run_task',
        task: entry.task,
        position: 'front',
        reason: `weekly ${entry.task} is due on ISO weekday ${weekday} (${week})`,
      },
    ];
  }

  // 8. The first unfinished daily `must` task; enabling comes first because the
  //    scheduler skips disabled tasks.
  for (const task of due) {
    if (attemptsFor(bucket.attempts, task) >= retry) continue;

    const key = toSnake(task);
    const scheduled = tasks.find((entry) => toSnake(entry.task) === key);
    if (scheduled !== undefined && scheduled.enable === false) {
      return [
        {
          kind: 'enable_task',
          task,
          reason: `${task} is due but disabled in the scheduler`,
        },
      ];
    }
    return [
      {
        kind: 'run_task',
        task,
        position: 'front',
        reason: `${task} is due today and not finished`,
      },
    ];
  }

  // 9. Several distinct failures with nothing left to run: stop the instance
  //    and let the agent investigate.
  const failed = Object.values(bucket.attempts).filter((count) => count > 0)
    .length;
  if (failed >= DISTINCT_FAILURES_FOR_STOP) {
    return [
      {
        kind: 'emergency_stop',
        reason: `${failed} tasks have failed at least once on ${instance}`,
      },
    ];
  }

  // 10. Nothing to do.
  return [];
}
