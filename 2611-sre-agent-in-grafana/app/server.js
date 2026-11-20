// Orders API: a demo service with a deliberate bug.
//
// The bug: every order that is looked up is added to an in-memory cache that
// is never evicted, and each lookup scans the whole cache. Memory and response
// time grow with every new order ID until lookups pass their deadline and fail
// with HTTP 503. Restarting the process empties the cache, so a restart is a
// mitigation, not a fix.

const http = require('http');

const PORT = process.env.PORT || 8080;
const ENTRY_KB = Number(process.env.ORDER_ENTRY_KB || 384);        // size of one cached order
const DEADLINE_MS = Number(process.env.LOOKUP_DEADLINE_MS || 1500); // lookup budget
const SCAN_STEP = Number(process.env.CACHE_SCAN_STEP || 2);        // smaller = slower scan
const startedAt = Date.now();

/** The cache: grows forever. */
const orderCache = [];

function findInCache(orderId, deadline) {
  for (let i = 0; i < orderCache.length; i++) {
    const entry = orderCache[i];
    // A linear scan that reads each cached payload to compare it.
    let checksum = 0;
    const body = entry.payload;
    for (let j = 0; j < body.length; j += SCAN_STEP) checksum = (checksum + body[j]) | 0;
    entry.lastChecksum = checksum;
    if (entry.id === orderId) return entry;
    if ((i & 15) === 0 && Date.now() > deadline) {
      const err = new Error(
        `OrderCacheLookupTimeout: scanned ${i} of ${orderCache.length} cached orders in ${DEADLINE_MS} ms`);
      err.name = 'OrderCacheLookupTimeout';
      throw err;
    }
  }
  return null;
}

function loadOrder(orderId) {
  // Stands in for a database read. The payload is cached and never evicted.
  const payload = Buffer.alloc(ENTRY_KB * 1024, orderId.length % 251);
  const entry = { id: orderId, payload, loadedAt: Date.now() };
  orderCache.push(entry);
  return entry;
}

function stats() {
  const mem = process.memoryUsage();
  return {
    uptimeSeconds: Math.round((Date.now() - startedAt) / 1000),
    cachedOrders: orderCache.length,
    cacheMb: Math.round((orderCache.length * ENTRY_KB) / 1024),
    rssMb: Math.round(mem.rss / 1048576),
  };
}

// Prometheus metrics, written by hand so the app has no dependencies.
const BUCKETS = [0.025, 0.05, 0.1, 0.25, 0.5, 1, 1.5, 2.5];
const lookupBuckets = BUCKETS.map(() => 0);
let lookupCount = 0;
let lookupSum = 0;
const requestsByCode = {};

function observe(code, seconds) {
  requestsByCode[code] = (requestsByCode[code] || 0) + 1;
  lookupCount++;
  lookupSum += seconds;
  BUCKETS.forEach((le, i) => { if (seconds <= le) lookupBuckets[i]++; });
}

function metricsText() {
  const s = stats();
  const lines = [
    '# HELP orders_cache_entries Orders held in the in-memory cache.',
    '# TYPE orders_cache_entries gauge',
    `orders_cache_entries ${orderCache.length}`,
    '# HELP orders_cache_bytes Bytes of order payloads held in the in-memory cache.',
    '# TYPE orders_cache_bytes gauge',
    `orders_cache_bytes ${orderCache.length * ENTRY_KB * 1024}`,
    '# HELP orders_process_resident_memory_bytes Resident memory of the Node.js process.',
    '# TYPE orders_process_resident_memory_bytes gauge',
    `orders_process_resident_memory_bytes ${process.memoryUsage().rss}`,
    '# HELP orders_process_uptime_seconds Seconds since the process started.',
    '# TYPE orders_process_uptime_seconds gauge',
    `orders_process_uptime_seconds ${s.uptimeSeconds}`,
    '# HELP orders_requests_total Order lookups by HTTP status code.',
    '# TYPE orders_requests_total counter',
    ...Object.entries(requestsByCode).map(([code, n]) => `orders_requests_total{code="${code}"} ${n}`),
    '# HELP orders_lookup_duration_seconds Time spent on one order lookup.',
    '# TYPE orders_lookup_duration_seconds histogram',
    ...BUCKETS.map((le, i) => `orders_lookup_duration_seconds_bucket{le="${le}"} ${lookupBuckets[i]}`),
    `orders_lookup_duration_seconds_bucket{le="+Inf"} ${lookupCount}`,
    `orders_lookup_duration_seconds_sum ${lookupSum.toFixed(3)}`,
    `orders_lookup_duration_seconds_count ${lookupCount}`,
  ];
  return lines.join('\n') + '\n';
}

let sinceLastLog = 0;

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const send = (code, obj) => {
    res.writeHead(code, { 'content-type': 'application/json' });
    res.end(JSON.stringify(obj));
  };

  if (url.pathname === '/' || url.pathname === '/healthz') return send(200, { status: 'ok' });
  if (url.pathname === '/stats') return send(200, stats());
  if (url.pathname === '/metrics') {
    res.writeHead(200, { 'content-type': 'text/plain; version=0.0.4' });
    return res.end(metricsText());
  }

  const match = url.pathname.match(/^\/api\/orders\/([\w-]+)$/);
  if (!match) return send(404, { error: 'not found' });

  const orderId = match[1];
  const begin = Date.now();
  try {
    const cached = findInCache(orderId, begin + DEADLINE_MS);
    const order = cached || loadOrder(orderId);
    if (++sinceLastLog >= 60) {
      sinceLastLog = 0;
      const s = stats();
      console.log(`order cache: ${s.cachedOrders} orders, ${s.cacheMb} MB cached, rss ${s.rssMb} MB, ` +
        `last lookup ${Date.now() - begin} ms, uptime ${s.uptimeSeconds} s`);
    }
    observe(200, (Date.now() - begin) / 1000);
    return send(200, { id: order.id, cached: Boolean(cached), lookupMs: Date.now() - begin });
  } catch (err) {
    observe(503, (Date.now() - begin) / 1000);
    console.error(`${err.message} (order ${orderId}, rss ${stats().rssMb} MB)`);
    return send(503, { error: err.name, message: err.message });
  }
});

server.listen(PORT, () => console.log(`orders-api listening on ${PORT}, entry ${ENTRY_KB} KB, deadline ${DEADLINE_MS} ms`));
