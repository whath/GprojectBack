// Works with browser/native runtimes that provide fetch and AbortController.
// A browser page must use the API's origin or an explicitly allowed CORS origin.
export class MarketClient {
  constructor(token, base = 'https://ericcself.top', timeoutMs = 20000) {
    const url = new URL(base);
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && url.hostname === '127.0.0.1')) {
      throw new Error('Use HTTPS or a local SSH tunnel');
    }
    this.token = token;
    this.base = base.replace(/\/$/, '');
    this.timeoutMs = timeoutMs;
  }

  async get(path, params = {}) {
    const url = new URL(this.base + path);
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(url, {
        method: 'GET', headers: { Authorization: `Bearer ${this.token}` },
        signal: controller.signal, cache: 'no-store',
      });
      const data = response.headers.get('content-type')?.includes('application/json')
        ? await response.json()
        : { detail: `HTTP ${response.status}` };
      if (!response.ok) {
        const error = new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`);
        error.status = response.status;
        error.retryAfter = response.headers.get('Retry-After');
        throw error;
      }
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  // Instruments support snapshot_id; close quotes do not share this guarantee.
  async *instruments(market = 'CN', kind = 'stock', limit = 500) {
    let offset = 0, snapshotId;
    while (true) {
      const page = await this.get('/v1/instruments', { market, kind, limit, offset, snapshot_id: snapshotId });
      snapshotId = page.snapshot_id;
      for (const item of page.items) yield item;
      offset += page.items.length;
      if (offset >= page.total) return;
      if (!page.items.length) throw new Error('Incomplete pagination');
    }
  }
}
