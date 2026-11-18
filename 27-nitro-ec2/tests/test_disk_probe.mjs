import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const root = mkdtempSync(path.join(os.tmpdir(), 'disk-probe-test-'));
process.env.PROBE_NO_SERVER = '1';
process.env.DSH_WORKDIR = root;
process.env.DSH_HOME = path.join(root, '.dsh');
mkdirSync(process.env.DSH_HOME);
const probe = await import('../guest/disk-probe.mjs');

try {
  test('file scope prevents traversal and unknown roots', () => {
    for (const file of ['../other', '/etc/passwd', '..', '.', 'nested/file']) {
      assert.throws(() => probe.target({ scope: 'workspace', file }));
    }
    assert.throws(() => probe.target({ scope: 'parent', file: 'file.txt' }));
    assert.equal(probe.target({ scope: 'home', file: 'marker.txt' }), path.join(root, '.dsh/marker.txt'));
  });
  test('write, fsync and bounded read return exact data', () => {
    const file = probe.target({ scope: 'workspace', file: 'shared.txt' });
    assert.equal(probe.readBounded(file).exists, false);
    probe.writeDurable(file, 'sentinel 中文');
    assert.deepEqual(probe.readBounded(file), { exists: true, text: 'sentinel 中文' });
    assert.throws(() => probe.writeDurable(file, 'x'.repeat(4097)));
  });
} finally {
  // Defer cleanup until queued test callbacks have run.
  process.on('exit', () => rmSync(root, { recursive: true, force: true }));
}
