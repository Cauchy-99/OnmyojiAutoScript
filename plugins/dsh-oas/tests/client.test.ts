import { describe, expect, it, vi } from 'vitest';

import { OasClient } from '../src/client.js';
import { ApiError } from '../src/types.js';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function makeClient(): {
  client: OasClient;
  fetchMock: ReturnType<typeof vi.fn<typeof fetch>>;
} {
  const fetchMock = vi.fn<typeof fetch>();
  return { client: new OasClient('http://x', fetchMock), fetchMock };
}

function callAt(
  fetchMock: ReturnType<typeof vi.fn<typeof fetch>>,
  index: number,
): [RequestInfo | URL, RequestInit | undefined] {
  return fetchMock.mock.calls[index]!;
}

describe('OasClient', () => {
  it('reads a per-instance briefing from the query string', async () => {
    const briefing = {
      now: '2026-10-24T10:00:00',
      instances: [],
      plan: {},
      state: {},
      hold: { active: false, until: null, reason: '' },
      playbook: '',
    };
    const { client, fetchMock } = makeClient();
    fetchMock.mockResolvedValueOnce(jsonResponse(briefing));

    await expect(client.briefing('oas1')).resolves.toEqual(briefing);

    const [url, init] = callAt(fetchMock, 0);
    expect(url).toBe('http://x/api/agent/briefing?instance=oas1');
    expect(init?.method).toBe('GET');
  });

  it('enables a task with its approval token in the body', async () => {
    const { client, fetchMock } = makeClient();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        ok: true,
        applied_at: '2026-10-24T10:00:00',
        effective: 'next_task_boundary',
        detail: {},
      }),
    );

    await client.setTaskEnabled('oas1', 'MysteryShop', true, 'tok');

    const [url, init] = callAt(fetchMock, 0);
    expect(url).toBe('http://x/api/agent/oas1/tasks/MysteryShop/enable');
    expect(init?.method).toBe('POST');
    expect(init?.headers).toMatchObject({
      'Content-Type': 'application/json',
    });
    expect(JSON.parse(String(init?.body))).toEqual({
      enabled: true,
      approval_token: 'tok',
    });
  });

  it('runs a task now through the /run_now path', async () => {
    const { client, fetchMock } = makeClient();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        ok: true,
        applied_at: '2026-10-24T10:00:00',
        effective: 'immediate',
        detail: {},
      }),
    );

    await client.runTaskNow('oas1', 'Exploration');

    const [url, init] = callAt(fetchMock, 0);
    expect(url).toBe('http://x/api/agent/oas1/tasks/Exploration/run_now');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({ position: 'front' });
  });

  it('maps a 423 response to ApiError with FastAPI detail', async () => {
    const detail = { error: 'hold_active', reason: 'playing', until: '2026-10-24 12:00:00' };
    const { client, fetchMock } = makeClient();
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail }, 423));

    let caught: unknown;
    try {
      await client.hold(30, 'manual');
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(ApiError);
    const apiError = caught as ApiError;
    expect(apiError.status).toBe(423);
    expect(apiError.detail).toEqual(detail);
  });

  it('keeps an approval token from a 403 detail object', async () => {
    const detail = {
      error: 'needs_approval',
      task: 'MysteryShop',
      approval_token: 'approval-123',
    };
    const { client, fetchMock } = makeClient();
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail }, 403));

    let caught: unknown;
    try {
      await client.setTaskEnabled('oas1', 'MysteryShop', true);
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(ApiError);
    const apiError = caught as ApiError;
    expect(apiError.status).toBe(403);
    expect(apiError.detail).toEqual(detail);
    expect((apiError.detail as { approval_token: string }).approval_token).toBe(
      'approval-123',
    );
  });

  it('releases the hold with DELETE /api/agent/hold', async () => {
    const { client, fetchMock } = makeClient();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ hold: { active: false, until: null, reason: '' } }),
    );

    await client.releaseHold();

    const [url, init] = callAt(fetchMock, 0);
    expect(url).toBe('http://x/api/agent/hold');
    expect(init?.method).toBe('DELETE');
  });

  it('URL-encodes instance and task path segments', async () => {
    const { client, fetchMock } = makeClient();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        ok: true,
        applied_at: '2026-10-24T10:00:00',
        effective: 'immediate',
        detail: {},
      }),
    );

    await client.setNextRun('oas 1/x', 'Mystery Shop/2026', '2026-10-25 07:00:00');

    const [url] = callAt(fetchMock, 0);
    expect(url).toBe(
      'http://x/api/agent/oas%201%2Fx/tasks/Mystery%20Shop%2F2026/next_run',
    );
  });
});
