import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, symlinkSync } from 'node:fs';
import http from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';

test('loopback wrapper validates paths, symlinks, body size, startup and busy gate', async () => {
  const workspace = mkdtempSync(path.join(tmpdir(), 'ecs-dsh-unit-'));
  const serverFile = fileURLToPath(new URL('../app/server.mjs', import.meta.url));
  const child = spawn(process.execPath, [serverFile], { env: { ...process.env,
    DSH_WORKDIR: workspace, DSH_HOME: path.join(workspace, '.dsh'), DSH_BIN: '/bin/true',
    PRIVATE_BIND: 'false', HOST: '127.0.0.1', PORT: '0' }, stdio: ['ignore', 'pipe', 'pipe'] });
  let output = '';
  child.stdout.on('data', chunk => { output += chunk; });
  child.stderr.on('data', chunk => { output += chunk; });
  try {
    const deadline = Date.now() + 5000;
    while (!output.includes('"event":"ready"')) {
      if (child.exitCode !== null || Date.now() > deadline) throw new Error(output || 'server not ready');
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    const ready = JSON.parse(output.trim().split('\n')[0]);
    const endpoint = `http://127.0.0.1:${ready.port}`;
    const post = body => fetch(endpoint + '/fs', { method: 'POST', body: JSON.stringify(body) });
    const written = await post({ op: 'write', scope: 'workspace', file: 'marker', text: 'private' });
    assert.equal((await written.json()).text, 'private');
    assert.equal((await (await post({ op: 'read', scope: 'home', file: 'marker' })).json()).exists, false);
    assert.equal((await post({ op: 'read', scope: 'workspace', file: '../marker' })).status, 400);
    symlinkSync('/etc/hostname', path.join(workspace, 'link'));
    assert.equal((await post({ op: 'read', scope: 'workspace', file: 'link' })).status, 400);
    assert.equal((await post({ op: 'write', scope: 'workspace', file: 'link', text: 'bad' })).status, 400);
    assert.equal((await post({ op: 'write', scope: 'workspace', file: 'marker', text: 'x'.repeat(4097) })).status, 400);
    const slow = http.request(endpoint + '/fs', { method: 'POST' });
    slow.flushHeaders();
    slow.write('{');
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal((await post({ op: 'read', scope: 'workspace', file: 'marker' })).status, 409);
    const responsePromise = once(slow, 'response');
    slow.end('}');
    const [slowResponse] = await responsePromise;
    slowResponse.resume();
    await once(slowResponse, 'end');
    const invoked = await fetch(endpoint + '/invoke', { method: 'POST', body: JSON.stringify({ mode: 'startup' }) });
    assert.equal((await invoked.json()).result.ok, true);
    const invalid = await fetch(endpoint + '/invoke', { method: 'POST', body: JSON.stringify({ mode: 'shell' }) });
    assert.equal(invalid.status, 400);
    const cpu = await fetch(endpoint + '/cpu', { method: 'POST', body: JSON.stringify({ duration_ms: 1000 }) });
    const cpuResult = await cpu.json();
    assert.equal(cpu.status, 200);
    assert.ok(cpuResult.operations > 0);
    assert.ok(cpuResult.operations_per_second > 0);
    const invalidCpu = await fetch(endpoint + '/cpu', { method: 'POST', body: JSON.stringify({ duration_ms: 10 }) });
    assert.equal(invalidCpu.status, 400);
    assert.equal((await fetch(endpoint + '/missing')).status, 404);
  } finally {
    child.kill('SIGTERM');
    if (child.exitCode === null) await once(child, 'exit');
    rmSync(workspace, { recursive: true, force: true });
  }
});
