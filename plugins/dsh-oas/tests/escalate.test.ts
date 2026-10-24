import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { EscalationStore } from '../src/escalate.js';

describe('EscalationStore', () => {
  let stateDir: string;
  let store: EscalationStore;

  beforeEach(() => {
    stateDir = mkdtempSync(join(tmpdir(), 'dsh-oas-escalations-'));
    store = new EscalationStore(stateDir);
  });

  afterEach(() => {
    rmSync(stateDir, { recursive: true, force: true });
  });

  it('writes an open escalation and lists it newest first', () => {
    const escalation = store.write({
      code: 'instance_warning',
      instance: 'oas1',
      reason: 'instance is WARNING',
    });

    expect(escalation).not.toBeNull();
    expect(escalation!.id).toMatch(/^ESC-\d{8}-\d{6}-instance_warning$/);
    expect(escalation!.status).toBe('open');
    expect(store.list()).toEqual([escalation]);
    expect(store.list('open')).toEqual([escalation]);
    expect(store.list('resolved')).toEqual([]);
  });

  it('deduplicates an open escalation with the same code, instance and task', () => {
    const input = {
      code: 'protected_task' as const,
      instance: 'oas1',
      task: 'MysteryShop',
      reason: 'needs approval',
    };

    const first = store.write(input);
    const second = store.write(input);

    expect(first).not.toBeNull();
    expect(second).toBeNull();
    expect(store.list('open')).toHaveLength(1);
  });

  it('resolves an escalation and records the note', () => {
    const escalation = store.write({
      code: 'retry_exhausted',
      instance: 'oas1',
      task: 'Exploration',
      reason: 'retry budget exhausted',
    });

    expect(store.resolve(escalation!.id, 'checked manually')).toBe(true);

    const resolved = store.list('resolved');
    expect(resolved).toHaveLength(1);
    expect(resolved[0]).toMatchObject({
      id: escalation!.id,
      status: 'resolved',
      note: 'checked manually',
    });
    expect(resolved[0]!.resolved_at).toBeTruthy();
    expect(store.list('open')).toEqual([]);
  });

  it('ignores unparseable files', () => {
    const escalation = store.write({
      code: 'state_unreadable',
      instance: 'oas1',
      reason: 'missing state',
    });
    writeFileSync(join(stateDir, 'broken.json'), '{not-json', 'utf8');

    expect(store.list()).toEqual([escalation]);
  });
});
