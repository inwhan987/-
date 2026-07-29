// JetKVM WebRTC client (core protocol layer)
// ---------------------------------------------------------------------------
// This module talks to a JetKVM device the same way its own web UI does:
//   1. POST /auth/login-local        -> establishes a session cookie (if the
//                                        device has a local password set)
//   2. POST /webrtc/session          -> exchange a base64-encoded SDP offer for
//                                        a base64-encoded SDP answer (the
//                                        "legacy" HTTP signaling path — simplest
//                                        for a native client, no websocket)
//   3. WebRTC media track            -> the remote screen (H.264 video)
//   4. "hidrpc" data channel         -> JSON-RPC keyboard/mouse reports
//
// Everything device-specific lives here so that, once a real device is on hand,
// only this file needs adjusting. Fields marked "VERIFY" are the ones worth
// double-checking against the firmware you actually run.
//
// Cross-origin note: JetKVM's local API authenticates with a plain cookie
// (authToken) and sends no CORS headers. Beyond CORS, the Fetch spec also
// unconditionally hides Set-Cookie from JS and forbids scripts from setting
// a Cookie header themselves — restrictions no CORS/same-origin workaround
// can lift. So the login+signaling calls go through JetKvmTransport (see
// transport.ts), which reads/sends the cookie manually over a platform
// bridge that isn't bound by the Fetch spec at all (CapacitorHttp's plugin
// API on Android/iOS, Node's main process via IPC on Electron).
// ---------------------------------------------------------------------------

import { JetKvmTransport } from './transport';

export type ConnectionState =
  | 'idle'
  | 'authenticating'
  | 'signaling'
  | 'connecting'
  | 'connected'
  | 'failed'
  | 'closed';

export interface JetKvmClientEvents {
  onState?: (state: ConnectionState, detail?: string) => void;
  onStream?: (stream: MediaStream) => void;
  onError?: (err: Error) => void;
}

export interface ConnectOptions {
  /** Host or full URL, e.g. "192.168.1.50", "jetkvm.tailnet.ts.net",
   *  or "https://jetkvm.tailnet.ts.net". */
  host: string;
  /** Local device password. Empty string if password protection is disabled. */
  password?: string;
  /** Extra ICE servers (STUN/TURN). A public STUN default is included. */
  iceServers?: RTCIceServer[];
}

const DEFAULT_ICE: RTCIceServer[] = [
  { urls: 'stun:stun.l.google.com:19302' },
];

// The request/response envelope field for /webrtc/session. JetKVM's OfferData
// uses "sd" (base64 SDP). We also read a few fallbacks when parsing the answer
// so a firmware revision that renames it still works. VERIFY on real hardware.
const SESSION_OFFER_FIELD = 'sd';

export class JetKvmClient {
  private pc: RTCPeerConnection | null = null;
  private hid: RTCDataChannel | null = null;
  private rpc: RTCDataChannel | null = null;
  private base = '';
  private transport: JetKvmTransport | null = null;
  private rpcId = 1;
  private state: ConnectionState = 'idle';

  constructor(private events: JetKvmClientEvents = {}) {}

  get connectionState() {
    return this.state;
  }

  private setState(s: ConnectionState, detail?: string) {
    this.state = s;
    this.events.onState?.(s, detail);
  }

  /** Normalize a user-entered host into a base URL (https by default). */
  static normalizeBase(host: string): string {
    const h = host.trim();
    if (/^https?:\/\//i.test(h)) return h.replace(/\/+$/, '');
    return `https://${h.replace(/\/+$/, '')}`;
  }

  async connect(opts: ConnectOptions): Promise<void> {
    this.base = JetKvmClient.normalizeBase(opts.host);
    this.transport = new JetKvmTransport(this.base);
    try {
      await this.authenticate(opts.password ?? '');
      await this.openPeer(opts.iceServers ?? DEFAULT_ICE);
    } catch (err) {
      const e = err instanceof Error ? err : new Error(String(err));
      this.setState('failed', e.message);
      this.events.onError?.(e);
      this.close();
      throw e;
    }
  }

  // --- Step 1: authenticate -------------------------------------------------
  private async authenticate(password: string) {
    if (!password) return; // no-password mode: nothing to do
    this.setState('authenticating');
    const res = await this.transport!.post('/auth/login-local', { password });
    if (!res.ok) {
      throw new Error(
        res.status === 401
          ? 'Wrong password'
          : `Login failed (HTTP ${res.status})`,
      );
    }
  }

  // --- Steps 2-4: WebRTC ----------------------------------------------------
  private async openPeer(iceServers: RTCIceServer[]) {
    this.setState('signaling');
    const pc = new RTCPeerConnection({ iceServers });
    this.pc = pc;

    // We only receive video/audio; we never send media.
    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.addTransceiver('audio', { direction: 'recvonly' });

    // Control + input channels (labels match JetKVM firmware).
    this.rpc = pc.createDataChannel('rpc', { ordered: true });
    this.hid = pc.createDataChannel('hidrpc', { ordered: true });

    pc.ontrack = (ev) => {
      if (ev.streams[0]) this.events.onStream?.(ev.streams[0]);
    };

    pc.onconnectionstatechange = () => {
      switch (pc.connectionState) {
        case 'connected':
          this.setState('connected');
          break;
        case 'failed':
          this.setState('failed', 'peer connection failed');
          break;
        case 'disconnected':
        case 'closed':
          this.setState('closed');
          break;
      }
    };

    // Create the offer and wait for ICE gathering to finish so we can hand the
    // firmware a single complete SDP (non-trickle) over the HTTP endpoint.
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await this.waitForIceGathering(pc);

    this.setState('connecting');
    const answer = await this.exchangeSdp(pc.localDescription!);
    await pc.setRemoteDescription(answer);
  }

  private waitForIceGathering(pc: RTCPeerConnection): Promise<void> {
    if (pc.iceGatheringState === 'complete') return Promise.resolve();
    return new Promise((resolve) => {
      const check = () => {
        if (pc.iceGatheringState === 'complete') {
          pc.removeEventListener('icegatheringstatechange', check);
          resolve();
        }
      };
      pc.addEventListener('icegatheringstatechange', check);
      // Safety timeout: some networks never report "complete".
      setTimeout(() => {
        pc.removeEventListener('icegatheringstatechange', check);
        resolve();
      }, 3000);
    });
  }

  private async exchangeSdp(
    local: RTCSessionDescription,
  ): Promise<RTCSessionDescriptionInit> {
    const offerB64 = btoa(JSON.stringify(local));
    const res = await this.transport!.post('/webrtc/session', {
      [SESSION_OFFER_FIELD]: offerB64,
    });
    if (!res.ok) {
      throw new Error(`Signaling failed (HTTP ${res.status})`);
    }
    const body = JSON.parse(res.text);
    // Accept a few shapes: {sd}, {answer}, {result:{sd}}.
    const answerB64: string | undefined =
      body.sd ?? body.answer ?? body.result?.sd ?? body.result;
    if (!answerB64 || typeof answerB64 !== 'string') {
      throw new Error('No SDP answer in signaling response');
    }
    return JSON.parse(atob(answerB64)) as RTCSessionDescriptionInit;
  }

  // --- HID input over the data channel --------------------------------------
  private sendHid(method: string, params: Record<string, unknown>) {
    if (!this.hid || this.hid.readyState !== 'open') return;
    const msg = { jsonrpc: '2.0', method, params, id: this.rpcId++ };
    this.hid.send(JSON.stringify(msg));
  }

  /** modifier: bitmask of Ctrl/Shift/Alt/GUI. keys: up to 6 USB HID usage IDs. */
  keyboardReport(modifier: number, keys: number[]) {
    this.sendHid('keyboardReport', { modifier, keys: keys.slice(0, 6) });
  }

  /** Absolute pointer. x/y are 0..32767 across the video surface. */
  absMouseReport(x: number, y: number, buttons: number) {
    this.sendHid('absMouseReport', {
      x: Math.max(0, Math.min(32767, Math.round(x))),
      y: Math.max(0, Math.min(32767, Math.round(y))),
      buttons,
    });
  }

  /** Relative pointer. dx/dy are signed 8-bit deltas. */
  relMouseReport(dx: number, dy: number, buttons: number) {
    const clamp = (v: number) => Math.max(-127, Math.min(127, Math.round(v)));
    this.sendHid('relMouseReport', { dx: clamp(dx), dy: clamp(dy), buttons });
  }

  /** Scroll wheel. Positive = up, negative = down (small integers). */
  wheelReport(wheelY: number) {
    this.sendHid('wheelReport', { wheelY: Math.max(-127, Math.min(127, wheelY)) });
  }

  close() {
    try {
      this.hid?.close();
      this.rpc?.close();
      this.pc?.close();
    } catch {
      /* ignore */
    }
    this.hid = this.rpc = null;
    this.pc = null;
    if (this.state !== 'failed') this.setState('closed');
  }
}
