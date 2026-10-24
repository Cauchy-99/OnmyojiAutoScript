import {
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

import type { EscalationCode } from './plan.js';

export interface Escalation {
  id: string;
  created_at: string;
  code: EscalationCode;
  instance: string;
  task?: string;
  reason: string;
  status: 'open' | 'resolved';
  note?: string;
  resolved_at?: string;
}

interface EscalationInput {
  code: EscalationCode;
  instance: string;
  task?: string;
  reason: string;
}

const ESCALATION_CODES: ReadonlySet<string> = new Set<EscalationCode>([
  'instance_warning',
  'retry_exhausted',
  'protected_task',
  'repeated_failures',
  'state_unreadable',
]);

const ID_PATTERN = /^ESC-\d{8}-\d{6}-[a-z_]+$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

/** `ESC-YYYYMMDD-HHMMSS` in UTC, matching `created_at`'s calendar. */
function idStamp(date: Date): string {
  const iso = date.toISOString();
  const [day, time] = iso.split('T');
  return `${day!.replace(/-/g, '')}-${time!.slice(0, 8).replace(/:/g, '')}`;
}

function readEscalation(path: string): Escalation | null {
  try {
    const value: unknown = JSON.parse(readFileSync(path, 'utf8'));
    if (!isRecord(value)) return null;

    const { id, created_at: createdAt, code, instance, reason, status } = value;
    if (
      typeof id !== 'string'
      || typeof createdAt !== 'string'
      || typeof code !== 'string'
      || !ESCALATION_CODES.has(code)
      || typeof instance !== 'string'
      || typeof reason !== 'string'
      || (status !== 'open' && status !== 'resolved')
    ) {
      return null;
    }

    const escalation: Escalation = {
      id,
      created_at: createdAt,
      code: code as EscalationCode,
      instance,
      reason,
      status,
    };
    const task = optionalString(value.task);
    if (task !== undefined) escalation.task = task;
    const note = optionalString(value.note);
    if (note !== undefined) escalation.note = note;
    const resolvedAt = optionalString(value.resolved_at);
    if (resolvedAt !== undefined) escalation.resolved_at = resolvedAt;
    return escalation;
  } catch {
    return null;
  }
}

/**
 * File-backed escalations the resident supervisor raises for the agent.
 *
 * One JSON document per escalation keeps the store append-only in practice:
 * `write` dedupes open entries by code/instance/task, and `resolve` is the
 * only operation that replaces an existing file.
 */
export class EscalationStore {
  private readonly stateDir: string;
  private tempCounter = 0;

  constructor(stateDir?: string) {
    this.stateDir = stateDir ?? join(homedir(), '.dsh', 'oas-escalations');
  }

  write(input: EscalationInput): Escalation | null {
    const alreadyOpen = this.list('open').some(
      (entry) =>
        entry.code === input.code
        && entry.instance === input.instance
        && entry.task === input.task,
    );
    if (alreadyOpen) return null;

    let createdAt = new Date();
    let id = `ESC-${idStamp(createdAt)}-${input.code}`;
    // The id has second precision. A different task with the same code in the
    // same second must not overwrite the first file, so advance the stamp by a
    // second until the name is free.
    for (let bump = 1; this.fileExists(id); bump += 1) {
      createdAt = new Date(createdAt.getTime() + 1_000);
      id = `ESC-${idStamp(createdAt)}-${input.code}`;
    }

    const escalation: Escalation = {
      id,
      created_at: createdAt.toISOString(),
      code: input.code,
      instance: input.instance,
      reason: input.reason,
      status: 'open',
    };
    if (input.task !== undefined) escalation.task = input.task;

    this.persist(escalation);
    return escalation;
  }

  list(status?: 'open' | 'resolved'): Escalation[] {
    let names: string[];
    try {
      names = readdirSync(this.stateDir);
    } catch {
      return [];
    }

    const entries: Escalation[] = [];
    for (const name of names) {
      if (!name.endsWith('.json')) continue;
      const entry = readEscalation(join(this.stateDir, name));
      if (entry === null) continue;
      if (status !== undefined && entry.status !== status) continue;
      entries.push(entry);
    }

    entries.sort(
      (left, right) =>
        right.created_at.localeCompare(left.created_at)
        || right.id.localeCompare(left.id),
    );
    return entries;
  }

  resolve(id: string, note?: string): boolean {
    if (!ID_PATTERN.test(id)) return false;

    const path = join(this.stateDir, `${id}.json`);
    const current = readEscalation(path);
    if (current === null) return false;

    const resolved: Escalation = {
      ...current,
      status: 'resolved',
      resolved_at: new Date().toISOString(),
    };
    if (note !== undefined) resolved.note = note;

    this.persist(resolved);
    return true;
  }

  private fileExists(id: string): boolean {
    const path = join(this.stateDir, `${id}.json`);
    try {
      readFileSync(path);
      return true;
    } catch {
      return false;
    }
  }

  private persist(escalation: Escalation): void {
    mkdirSync(this.stateDir, { recursive: true });

    const destination = join(this.stateDir, `${escalation.id}.json`);
    this.tempCounter += 1;
    const temporary = join(
      this.stateDir,
      `.${escalation.id}.${process.pid}.${this.tempCounter}.tmp`,
    );

    try {
      writeFileSync(temporary, `${JSON.stringify(escalation, null, 2)}\n`, 'utf8');
      renameSync(temporary, destination);
    } catch (error) {
      try {
        unlinkSync(temporary);
      } catch {
        // The temp file may never have been created; the original error wins.
      }
      throw error;
    }
  }
}
