// Minimal trusted benchmark guest; not a production agent endpoint.
import http from 'node:http';
import { spawn } from 'node:child_process';
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { performance } from 'node:perf_hooks';
import path from 'node:path';

const DSH_BIN = process.env.DSH_BIN || '/opt/dsh/node_modules/.bin/dsh';
const DSH_HOME = process.env.DSH_HOME || '/home/dsh/.dsh';
const WORKDIR = process.env.DSH_WORKDIR || '/workspace';
const RUN_ID = process.env.BENCH_RUN_ID || 'local';
const BOOT_ID = readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim();
const LIMIT = 32768;
let busy = false;

const now = () => performance.now();
const elapsed = (start) => now() - start;
const dshVersion = () => {
  try {
    return JSON.parse(readFileSync(path.join(path.dirname(DSH_BIN), '..', '@deepseek-ai',
      'dsh', 'package.json'), 'utf8')).version;
  } catch { return 'unknown'; }
};

function profile(region, model) {
  if (!/^[a-z0-9-]+$/.test(region) || typeof model !== 'string' || !model.length || model.length > 256) {
    throw new Error('invalid region/model');
  }
  const dir = path.join(DSH_HOME, 'profiles', 'headless');
  mkdirSync(dir, { recursive: true });
  writeFileSync(path.join(dir, 'cordis.patch.yml'), [
    '- id: llm-pi-ai', '  config:', '    providers:', '      amazon-bedrock:',
    `        baseURL: https://bedrock-runtime.${region}.amazonaws.com`,
    '        models:', `          - id: ${JSON.stringify(model)}`, '            maxTokens: 512',
    '- id: agent-default-model', '  config:', '    provider: amazon-bedrock',
    `    model: ${JSON.stringify(model)}`, '',
  ].join('\n'));
}

function childEnvironment(payload) {
  // Do not inherit host credentials or allow requests to set arbitrary env vars.
  const env = { PATH: process.env.PATH, HOME: '/home/dsh', DSH_HOME,
    AWS_EC2_METADATA_DISABLED: 'true', AWS_MAX_ATTEMPTS: '1' };
  if (payload.mode === 'agent') {
    profile(payload.region, payload.model);
    const creds = payload.credentials;
    if (!creds || !creds.AWS_ACCESS_KEY_ID || !creds.AWS_SECRET_ACCESS_KEY) {
      throw new Error('agent mode needs explicit temporary credentials');
    }
    for (const key of ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN']) {
      if (creds[key] !== undefined) {
        if (typeof creds[key] !== 'string' || creds[key].length > 8192) throw new Error('invalid credentials');
        env[key] = creds[key];
      }
    }
    env.AWS_REGION = payload.region;
  }
  return env;
}

function runDsh(payload) {
  const env = childEnvironment(payload);
  const args = payload.mode === 'startup' ? ['headless', '--help']
    : ['headless', '--json', '--approval-policy', 'never', '-'];
  const start = now();
  const result = { ok: false, exit_code: null, timed_out: false,
    dsh_first_event_ms: null, dsh_first_text_ms: null, dsh_total_ms: null,
    stop_reason: null, error: null, answer_matches_expected: null };
  return new Promise((resolve) => {
    let finalSeen = false;
    let buffer = '';
    let outputBytes = 0;
    const child = spawn(DSH_BIN, args, { cwd: WORKDIR, env,
      stdio: ['pipe', 'pipe', 'pipe'], detached: true });
    const kill = () => {
      if (child.pid) {
        try { process.kill(-child.pid, 'SIGKILL'); } catch { /* already exited */ }
      }
    };
    const timer = setTimeout(() => {
      result.timed_out = true;
      result.error = 'agent timeout';
      kill();
    }, payload.timeout_ms);
    const onLine = (line) => {
      let event;
      try { event = JSON.parse(line); } catch { return; }
      if (!event || typeof event.type !== 'string') return;
      if (result.dsh_first_event_ms === null) result.dsh_first_event_ms = elapsed(start);
      if (event.type === 'text' && typeof event.text === 'string' && event.text.length
          && result.dsh_first_text_ms === null) result.dsh_first_text_ms = elapsed(start);
      if (event.type === 'final') {
        finalSeen = true;
        result.stop_reason = event.reason?.kind ?? null;
        if (payload.expected_text) {
          result.answer_matches_expected = typeof event.text === 'string'
            && event.text.trim() === payload.expected_text;
        }
      }
    };
    const countOutput = (chunk) => {
      outputBytes += chunk.length;
      if (outputBytes > 1024 * 1024) { result.error = 'output limit exceeded'; kill(); }
    };
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      countOutput(chunk);
      if (result.error) return;
      buffer += chunk;
      let idx;
      while ((idx = buffer.indexOf('\n')) >= 0) {
        onLine(buffer.slice(0, idx));
        buffer = buffer.slice(idx + 1);
      }
    });
    // Never return raw stdout/stderr, model text or provider errors: they may
    // contain prompts/credentials. Still drain and bound both pipes.
    child.stderr.on('data', countOutput);
    child.stdin.on('error', () => { /* early exit is reported by close */ });
    child.on('error', (err) => { result.error = `spawn failed: ${err.code || 'unknown'}`; });
    child.on('close', (code) => {
      clearTimeout(timer);
      onLine(buffer);
      result.exit_code = code;
      result.dsh_total_ms = elapsed(start);
      result.ok = !result.error && code === 0
        && (payload.mode === 'startup' || (finalSeen && result.stop_reason === 'completed'
          && result.dsh_first_text_ms !== null && result.answer_matches_expected !== false));
      if (!result.ok && !result.error) result.error = 'DSH failed or no completed text turn';
      // A headless tool could leave descendants alive after the parent exits.
      kill();
      resolve(result);
    });
    child.stdin.end(payload.mode === 'agent' ? payload.prompt : '');
  });
}

function send(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > LIMIT) { reject(new Error('body too large')); req.destroy(); return; }
      chunks.push(chunk);
    });
    req.on('end', () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch { reject(new Error('invalid JSON')); }
    });
    req.on('error', reject);
  });
}

mkdirSync(WORKDIR, { recursive: true });
const health = { run_id: RUN_ID, boot_id: BOOT_ID, dsh_version: dshVersion(),
  node_version: process.version, arch: process.arch };
http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/health') return send(res, 200, health);
  if (req.method !== 'POST' || req.url !== '/invoke') return send(res, 404, { error: 'not found' });
  if (busy) return send(res, 409, { error: 'one invocation at a time' });
  busy = true;
  try {
    const payload = await readBody(req);
    if (!['startup', 'agent'].includes(payload?.mode)
        || !Number.isFinite(payload.timeout_ms) || payload.timeout_ms < 1 || payload.timeout_ms > 300000
        || (payload.expected_text !== undefined && (typeof payload.expected_text !== 'string' || payload.expected_text.length > 256))
        || (payload.mode === 'agent' && (typeof payload.prompt !== 'string'
          || !payload.prompt.trim() || payload.prompt.length > 4000))) {
      return send(res, 400, { error: 'invalid mode/timeout/prompt' });
    }
    const agent = await runDsh(payload);
    send(res, 200, { ...health, ...agent });
  } catch {
    send(res, 400, { error: 'invalid request/configuration' });
  } finally { busy = false; }
}).listen(Number(process.env.PORT || 8080), process.env.HOST || '192.0.2.2');
