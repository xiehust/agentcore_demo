// DSH HTTP benchmark wrapper for ECS isolated tasks. Not a public agent service.
import http from 'node:http';
import { spawn } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, chownSync, chmodSync, statSync,
  openSync, writeSync, fsyncSync, closeSync, statfsSync, constants, fstatSync, unlinkSync, ftruncateSync } from 'node:fs';
import { networkInterfaces } from 'node:os';
import { createRequire } from 'node:module';
import { performance } from 'node:perf_hooks';
import path from 'node:path';

const ROOT = process.env.DSH_WORKDIR || '/workspace';
const DSH_HOME = process.env.DSH_HOME || path.join(ROOT, '.dsh');
const DSH_BIN = process.env.DSH_BIN || '/opt/dsh/node_modules/.bin/dsh';
const RUN_ID = process.env.BENCH_RUN_ID || 'local';
const REGION = process.env.AWS_REGION || 'us-east-1';
const MODEL = process.env.DSH_MODEL_ID || 'deepseek.v3.2';
const BOOT_ID = readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim();
let busy = false;

for (const directory of [ROOT, '/tmp', DSH_HOME]) {
  mkdirSync(directory, { recursive: true });
  if (process.getuid?.() === 0 && statSync(directory).uid !== 1000) chownSync(directory, 1000, 1000);
}
if (process.getuid?.() === 0) { process.setgid(1000); process.setuid(1000); }
const profile = path.join(DSH_HOME, 'profiles', 'headless');
mkdirSync(profile, { recursive: true });
writeFileSync(path.join(profile, 'cordis.patch.yml'), [
  '- id: llm-pi-ai', '  config:', '    providers:', '      amazon-bedrock:',
  `        baseURL: https://bedrock-runtime.${REGION}.amazonaws.com`, '        models:',
  `          - id: ${JSON.stringify(MODEL)}`, '            maxTokens: 512',
  '- id: agent-default-model', '  config:', '    provider: amazon-bedrock',
  `    model: ${JSON.stringify(MODEL)}`, '',
].join('\n'));
let version = 'unknown';
try { version = JSON.parse(readFileSync('/opt/dsh/node_modules/@deepseek-ai/dsh/package.json', 'utf8')).version; } catch {}
const identity = { run_id: RUN_ID, boot_id: BOOT_ID, dsh_version: version,
  node_version: process.version, uid: process.getuid?.(), workdir: ROOT, dsh_home: DSH_HOME };
const elapsed = start => performance.now() - start;
const addresses = Object.values(networkInterfaces()).flat().filter(address =>
  address.family === 'IPv4' && !address.internal && /^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)/.test(address.address));
if (process.env.PRIVATE_BIND === 'true' && addresses.length !== 1) throw new Error('private task ENI is ambiguous');
const HOST = process.env.PRIVATE_BIND === 'true' ? addresses[0].address : (process.env.HOST || '127.0.0.1');

function fileTarget(body) {
  if (!['workspace', 'home', 'tmp'].includes(body.scope) || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(body.file || '')) {
    throw new Error('invalid file selection');
  }
  return path.join(body.scope === 'home' ? DSH_HOME : body.scope === 'tmp' ? '/tmp' : ROOT, body.file);
}

function fileOperation(body) {
  const file = fileTarget(body);
  if (body.op === 'write') {
    if (typeof body.text !== 'string' || Buffer.byteLength(body.text) > 4096) throw new Error('text too large');
    const fd = openSync(file, constants.O_WRONLY | constants.O_CREAT | constants.O_NOFOLLOW, 0o600);
    if (!fstatSync(fd).isFile() || fstatSync(fd).nlink !== 1) { closeSync(fd); throw new Error('not a private regular file'); }
    try { ftruncateSync(fd, 0); writeSync(fd, body.text); fsyncSync(fd); } finally { closeSync(fd); }
    const parent = openSync(path.dirname(file), 'r');
    try { fsyncSync(parent); } finally { closeSync(parent); }
  } else if (body.op !== 'read') throw new Error('invalid operation');
  try {
    const descriptor = openSync(file, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const info = fstatSync(descriptor);
      if (!info.isFile() || info.size > 4096 || info.nlink !== 1) throw new Error('not a private small file');
      return { exists: true, text: readFileSync(descriptor, 'utf8') };
    } finally { closeSync(descriptor); }
  } catch (error) {
    if (error.code === 'ENOENT') return { exists: false, text: null };
    throw error;
  }
}

function storageProbe() {
  let rootWriteError = null;
  const file = path.join('/opt/dsh', `readonly-probe-${process.pid}`);
  try { writeFileSync(file, 'probe', { flag: 'wx' }); unlinkSync(file); }
  catch (error) { rootWriteError = error.code; }
  return { root_write_error: rootWriteError, filesystems: [ROOT, DSH_HOME, '/tmp'].map(directory => {
    const info = statfsSync(directory);
    return { path: directory, type: info.type, capacity_bytes: info.bsize * info.blocks,
      available_bytes: info.bsize * info.bavail };
  }) };
}

function cpuProbe(body) {
  if (!Number.isInteger(body.duration_ms) || body.duration_ms < 1000 || body.duration_ms > 30000) {
    throw new Error('invalid CPU probe duration');
  }
  const started = performance.now();
  const deadline = started + body.duration_ms;
  let operations = 0;
  let checksum = 2166136261;
  while (performance.now() < deadline) {
    for (let index = 0; index < 4096; index++) {
      checksum = Math.imul(checksum ^ (operations + index), 16777619);
    }
    operations += 4096;
  }
  const elapsedMs = performance.now() - started;
  return { cpu_probe_ms: elapsedMs, operations,
    operations_per_second: operations * 1000 / elapsedMs, checksum: checksum >>> 0 };
}

async function runDsh(body) {
  if (!['startup', 'agent'].includes(body.mode)) throw new Error('invalid mode');
  if (body.mode === 'agent' && (typeof body.prompt !== 'string' || !body.prompt.trim() || body.prompt.length > 4000)) {
    throw new Error('invalid prompt');
  }
  const env = { ...process.env, DSH_HOME, AWS_REGION: REGION, AWS_MAX_ATTEMPTS: '1' };
  const result = { ok: false, exit_code: null, stop_reason: null, timed_out: false,
    first_event_ms: null, first_text_ms: null, total_ms: null, credentials_ms: 0,
    answer_matches_expected: null, error_class: null };
  if (body.mode === 'agent') {
    const credentialStart = performance.now();
    try {
      const require = createRequire(import.meta.url);
      const { defaultProvider } = require('@aws-sdk/credential-provider-node');
      const credentials = await defaultProvider()();
      env.AWS_ACCESS_KEY_ID = credentials.accessKeyId;
      env.AWS_SECRET_ACCESS_KEY = credentials.secretAccessKey;
      if (credentials.sessionToken) env.AWS_SESSION_TOKEN = credentials.sessionToken;
    } catch { result.error_class = 'credential_resolution'; return result; }
    result.credentials_ms = elapsed(credentialStart);
  }
  const start = performance.now();
  return new Promise(resolve => {
    let buffer = '';
    let count = 0;
    let error = false;
    const args = body.mode === 'startup' ? ['headless', '--help']
      : ['headless', '--json', '--approval-policy', 'never', '-'];
    const child = spawn(DSH_BIN, args, { cwd: ROOT, env, stdio: ['pipe', 'pipe', 'pipe'], detached: true });
    const kill = () => { if (child.pid) try { process.kill(-child.pid, 'SIGKILL'); } catch {} };
    const timer = setTimeout(() => { result.timed_out = true; result.error_class = 'timeout'; error = true; kill(); }, 120000);
    const line = text => {
      let event;
      try { event = JSON.parse(text); } catch { return; }
      if (!event || typeof event.type !== 'string') return;
      if (result.first_event_ms === null) result.first_event_ms = elapsed(start);
      if (event.type === 'text' && typeof event.text === 'string' && event.text.length && result.first_text_ms === null) {
        result.first_text_ms = elapsed(start);
      }
      if (event.type === 'final') {
        result.stop_reason = event.reason?.kind ?? null;
        result.answer_matches_expected = typeof event.text === 'string' && event.text.trim() === 'pong';
      }
    };
    const bound = chunk => {
      count += Buffer.byteLength(chunk);
      if (count > 1024 * 1024) { error = true; result.error_class = 'output_limit'; kill(); }
    };
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      bound(chunk); if (error) return;
      buffer += chunk;
      let index;
      while ((index = buffer.indexOf('\n')) >= 0) { line(buffer.slice(0, index)); buffer = buffer.slice(index + 1); }
    });
    child.stderr.on('data', bound);
    child.stdin.on('error', () => {});
    child.on('error', () => { error = true; result.error_class = 'spawn'; });
    child.on('close', code => {
      clearTimeout(timer); line(buffer);
      result.exit_code = code; result.total_ms = elapsed(start);
      result.ok = !error && code === 0 && (body.mode === 'startup'
        || (result.stop_reason === 'completed' && result.first_text_ms !== null));
      if (!result.ok && !result.error_class) result.error_class = 'not_completed';
      kill(); resolve(result);
    });
    child.stdin.end(body.mode === 'agent' ? body.prompt : '');
  });
}

function send(res, code, body) {
  res.writeHead(code, { 'content-type': 'application/json' });
  res.end(JSON.stringify({ ...identity, ...body }));
}
function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0; const chunks = [];
    req.on('data', chunk => {
      size += chunk.length;
      if (size > 16384) { reject(new Error('too large')); req.destroy(); return; }
      chunks.push(chunk);
    });
    req.on('end', () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); } catch (error) { reject(error); }
    });
    req.on('error', reject);
  });
}
const server = http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    return send(res, 200, { process_boot_ms: performance.now(), mount:
      readFileSync('/proc/mounts', 'utf8').split('\n').filter(line => ['/workspace', '/tmp'].includes(line.split(' ')[1])) });
  }
  if (req.method === 'GET' && req.url === '/storage') return send(res, 200, storageProbe());
  if (req.method === 'GET' && req.url === '/metadata') {
    try {
      const uri = process.env.ECS_CONTAINER_METADATA_URI_V4;
      if (!uri) return send(res, 200, { task_arn: null });
      const task = await (await fetch(uri + '/task', { signal: AbortSignal.timeout(3000) })).json();
      return send(res, 200, { task_arn: task.TaskARN, availability_zone: task.AvailabilityZone,
        ephemeral_storage_metrics: task.EphemeralStorageMetrics || null });
    } catch { return send(res, 500, { error: 'metadata failed' }); }
  }
  if (req.method !== 'POST') return send(res, 404, { error: 'not found' });
  if (busy) return send(res, 409, { error: 'one operation at a time' });
  busy = true;
  try {
    const body = await readBody(req);
    if (req.url === '/fs') return send(res, 200, fileOperation(body));
    if (req.url === '/cpu') return send(res, 200, cpuProbe(body));
    if (req.url === '/invoke') return send(res, 200, { result: await runDsh(body) });
    send(res, 404, { error: 'not found' });
  } catch { send(res, 400, { error: 'invalid request or operation' }); }
  finally { busy = false; }
}).listen(Number(process.env.PORT || 8080), HOST, () => {
  console.log(JSON.stringify({ event: 'ready', ...identity, host: HOST, port: server.address().port, process_boot_ms: performance.now() }));
});
