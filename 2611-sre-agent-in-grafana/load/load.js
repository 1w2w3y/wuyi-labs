// Steady, modest traffic for the Orders API demo: each worker looks up a new
// order ID, waits for the answer, then pauses so it sends at most one request
// per interval. Usage: node load.js https://<app>.azurewebsites.net [workers] [intervalMs]

const base = (process.argv[2] || '').replace(/\/$/, '');
const workers = Number(process.argv[3] || 2);
const intervalMs = Number(process.argv[4] || 1000);
if (!base) {
  console.error('usage: node load.js <base-url> [workers] [intervalMs]');
  process.exit(1);
}

const counts = { ok: 0, failed: 0 };
let latencySum = 0;

async function worker(n) {
  for (;;) {
    const id = `ord-${Date.now().toString(36)}-${n}-${Math.floor(Math.random() * 1e6)}`;
    const begin = Date.now();
    try {
      const res = await fetch(`${base}/api/orders/${id}`, { signal: AbortSignal.timeout(20000) });
      await res.arrayBuffer();
      if (res.ok) counts.ok++; else counts.failed++;
    } catch {
      counts.failed++;
    }
    const took = Date.now() - begin;
    latencySum += took;
    await new Promise((r) => setTimeout(r, Math.max(0, intervalMs - took)));
  }
}

setInterval(() => {
  const total = counts.ok + counts.failed;
  console.log(`${new Date().toISOString()} ok=${counts.ok} failed=${counts.failed} avg=${total ? Math.round(latencySum / total) : 0}ms`);
  counts.ok = counts.failed = 0;
  latencySum = 0;
}, 60000);

for (let n = 0; n < workers; n++) worker(n);
