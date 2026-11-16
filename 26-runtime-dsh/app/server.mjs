// AgentCore Runtime HTTP wrapper around DeepSeek Harness (DSH) headless mode.
//
// Contract (AgentCore HTTP protocol, port 8080):
//   GET  /ping         -> {"status": "Healthy", "time_of_last_update": <epoch s>}
//   POST /invocations  -> {"mode": "ping"}                      timestamps only
//                         {"mode": "agent", "prompt": "<text>"} one `dsh headless` turn
//   POST /warmup       -> build warmup lifecycle hook (V2), only when DSH_HOOK is set
//
// Every response carries the timestamps the benchmark client needs to split a
// cold start into platform boot, DSH process boot and model time. All *_ts
// fields are epoch seconds on this microVM's clock.
import http from 'node:http';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import {
  mkdirSync, writeFileSync, readFileSync, copyFileSync, chmodSync, statSync, readdirSync,
  symlinkSync, realpathSync, statfsSync, openSync, readSync, closeSync,
} from 'node:fs';
import { performance } from 'node:perf_hooks';
import path from 'node:path';
import os from 'node:os';

const PROC_START_TS = performance.timeOrigin / 1000;
const now = () => Date.now() / 1000;

// Bind loopback unless told otherwise; the container image sets HOST=0.0.0.0
// because AgentCore reaches the container over the microVM's internal network.
const HOST = process.env.HOST || '127.0.0.1';
const PORT = Number(process.env.PORT || 8080);
const APP_DIR = path.dirname(new URL(import.meta.url).pathname);
const DSH_BIN = process.env.DSH_BIN || path.join(APP_DIR, 'node_modules', '.bin', 'dsh');
const DSH_HOME = process.env.DSH_HOME || path.join(os.homedir(), '.dsh');
const WORKDIR = process.env.DSH_WORKDIR || '/workspace';
const MODEL_ID = process.env.DSH_MODEL_ID || 'deepseek.v3.2';
const REGION = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || 'us-east-1';
const IMAGE_TAG = process.env.IMAGE_TAG || 'dev';
const AGENT_TIMEOUT_MS = Number(process.env.AGENT_TIMEOUT_MS || 120000);
// DSH_PREWARM: 0 = off; 1 = boot the headless profile once after listening;
// 3 = page pin: before listening, map the files a first turn reads so they
// stay in memory when the platform snapshots the started container (V2);
// 4 = RAM disk: before listening, copy those files to SHM_DIR (/dev/shm) and
// run DSH from there, so a restored session reads them from memory.
const PREWARM_LEVEL = Number(process.env.DSH_PREWARM || 0);
const PREWARM = PREWARM_LEVEL >= 1;
const PIN_MANIFEST = process.env.PIN_MANIFEST || path.join(APP_DIR, 'pin-manifest.txt');
const PIN_WAIT_MS = Number(process.env.PIN_WAIT_MS || 90000);
const SHM_DIR = process.env.SHM_DIR || '/dev/shm/dsh';
// DSH_HOOK: what the build warmup lifecycle hook (POST /warmup) does in its
// `disk` call. '' = no hook route; 'read' = read the manifest's files cold
// (cache dropped per file first) and let go; 'pin' = the same cold reads by the
// pinner, which then keeps the files mapped. The `memory` call has nothing left
// to do: startup work already runs before listening.
// Measured on AgentCore (2026-11-14): sessions are restored from the state
// after the `memory` call; the `disk` call runs ~90 s later on a restored copy,
// so what it leaves in memory (pin mode's mappings) never reaches a session.
// To pin into the snapshot, combine DSH_PREWARM=3 with DSH_HOOK=read.
const HOOK_MODE = process.env.DSH_HOOK || '';
const WARMUP_PATH = '/warmup';
const WARMUP_TYPE_HEADER = 'x-amzn-bedrock-agentcore-runtime-warmup-type';
if (!['', 'read', 'pin'].includes(HOOK_MODE)) throw new Error(`DSH_HOOK must be "read" or "pin", got ${JSON.stringify(HOOK_MODE)}`);
if (HOOK_MODE === 'pin' && PREWARM_LEVEL === 3) throw new Error('DSH_HOOK=pin and DSH_PREWARM=3 both pin; set one');
if (![0, 1, 3, 4].includes(PREWARM_LEVEL)) throw new Error(`DSH_PREWARM must be 0, 1, 3 or 4, got ${process.env.DSH_PREWARM}`);
const MAX_BODY_BYTES = 1024 * 1024;
const MAX_OUTPUT_CHARS = 4000;

let invocationSeq = 0;
let lastUpdate = Math.floor(now());
let dshVersion = 'unknown';
try {
  const pkg = path.join(APP_DIR, 'node_modules', '@deepseek-ai', 'dsh', 'package.json');
  dshVersion = JSON.parse(readFileSync(pkg, 'utf8')).version;
} catch { /* reported as "unknown" */ }

// Point the headless profile at the Bedrock model. Written at start so the
// model and region are runtime configuration, not baked into the image.
function writeProfilePatch() {
  const dir = path.join(DSH_HOME, 'profiles', 'headless');
  mkdirSync(dir, { recursive: true });
  const yaml = [
    '- id: llm-pi-ai',
    '  config:',
    '    providers:',
    '      amazon-bedrock:',
    `        baseURL: https://bedrock-runtime.${REGION}.amazonaws.com`,
    '        models:',
    `          - id: ${JSON.stringify(MODEL_ID)}`,
    '- id: agent-default-model',
    '  config:',
    '    provider: amazon-bedrock',
    `    model: ${JSON.stringify(MODEL_ID)}`,
    '',
  ].join('\n');
  writeFileSync(path.join(dir, 'cordis.patch.yml'), yaml);
}

// DSH's Bedrock route only signs requests when it sees one of these in the
// environment. If the platform delivers role credentials another way (instance
// metadata), resolve them with the AWS SDK default chain and hand them to the
// child process as environment variables. Values are never logged or returned.
const AMBIENT_KEYS = [
  'AWS_BEARER_TOKEN_BEDROCK', 'AWS_PROFILE', 'AWS_ACCESS_KEY_ID',
  'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI', 'AWS_CONTAINER_CREDENTIALS_FULL_URI',
  'AWS_WEB_IDENTITY_TOKEN_FILE',
];
let cachedCreds = null;
async function childEnv() {
  const env = { ...process.env, DSH_HOME, AWS_REGION: REGION };
  const ambient = AMBIENT_KEYS.find((k) => process.env[k]);
  if (ambient) return { env, source: `env:${ambient}` };
  const fresh = cachedCreds && (!cachedCreds.expiration || cachedCreds.expiration - Date.now() > 5 * 60 * 1000);
  if (!fresh) {
    const require = createRequire(import.meta.url);
    const { defaultProvider } = require('@aws-sdk/credential-provider-node');
    cachedCreds = await defaultProvider()();
  }
  env.AWS_ACCESS_KEY_ID = cachedCreds.accessKeyId;
  env.AWS_SECRET_ACCESS_KEY = cachedCreds.secretAccessKey;
  if (cachedCreds.sessionToken) env.AWS_SESSION_TOKEN = cachedCreds.sessionToken;
  return { env, source: 'sdk-default-chain' };
}

// Optional pre-warm: boot the headless profile once at container start so the
// first real turn finds DSH's modules already in the page cache.
const prewarm = { enabled: PREWARM, level: PREWARM_LEVEL, start_ts: null, end_ts: null, exit_code: null, detail: null, released_ts: null };
function runPrewarm() {
  prewarm.start_ts = now();
  const child = spawn(DSH_BIN, ['headless', '--help'], {
    cwd: WORKDIR, env: { ...process.env, DSH_HOME }, stdio: 'ignore',
  });
  child.on('error', () => { prewarm.end_ts = now(); prewarm.exit_code = -1; });
  child.on('close', (code) => { prewarm.end_ts = now(); prewarm.exit_code = code; });
}

// Page pin (level 3). pinner.py reads and mmaps every file in the manifest,
// prints one JSON line, then blocks holding the mappings. Pages mapped by a
// live process survive the page-cache drop that precedes the V2 snapshot, so a
// restored session finds DSH's files in memory instead of reading them cold.
// Best effort: any failure is logged and the server starts anyway.
let pinner = null;
// Runs pinner.py and resolves with its result line. `hold` keeps the process
// (and its mappings) as `pinner`; otherwise it is a read-only pass that exits.
function runPinner(extraArgs = [], hold = true) {
  return new Promise((resolve) => {
    let settled = false;
    let out = '';
    let child;
    const finish = (detail) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(detail);
    };
    const timer = setTimeout(() => finish({ error: `no result within ${PIN_WAIT_MS} ms` }), PIN_WAIT_MS);
    try {
      child = spawn('python3', [path.join(APP_DIR, 'pinner.py'), ...extraArgs, PIN_MANIFEST], { stdio: ['pipe', 'pipe', 'inherit'] });
    } catch (err) {
      return finish({ error: `spawn failed: ${err.code || err.name}` });
    }
    if (hold) pinner = child;
    const drop = () => { if (pinner === child) pinner = null; };
    child.on('error', (err) => { drop(); finish({ error: `spawn failed: ${err.code || err.name}` }); });
    child.on('close', (code) => {
      drop();
      if (hold) prewarm.exit_code = code;
      finish({ error: `pinner exited with ${code}` });
    });
    child.stdout.on('data', (chunk) => {
      out += chunk.toString('utf8');
      const idx = out.indexOf('\n');
      if (idx < 0) return;
      try { finish(JSON.parse(out.slice(0, idx))); } catch { finish({ error: 'pinner printed something other than its result line' }); }
    });
  });
}
async function startPinner() {
  prewarm.start_ts = now();
  prewarm.detail = await runPinner();
  prewarm.end_ts = now();
}

// Called after the first turn: the files are in the page cache now, so the
// mappings are no longer needed.
function releasePinner() {
  if (!pinner) return;
  pinner.kill('SIGKILL');
  pinner = null;
  prewarm.released_ts = now();
}

// RAM-disk staging (level 4). AWS's fast-session-startup blog: memory,
// including RAM-backed file systems such as /dev/shm, comes back whole with a
// V2 snapshot, while files read from the image are fetched again by every new
// session. Before listening, mirror APP_DIR under SHM_DIR: files in the
// manifest are copied, every other entry of the directories on their paths
// becomes a symlink back to the image, so code paths outside the manifest
// still resolve (from the image, at image speed). The node binary and the
// libraries it loads are copied too when SHM_DIR is not mounted noexec; on a
// noexec mount, ELF files (native addons) stay on the image, because they
// cannot be mapped executable from there.
// Best effort: on any failure DSH runs from the image as usual. The copies
// stay in memory for the whole session (pin releases its pages after turn 1).
const shm = { staged: false, node: null, entry: null, libdir: null };
function mountOf(dir) {
  let best = null;
  for (const line of readFileSync('/proc/self/mounts', 'utf8').split('\n')) {
    const [, mnt, type, opts] = line.split(' ');
    if (!mnt) continue;
    const inside = dir === mnt || dir.startsWith(mnt === '/' ? '/' : `${mnt}/`);
    if (inside && (!best || mnt.length > best.mnt.length)) best = { mnt, type, opts };
  }
  return best;
}
function isElf(file) {
  const fd = openSync(file, 'r');
  try {
    const head = Buffer.alloc(4);
    return readSync(fd, head, 0, 4, 0) === 4 && head.equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46]));
  } finally {
    closeSync(fd);
  }
}
function stageToShm() {
  prewarm.start_ts = now();
  const detail = { copied: 0, bytes: 0, links: 0, dirs: 0, elf_on_image: 0, exec: false, ms: null,
                   mount: null, free_bytes: null, tmpfs: [], error: null };
  try {
    const listed = readFileSync(PIN_MANIFEST, 'utf8').split('\n')
      .map((line) => line.trim()).filter((line) => line && !line.startsWith('#'));
    const hot = new Set(listed.filter((p) => p.startsWith(`${APP_DIR}/`)));
    const dirs = new Set([APP_DIR]);
    for (const p of hot) for (let d = path.dirname(p); d.length > APP_DIR.length; d = path.dirname(d)) dirs.add(d);
    detail.tmpfs = readFileSync('/proc/self/mounts', 'utf8').split('\n')  // RAM-backed mounts, for the record
      .map((line) => line.split(' ')).filter((f) => f[2] === 'tmpfs' || f[2] === 'ramfs')
      .map((f) => `${f[1]} ${f[3]}`);
    mkdirSync(SHM_DIR, { recursive: true });
    const mnt = mountOf(SHM_DIR);
    detail.mount = mnt ? `${mnt.mnt} ${mnt.type} ${mnt.opts}` : null;
    const fsStat = statfsSync(SHM_DIR);
    detail.free_bytes = fsStat.bavail * fsStat.bsize;
    detail.exec = !(mnt && mnt.opts.split(',').includes('noexec'));
    const mirror = (p) => path.join(SHM_DIR, 'app', path.relative(APP_DIR, p));
    const stageable = (p) => detail.exec || !isElf(p) || (detail.elf_on_image += 1, false);
    const size = (p) => { try { return statSync(p).size; } catch { return 0; } };
    const libs = listed.filter((f) => /^\/(usr\/)?lib\//.test(f) && f.includes('.so'));
    const need = [...hot].filter((p) => detail.exec || !isElf(p)).reduce((sum, p) => sum + size(p), 0)
      + (detail.exec ? size(process.execPath) + libs.reduce((sum, p) => sum + size(p), 0) : 0);
    if (need * 1.1 > detail.free_bytes) throw new Error(`needs ${need} bytes, ${detail.free_bytes} free`);
    const copy = (from, to, mode) => {
      copyFileSync(from, to);
      chmodSync(to, mode);
      detail.copied += 1;
      detail.bytes += statSync(to).size;
    };
    const build = (dir) => {
      mkdirSync(mirror(dir), { recursive: true });
      detail.dirs += 1;
      for (const ent of readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, ent.name);
        if (dirs.has(p) && ent.isDirectory()) build(p);
        else if (hot.has(p) && ent.isFile() && stageable(p)) copy(p, mirror(p), statSync(p).mode & 0o777);
        else { symlinkSync(p, mirror(p)); detail.links += 1; }
      }
    };
    build(APP_DIR);
    if (detail.exec) {
      const bin = path.join(SHM_DIR, 'bin');
      const lib = path.join(SHM_DIR, 'lib');
      mkdirSync(bin, { recursive: true });
      mkdirSync(lib, { recursive: true });
      copy(process.execPath, path.join(bin, 'node'), 0o755);
      for (const p of libs) {
        const soname = path.basename(p).match(/^(.*?\.so(?:\.\d+)?)/)[1];  // libstdc++.so.6.0.30 -> libstdc++.so.6
        copy(p, path.join(lib, soname), 0o755);
      }
      shm.node = path.join(bin, 'node');
      shm.libdir = lib;
    }
    shm.entry = mirror(realpathSync(DSH_BIN));
    statSync(shm.entry);  // the entry script must be a real copy
    shm.staged = true;
  } catch (err) {
    detail.error = String(err.message || err).slice(0, 300);
    shm.staged = false;
    shm.node = shm.libdir = shm.entry = null;
  }
  prewarm.end_ts = now();
  detail.ms = Math.round((prewarm.end_ts - prewarm.start_ts) * 1000);
  prewarm.detail = detail;
}

// Build warmup lifecycle hook. AgentCore calls POST /warmup twice while it
// prepares a V2 runtime (create/update), `memory` first, then `disk`. Sessions
// start from the state after `memory`; `disk` runs later on a restored copy and
// only tells the platform which files to make available faster (measured
// 2026-11-14). A 2xx means the work is done; anything else fails the
// create/update, which is what a broken variant of this benchmark should do.
// The work runs in a child process, so /ping keeps answering meanwhile.
// runPinner() is the page-pin helper above.
const hook = { mode: HOOK_MODE || null, memory_ts: null, disk_calls: 0, disk_start_ts: null, disk_end_ts: null, detail: null };
async function handleWarmup(type, res) {
  if (type === 'memory') {
    hook.memory_ts = now();
    console.log(JSON.stringify({ event: 'warmup', type, mode: HOOK_MODE }));
    return send(res, 200, { type });
  }
  if (type !== 'disk') {
    console.log(JSON.stringify({ event: 'warmup', type, mode: HOOK_MODE, ignored: true }));
    return send(res, 200, { type, ignored: true });
  }
  hook.disk_calls += 1;
  if (HOOK_MODE === 'pin' && pinner) return send(res, 200, { type, reused: true, detail: hook.detail });
  hook.disk_start_ts = now();
  const hold = HOOK_MODE === 'pin';
  const detail = await runPinner(hold ? ['--evict'] : ['--evict', '--no-hold'], hold);
  hook.disk_end_ts = now();
  hook.detail = detail;
  if (hold) {  // so the report's page-pin sections see this runtime as pinned
    prewarm.detail = detail;
    prewarm.start_ts = hook.disk_start_ts;
    prewarm.end_ts = hook.disk_end_ts;
  }
  const ok = !detail.error && (detail.read ?? 0) > 0;
  console.log(JSON.stringify({ event: 'warmup', type, mode: HOOK_MODE, ok, ms: Math.round((hook.disk_end_ts - hook.disk_start_ts) * 1000), detail }));
  return send(res, ok ? 200 : 500, { type, detail });
}

// Load the SDK credential chain's modules at startup so they are part of the
// process (and of a V2 snapshot). The credentials themselves are discarded:
// values resolved before a snapshot would be shared and could be expired.
async function warmCredentialChain() {
  try {
    const require = createRequire(import.meta.url);
    const { defaultProvider } = require('@aws-sdk/credential-provider-node');
    await Promise.race([defaultProvider()(), new Promise((_, reject) => setTimeout(reject, 5000))]);
  } catch { /* no credentials at startup is fine; the handler resolves them */ }
}

async function runAgent(prompt) {
  const result = {
    ok: false, exit_code: null, spawn_ts: null, first_output_ts: null, first_text_ts: null,
    exit_ts: null, model: MODEL_ID, output: '', error: null, usage: null,
    stop_reason: null, cred_source: null, ran_from: 'image',
  };
  let env;
  try {
    const resolved = await childEnv();
    env = resolved.env;
    result.cred_source = resolved.source;
  } catch (err) {
    result.spawn_ts = result.exit_ts = now();
    result.error = `credential resolution failed: ${err.name}`;
    return result;
  }
  return new Promise((resolve) => {
    result.spawn_ts = now();
    // The task is passed on stdin (`-`), never through a shell or argv.
    const dshArgs = ['headless', '--json', '--approval-policy', 'never', '-'];
    let cmd = DSH_BIN;
    let args = dshArgs;
    if (shm.staged) {  // run the staged copy: node and DSH from the RAM disk
      cmd = shm.node || process.execPath;
      args = [shm.entry, ...dshArgs];
      if (shm.node) env.PATH = `${path.dirname(shm.node)}:${env.PATH || ''}`;
      if (shm.libdir) env.LD_LIBRARY_PATH = shm.libdir;
      result.ran_from = shm.node ? 'shm' : 'shm-js';
    }
    const child = spawn(cmd, args, {
      cwd: WORKDIR, env, stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stderrTail = '';
    let buffer = '';
    let finalText = null;
    const timer = setTimeout(() => {
      result.error = `timeout after ${AGENT_TIMEOUT_MS} ms`;
      child.kill('SIGKILL');
    }, AGENT_TIMEOUT_MS);
    const onLine = (line) => {
      if (!line.trim()) return;
      let event;
      try { event = JSON.parse(line); } catch { return; }
      if (event.type === 'text' && result.first_text_ts === null) result.first_text_ts = now();
      if (event.type === 'status' && event.phase === 'step_end' && event.usage) result.usage = event.usage;
      if (event.type === 'final') {
        finalText = event.text ?? '';
        result.stop_reason = event.reason?.kind ?? null;
        if (event.reason?.kind !== 'completed') {
          result.error = String(event.reason?.error?.message ?? event.reason?.kind ?? 'not completed').slice(0, 500);
        }
      }
    };
    child.stdout.on('data', (chunk) => {
      if (result.first_output_ts === null) result.first_output_ts = now();
      buffer += chunk.toString('utf8');
      let idx;
      while ((idx = buffer.indexOf('\n')) >= 0) {
        onLine(buffer.slice(0, idx));
        buffer = buffer.slice(idx + 1);
      }
    });
    child.stderr.on('data', (chunk) => {
      stderrTail = (stderrTail + chunk.toString('utf8')).slice(-800);
    });
    child.on('error', (err) => {
      clearTimeout(timer);
      result.exit_ts = now();
      result.exit_code = -1;
      result.error = `spawn failed: ${err.code || err.name}`;
      resolve(result);
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      onLine(buffer);
      result.exit_ts = now();
      result.exit_code = code;
      result.output = (finalText ?? '').slice(0, MAX_OUTPUT_CHARS);
      result.ok = code === 0 && result.stop_reason === 'completed';
      if (!result.ok && !result.error) result.error = stderrTail.trim() || `exit code ${code}`;
      resolve(result);
    });
    child.stdin.end(prompt);
  });
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) { reject(new Error('body too large')); req.destroy(); return; }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function send(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) });
  res.end(data);
}

const server = http.createServer(async (req, res) => {
  const url = (req.url || '').split('?')[0];
  if (req.method === 'GET' && url === '/ping') {
    return send(res, 200, { status: 'Healthy', time_of_last_update: lastUpdate });
  }
  if (HOOK_MODE && req.method === 'POST' && url === WARMUP_PATH) {
    return handleWarmup(String(req.headers[WARMUP_TYPE_HEADER] || '').toLowerCase(), res);
  }
  if (req.method !== 'POST' || url !== '/invocations') return send(res, 404, { error: 'not found' });

  const requestTs = now();
  invocationSeq += 1;
  const seq = invocationSeq;
  let payload;
  try {
    const raw = await readBody(req);
    payload = raw ? JSON.parse(raw) : {};
  } catch {
    return send(res, 400, { error: 'invalid JSON body' });
  }
  const mode = payload.mode ?? 'ping';
  const base = {
    mode, proc_start_ts: PROC_START_TS, request_ts: requestTs, invocation_seq: seq,
    image_tag: IMAGE_TAG, dsh_version: dshVersion, node_version: process.version, prewarm, hook,
  };
  if (mode === 'ping') return send(res, 200, { ...base, response_ts: now() });
  if (mode !== 'agent') return send(res, 400, { error: 'mode must be "ping" or "agent"' });
  if (typeof payload.prompt !== 'string' || !payload.prompt.trim()) {
    return send(res, 400, { error: 'agent mode needs a non-empty "prompt" string' });
  }
  let agent;
  try {
    agent = await runAgent(payload.prompt);
  } finally {
    releasePinner();
  }
  lastUpdate = Math.floor(now());
  console.log(JSON.stringify({
    event: 'agent_turn', seq, ok: agent.ok, exit_code: agent.exit_code, cred_source: agent.cred_source,
    dsh_boot_ms: agent.first_output_ts ? Math.round((agent.first_output_ts - agent.spawn_ts) * 1000) : null,
    dsh_total_ms: Math.round((agent.exit_ts - agent.spawn_ts) * 1000), error: agent.error,
  }));
  return send(res, 200, { ...base, agent, response_ts: now() });
});

mkdirSync(WORKDIR, { recursive: true });
writeProfilePatch();
await warmCredentialChain();
if (PREWARM_LEVEL === 3) await startPinner();
if (PREWARM_LEVEL === 4) stageToShm();
server.listen(PORT, HOST, () => {
  console.log(JSON.stringify({
    event: 'listening', host: HOST, port: PORT, dsh_version: dshVersion, model: MODEL_ID,
    region: REGION, prewarm: PREWARM_LEVEL, prewarm_detail: prewarm.detail, hook: HOOK_MODE || null,
    boot_ms: Math.round((now() - PROC_START_TS) * 1000),
  }));
  if (PREWARM_LEVEL === 1) runPrewarm();
});
