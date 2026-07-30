// Cookie-aware HTTP transport for talking to a JetKVM device.
// ---------------------------------------------------------------------------
// Why this exists: the Fetch API has two hardcoded, security-motivated rules
// that make plain fetch() unusable for JetKVM's cookie-based cross-origin
// auth, no matter how CORS is configured:
//   1. JavaScript can never read a response's Set-Cookie header (it's
//      stripped before fetch() even sees it) — CORS-independent.
//   2. JavaScript can never set a request's Cookie header manually — also
//      CORS-independent ("forbidden header name" in the Fetch spec).
// So even with CapacitorHttp/webSecurity:false removing CORS, /auth/login-local
// succeeds but we can't read the authToken, and /webrtc/session can't send it.
//
// The fix: don't use fetch() for these two calls. Route them through a
// transport that isn't bound by the Fetch spec, so we can read Set-Cookie
// and set Cookie ourselves — a tiny manual cookie jar, one string per client:
//   - Android/iOS: the CapacitorHttp *plugin API* (not the patched fetch) —
//     it's a native bridge, not a Response object, so it isn't bound by the
//     Fetch spec's header rules.
//   - Electron: proxied over IPC to the main process, which uses Node's
//     https module — not a browser at all, no such restrictions exist.
//   - Plain browser (npm run dev): falls back to fetch with credentials:
//     'include'. Cross-origin cookies won't actually persist there, but it's
//     only used for local UI development, never for a packaged app.
// ---------------------------------------------------------------------------

export interface TransportResponse {
  status: number;
  ok: boolean;
  text: string;
}

interface CapacitorHttpModule {
  request(options: {
    url: string;
    method: string;
    headers: Record<string, string>;
    data?: string;
  }): Promise<{ status: number; data: unknown; headers: Record<string, string> }>;
}

interface JetKvmIpcBridge {
  request(options: {
    url: string;
    method: string;
    headers: Record<string, string>;
    body?: string;
  }): Promise<{ status: number; body: string }>;
  /** Opens a URL in the system's default browser (Electron only). */
  openExternal(url: string): Promise<void>;
  /** Points the local settings-iframe reverse proxy at a device (Electron only). */
  setProxyTarget(base: string): Promise<void>;
  /** Launches Windows' on-screen touch keyboard (Electron/Windows only, no-op elsewhere). */
  showTouchKeyboard(): Promise<void>;
}

declare global {
  interface Window {
    jetkvmIpc?: JetKvmIpcBridge;
  }
}

// Loaded lazily so this module works in the plain-browser dev fallback too,
// where @capacitor/core's native bridge isn't present/relevant.
let capacitorModulePromise: Promise<{
  Capacitor: { isNativePlatform(): boolean };
  CapacitorHttp: CapacitorHttpModule;
} | null> | null = null;

function loadCapacitor() {
  if (!capacitorModulePromise) {
    capacitorModulePromise = import('@capacitor/core').catch(() => null);
  }
  return capacitorModulePromise;
}

// The CapacitorHttp native bridge occasionally never delivers its result
// message back to JS at all, even when the underlying request actually
// succeeded on the native side (confirmed via remote-debugging: the native
// log shows the response, status 200 and all, but the JS Promise just never
// settles). One retry after a bounded wait -- rather than an unbounded
// await -- is enough to turn a permanent hang into a brief delay in
// practice, without guessing at *why* the bridge drops the odd message.
async function withTimeoutRetry<T>(attempt: () => Promise<T>, timeoutMs: number): Promise<T> {
  const once = () =>
    new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('native request timed out')), timeoutMs);
      attempt().then(
        (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        (e) => {
          clearTimeout(timer);
          reject(e);
        },
      );
    });
  try {
    return await once();
  } catch {
    return once();
  }
}

/** One authenticated session against one JetKVM device. */
export class JetKvmTransport {
  private cookie: string | null = null;

  constructor(private base: string) {}

  /** Store the auth cookie value found in a Set-Cookie header. */
  private captureCookie(setCookieHeader: string | undefined) {
    if (!setCookieHeader) return;
    // Only the "authToken=<value>" pair matters; drop attributes like Path=/.
    const match = /authToken=([^;]+)/.exec(setCookieHeader);
    if (match) this.cookie = `authToken=${match[1]}`;
  }

  private headers(extra?: Record<string, string>): Record<string, string> {
    const h: Record<string, string> = { 'Content-Type': 'application/json', ...extra };
    if (this.cookie) h['Cookie'] = this.cookie;
    return h;
  }

  async post(path: string, body: unknown): Promise<TransportResponse> {
    const url = `${this.base}${path}`;
    const payload = JSON.stringify(body);

    const capacitor = await loadCapacitor();
    if (capacitor?.Capacitor.isNativePlatform()) {
      // The native bridge call itself has no timeout, and confirmed (via
      // remote-debugging a real device) to sometimes just never deliver its
      // "result" message back to JS at all -- the plugin's native side logs
      // the request going out and even a 200 response coming back, but the
      // JS-side Promise never settles, so callers hang forever with no
      // error. This was the actual cause of "stuck at connecting/후보 h_/s_"
      // for good on both mobile and desktop -- not ICE/TURN at all, since
      // execution never got far enough to reach setRemoteDescription in
      // those cases. One retry after a bounded wait turns a permanent hang
      // into, at worst, one extra request's worth of delay.
      const res = await withTimeoutRetry(
        () =>
          capacitor.CapacitorHttp.request({
            url,
            method: 'POST',
            headers: this.headers(),
            data: payload,
          }),
        4000,
      );
      // Capacitor normalizes header casing inconsistently across platforms;
      // check a few likely keys.
      const setCookie =
        res.headers?.['set-cookie'] ?? res.headers?.['Set-Cookie'];
      this.captureCookie(setCookie);
      // Android's local settings proxy (JetKvmProxyServer.java) blindly
      // forwards whatever Cookie header the WebView itself attaches to
      // http://127.0.0.1:47623/settings on to the real device -- so handing
      // this SAME token to the WebView's own cookie jar for its own origin
      // (just a plain JS document.cookie set, since the whole app already
      // *is* that origin) is enough to make the settings iframe look
      // logged in, no second /auth/login-local call needed. Harmless no-op
      // anywhere else (Electron ignores it; this branch never runs there).
      if (this.cookie) {
        document.cookie = `${this.cookie}; path=/`;
      }
      const text =
        typeof res.data === 'string' ? res.data : JSON.stringify(res.data ?? {});
      return { status: res.status, ok: res.status >= 200 && res.status < 300, text };
    }

    if (window.jetkvmIpc) {
      const res = await window.jetkvmIpc.request({
        url,
        method: 'POST',
        headers: this.headers(),
        body: payload,
      });
      // The main process folds Set-Cookie handling into its own jar keyed by
      // host, but also echoes back whether it captured one via this header
      // so devtools/debugging stays legible; the main process is the source
      // of truth for what it sends on the next call regardless.
      return { status: res.status, ok: res.status >= 200 && res.status < 300, text: res.body };
    }

    // Plain-browser dev fallback (no native bridge available).
    const res = await fetch(url, {
      method: 'POST',
      headers: this.headers(),
      credentials: 'include',
      body: payload,
    });
    return { status: res.status, ok: res.ok, text: await res.text() };
  }
}
