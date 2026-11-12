// AgentCore Runtime HTTP wrapper around DeepSeek Harness (DSH) headless mode.
//
// Contract (AgentCore HTTP protocol, port 8080):
//   GET  /ping         -> {"status": "Healthy", "time_of_last_update": <epoch s>}
//   POST /invocations  -> {"mode": "ping"}                      timestamps only
//                         {"mode": "agent", "prompt": "<text>"} one `dsh headless` turn
//
// Every response carries the timestamps the benchmark client needs to split a
// cold start into platform boot, DSH process boot and model time. All *_ts
// fields are epoch seconds on this microVM's clock.
import http from 'node:http';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync, readFileSync } from 'node:fs';
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
// stay in memory when the platform snapshots the started container (V2).
const PREWARM_LEVEL = Number(process.env.DSH_PREWARM || 0);
const PREWARM = PREWARM_LEVEL >= 1;
const PIN_MANIFEST = process.env.PIN_MANIFEST || path.join(APP_DIR, 'pin-manifest.txt');
const PIN_WAIT_MS = Number(process.env.PIN_WAIT_MS || 90000);
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
function startPinner() {
  prewarm.start_ts = now();
  return new Promise((resolve) => {
    let settled = false;
    let out = '';
    const finish = (detail) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      prewarm.detail = detail;
      prewarm.end_ts = now();
      resolve();
    };
    const timer = setTimeout(() => finish({ error: `no result within ${PIN_WAIT_MS} ms` }), PIN_WAIT_MS);
    try {
      pinner = spawn('python3', [path.join(APP_DIR, 'pinner.py'), PIN_MANIFEST], { stdio: ['pipe', 'pipe', 'inherit'] });
    } catch (err) {
      return finish({ error: `spawn failed: ${err.code || err.name}` });
    }
    pinner.on('error', (err) => { pinner = null; finish({ error: `spawn failed: ${err.code || err.name}` }); });
    pinner.on('close', (code) => { pinner = null; prewarm.exit_code = code; finish({ error: `pinner exited with ${code}` }); });
    pinner.stdout.on('data', (chunk) => {
      out += chunk.toString('utf8');
      const idx = out.indexOf('\n');
      if (idx < 0) return;
      try { finish(JSON.parse(out.slice(0, idx))); } catch { finish({ error: 'pinner printed something other than its result line' }); }
    });
  });
}
// Called after the first turn: the files are in the page cache now, so the
// mappings are no longer needed.
function releasePinner() {
  if (!pinner) return;
  pinner.kill('SIGKILL');
  pinner = null;
  prewarm.released_ts = now();
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
    stop_reason: null, cred_source: null,
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
    const child = spawn(DSH_BIN, ['headless', '--json', '--approval-policy', 'never', '-'], {
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
    image_tag: IMAGE_TAG, dsh_version: dshVersion, node_version: process.version, prewarm,
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
server.listen(PORT, HOST, () => {
  console.log(JSON.stringify({
    event: 'listening', host: HOST, port: PORT, dsh_version: dshVersion, model: MODEL_ID,
    region: REGION, prewarm: PREWARM_LEVEL, prewarm_detail: prewarm.detail,
    boot_ms: Math.round((now() - PROC_START_TS) * 1000),
  }));
  if (PREWARM_LEVEL === 1) runPrewarm();
});
