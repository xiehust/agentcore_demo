#!/usr/bin/env node
// Offline fixture only: never invokes a provider or represents real DSH timing.
if (process.argv.includes('--help')) {
  console.log('fake headless help');
} else {
  let prompt = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => { prompt += chunk; });
  process.stdin.on('end', () => {
    if (prompt === 'timeout') return setTimeout(() => {}, 5000);
    if (prompt === 'flood') { process.stdout.write('x'.repeat(2 * 1024 * 1024)); return; }
    if (prompt === 'failed') {
      console.error('secret-from-provider');
      console.log(JSON.stringify({ type: 'final', reason: { kind: 'failed' } }));
      return;
    }
    console.log('not a JSON event');
    console.log(JSON.stringify({ type: 'status', phase: 'step_start' }));
    if (prompt !== 'missing-text') {
      // Fragment a Unicode JSON event across writes.
      const text = JSON.stringify({ type: 'text', text: 'pong 中文' });
      process.stdout.write(text.slice(0, 7));
      process.stdout.write(text.slice(7) + '\n');
    }
    // No final newline: the runner must process the buffered tail on close.
    process.stdout.write(JSON.stringify({ type: 'final', text: 'pong', reason: { kind: 'completed' } }));
  });
}
