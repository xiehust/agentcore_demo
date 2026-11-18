// Trusted test-only filesystem probe, not an endpoint for untrusted users.
import http from 'node:http';
import { readFileSync, readdirSync, openSync, writeSync, fsyncSync, closeSync, statSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';

const ROOT = process.env.DSH_WORKDIR || '/workspace';
const HOME = process.env.DSH_HOME || path.join(ROOT, '.dsh');
const identity = {
  run_id: process.env.BENCH_RUN_ID || 'local', sandbox_id: process.env.SANDBOX_ID || 'local',
  boot_id: readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim(),
};
let quiesced = false;
let busy = false;

function target(body) {
  if (!['workspace', 'home', 'volatile'].includes(body.scope)
      || typeof body.file !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(body.file)) {
    throw new Error('invalid file selection');
  }
  const base = body.scope === 'home' ? HOME : body.scope === 'volatile' ? '/tmp' : ROOT;
  return path.join(base, body.file);
}

function writeDurable(file, text) {
  if (typeof text !== 'string' || Buffer.byteLength(text) > 4096) throw new Error('invalid text');
  const buffer = Buffer.from(text);
  const fd = openSync(file, 'w', 0o600);
  try {
    let offset = 0;
    while (offset < buffer.length) {
      const n = writeSync(fd, buffer, offset, buffer.length - offset);
      if (n <= 0) throw new Error('write made no progress');
      offset += n;
    }
    fsyncSync(fd);
  } finally { closeSync(fd); }
  const dir = openSync(path.dirname(file), 'r');
  try { fsyncSync(dir); } finally { closeSync(dir); }
}

function readBounded(file) {
  try {
    if (statSync(file).size > 4096) throw new Error('file too large');
    return { exists: true, text: readFileSync(file, 'utf8') };
  } catch (err) {
    if (err.code === 'ENOENT') return { exists: false, text: null };
    throw err;
  }
}

function run(program, args) {
  const result = spawnSync(program, args, { timeout: 10000, maxBuffer: 65536,
    env: { PATH: process.env.PATH, HOME: '/home/dsh', DSH_HOME: HOME, AWS_EC2_METADATA_DISABLED: 'true' },
    cwd: '/' });
  if (result.status !== 0 || result.error) throw new Error(`command failed: ${program}`);
  return result.stdout.toString();
}

function health() {
  const mounts = readFileSync('/proc/mounts', 'utf8').split('\n');
  const mount = mounts.find(line => line.split(' ')[1] === ROOT) || null;
  return { ...identity, workspace: ROOT, dsh_home: HOME, mount,
    filesystem_uuid: quiesced ? null : run('blkid', ['-s', 'UUID', '-o', 'value', '/dev/vda']).trim(),
    block_devices: readdirSync('/sys/block'), quiesced };
}

function body(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', chunk => {
      size += chunk.length;
      if (size > 16384) { reject(new Error('body too large')); req.destroy(); return; }
      chunks.push(chunk);
    });
    req.on('end', () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); } catch (err) { reject(err); }
    });
    req.on('error', reject);
  });
}

function send(res, code, data) {
  res.writeHead(code, { 'content-type': 'application/json' });
  res.end(JSON.stringify({ ...identity, ...data }));
}

if (process.env.PROBE_NO_SERVER !== '1') http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    try { send(res, 200, health()); } catch { send(res, 500, { error: 'health failed' }); }
    return;
  }
  if (req.method !== 'POST') return send(res, 404, { error: 'not found' });
  if (busy || quiesced) return send(res, 409, { error: 'busy or unmounted' });
  busy = true;
  try {
    const data = await body(req);
    if (req.url === '/write') {
      const file = target(data);
      writeDurable(file, data.text);
      return send(res, 200, { ...readBounded(file), fsynced: true });
    }
    if (req.url === '/read') return send(res, 200, readBounded(target(data)));
    if (req.url === '/dsh-help') {
      const stdout = run('/opt/dsh/node_modules/.bin/dsh', ['headless', '--help']);
      return send(res, 200, { exit_code: 0, stock_help: stdout.includes('Usage: dsh'), dsh_home: HOME });
    }
    if (req.url === '/quiesce') {
      run('sync', []);
      run('umount', [ROOT]);
      quiesced = true;
      return send(res, 200, { unmounted: true });
    }
    send(res, 404, { error: 'not found' });
  } catch { send(res, 400, { error: 'probe operation failed' }); }
  finally { busy = false; }
}).listen(8090, process.env.HOST);

export { target, writeDurable, readBounded };
