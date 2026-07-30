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

export interface ConnStats {
  bitrateKbps: number | null;
  fps: number | null;
  rttMs: number | null;
  packetsLost: number | null;
  /** "relay" | "srflx" | "prflx" | "host" — how the media path was reached. */
  candidateType: string | null;
}

// STUN alone can't traverse a symmetric/carrier-grade NAT -- common on
// mobile data -- since it only helps two peers discover each other's public
// address, not relay traffic when a direct path isn't possible at all
// (confirmed: same WiFi as the device connects fine, switching to LTE gets
// stuck). A TURN relay is the actual fix for that case. Metered's OpenRelay
// is a free, no-signup, publicly documented TURN service commonly used for
// exactly this (published static credentials, not a secret) -- WebRTC tries
// STUN/direct paths first regardless and only falls back to relaying
// through here if nothing better works, so this is a fallback, not the
// primary path. It's a shared free tier with bandwidth limits, not a
// guaranteed-forever fix; a real deployment would want its own TURN server.
const DEFAULT_ICE: RTCIceServer[] = [
  { urls: 'stun:stun.l.google.com:19302' },
  { urls: 'stun:openrelay.metered.ca:80' },
  { urls: 'turn:openrelay.metered.ca:80', username: 'openrelayproject', credential: 'openrelayproject' },
  { urls: 'turn:openrelay.metered.ca:443', username: 'openrelayproject', credential: 'openrelayproject' },
  {
    urls: 'turn:openrelay.metered.ca:443?transport=tcp',
    username: 'openrelayproject',
    credential: 'openrelayproject',
  },
];

// The request/response envelope field for /webrtc/session. JetKVM's OfferData
// uses "sd" (base64 SDP). We also read a few fallbacks when parsing the answer
// so a firmware revision that renames it still works. VERIFY on real hardware.
const SESSION_OFFER_FIELD = 'sd';

export class JetKvmClient {
  private pc: RTCPeerConnection | null = null;
  private rpc: RTCDataChannel | null = null;
  private base = '';
  private transport: JetKvmTransport | null = null;
  private rpcId = 1;
  private state: ConnectionState = 'idle';
  private lastVideoStat: { bytes: number; ts: number } | null = null;
  private pendingCalls = new Map<
    number,
    { resolve: (v: unknown) => void; reject: (e: Error) => void }
  >();

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

    // Confirmed against JetKVM's own frontend (ui/src/routes/devices.$id.tsx,
    // setupPeerConnection): the CLIENT creates all four channels — the
    // device never initiates its own, so there's no ondatachannel to listen
    // for. "hidrpc" is binary-only (binaryType set to arraybuffer) with an
    // undocumented byte layout; the two unreliable variants are for the
    // same. Rather than guess that binary format, HID reports go over "rpc"
    // as plain JSON-RPC (keyboardReport/absMouseReport/etc.) — confirmed to
    // exist server-side (jsonrpc.go) as the documented legacy/compat path,
    // same channel and format the settings calls already use.
    this.rpc = pc.createDataChannel('rpc', { ordered: true });
    this.rpc.onmessage = (ev) => this.onRpcMessage(ev.data);

    const hidBinary = pc.createDataChannel('hidrpc', { ordered: true });
    hidBinary.binaryType = 'arraybuffer';
    pc.createDataChannel('hidrpc-unreliable-ordered', { ordered: true, maxRetransmits: 0 });
    pc.createDataChannel('hidrpc-unreliable-nonordered', { ordered: false, maxRetransmits: 0 });

    pc.ontrack = (ev) => {
      if (ev.streams[0]) this.events.onStream?.(ev.streams[0]);
    };

    let connectTimeout: ReturnType<typeof setTimeout> | null = null;
    const clearConnectTimeout = () => {
      if (connectTimeout) {
        clearTimeout(connectTimeout);
        connectTimeout = null;
      }
    };

    pc.onconnectionstatechange = () => {
      switch (pc.connectionState) {
        case 'connected':
          clearConnectTimeout();
          this.setState('connected');
          break;
        case 'failed':
          clearConnectTimeout();
          this.setState('failed', 'peer connection failed');
          break;
        case 'disconnected':
        case 'closed':
          clearConnectTimeout();
          this.setState('closed');
          break;
      }
    };
    // ICE connection state alone (before the overall connectionState reaches
    // a terminal value) shown live in the status line -- previously the UI
    // just sat on "연결중" with zero information if ICE never actually
    // connected, indistinguishable from "still working on it". This surfaces
    // what ICE itself is doing (checking / disconnected / etc.) so a stuck
    // connection is at least screenshot-able instead of a silent hang.
    pc.oniceconnectionstatechange = () => {
      if (this.state === 'connecting' || this.state === 'signaling') {
        this.setState(this.state, `ICE: ${pc.iceConnectionState}`);
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

    // Without this, a peer connection that never reaches a terminal
    // connectionState (some networks just leave ICE stuck "checking"
    // forever instead of ever reporting "failed") left the UI on "연결중"
    // indefinitely with no way out except force-closing the app.
    connectTimeout = setTimeout(() => {
      if (this.state !== 'connected') {
        this.setState(
          'failed',
          `연결 시간 초과 (ICE: ${pc.iceConnectionState}) — 네트워크 경로를 확인하세요`,
        );
        this.close();
      }
    }, 20000);
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

  // --- HID input, sent as JSON-RPC over the "rpc" channel (same channel and
  // format settings calls use — see the note in openPeer() for why not the
  // binary "hidrpc" channel). Fire-and-forget: any response is just dropped
  // by onRpcMessage since no pending entry is registered for its id. --------
  private sendHid(method: string, params: Record<string, unknown>) {
    if (!this.rpc || this.rpc.readyState !== 'open') return;
    const msg = { jsonrpc: '2.0', method, params, id: this.rpcId++ };
    this.rpc.send(JSON.stringify(msg));
  }

  // --- General JSON-RPC calls (settings, device state) over the "rpc"
  // channel — these expect a matching {id, result} response, unlike the
  // fire-and-forget HID reports above. Method names are taken from JetKVM's
  // own frontend source (ui/src/routes/devices.$id.settings.*.tsx). ---------
  private onRpcMessage(data: string) {
    let msg: { id?: number; result?: unknown; error?: { message?: string } };
    try {
      msg = JSON.parse(data);
    } catch {
      return;
    }
    if (msg.id === undefined) return;
    const pending = this.pendingCalls.get(msg.id);
    if (!pending) return;
    this.pendingCalls.delete(msg.id);
    if (msg.error) pending.reject(new Error(msg.error.message ?? 'RPC error'));
    else pending.resolve(msg.result);
  }

  call(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    if (!this.rpc || this.rpc.readyState !== 'open') {
      return Promise.reject(new Error('RPC channel not ready'));
    }
    const id = this.rpcId++;
    const rpc = this.rpc;
    return new Promise((resolve, reject) => {
      this.pendingCalls.set(id, { resolve, reject });
      rpc.send(JSON.stringify({ jsonrpc: '2.0', method, params, id }));
      setTimeout(() => {
        if (this.pendingCalls.delete(id)) reject(new Error('RPC timeout'));
      }, 5000);
    });
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

  /** Live WebRTC stats for a "connection info" panel. Call while connected. */
  async getStats(): Promise<ConnStats | null> {
    if (!this.pc) return null;
    const report = await this.pc.getStats();
    const stats: ConnStats = {
      bitrateKbps: null,
      fps: null,
      rttMs: null,
      packetsLost: null,
      candidateType: null,
    };
    const pair: { nominated: { localCandidateId?: string } | null } = { nominated: null };

    report.forEach((s: RTCStats & Record<string, unknown>) => {
      if (s.type === 'inbound-rtp' && s.kind === 'video') {
        stats.fps = (s.framesPerSecond as number) ?? null;
        stats.packetsLost = (s.packetsLost as number) ?? null;
        const bytes = s.bytesReceived as number | undefined;
        if (bytes !== undefined) {
          if (this.lastVideoStat) {
            const dtSec = (s.timestamp - this.lastVideoStat.ts) / 1000;
            const dBytes = bytes - this.lastVideoStat.bytes;
            if (dtSec > 0) stats.bitrateKbps = Math.round((dBytes * 8) / dtSec / 1000);
          }
          this.lastVideoStat = { bytes, ts: s.timestamp as number };
        }
      }
      if (s.type === 'candidate-pair' && s.nominated && s.state === 'succeeded') {
        pair.nominated = s as { localCandidateId?: string };
        const rtt = s.currentRoundTripTime as number | undefined;
        stats.rttMs = rtt !== undefined ? Math.round(rtt * 1000) : null;
      }
    });

    if (pair.nominated?.localCandidateId) {
      const localCand = report.get(pair.nominated.localCandidateId) as
        | (RTCStats & { candidateType?: string })
        | undefined;
      stats.candidateType = localCand?.candidateType ?? null;
    }

    return stats;
  }

  close() {
    try {
      this.rpc?.close();
      this.pc?.close(); // also closes the other data channels created in openPeer()
    } catch {
      /* ignore */
    }
    this.rpc = null;
    this.pc = null;
    if (this.state !== 'failed') this.setState('closed');
  }
}
