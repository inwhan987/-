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
  // Same underlying service, newer domain Metered has been migrating
  // customers to -- included in case the older openrelay.metered.ca
  // hostname is throttled/deprecated for some networks while this one
  // isn't (or vice versa). A wrong/unreachable entry here just gets
  // skipped by ICE, so there's no downside to listing both.
  { urls: 'stun:stun.relay.metered.ca:80' },
  { urls: 'turn:global.relay.metered.ca:80', username: 'openrelayproject', credential: 'openrelayproject' },
  { urls: 'turn:global.relay.metered.ca:443', username: 'openrelayproject', credential: 'openrelayproject' },
  {
    urls: 'turn:global.relay.metered.ca:443?transport=tcp',
    username: 'openrelayproject',
    credential: 'openrelayproject',
  },
  // TURN-over-TLS (the turns: scheme, not turn:) -- plain TURN, even over
  // TCP, is still identifiable as TURN by anything doing deep packet
  // inspection (the message types are part of the unencrypted protocol
  // header). Real-world data point: 8s+ of gathering time produced zero
  // relay candidates from *any* of the 6 plain turn: entries above while
  // STUN succeeded instantly -- consistent with a network that's actively
  // dropping/blocking recognized TURN traffic rather than one that's just
  // slow, which some mobile carrier and corporate firewalls do specifically
  // to prevent using TURN as a VPN-like tunnel. Wrapped in TLS, TURN
  // traffic is indistinguishable from ordinary HTTPS to that kind of
  // inspection, since only the negotiated port/protocol is visible.
  { urls: 'turns:global.relay.metered.ca:443?transport=tcp', username: 'openrelayproject', credential: 'openrelayproject' },
  { urls: 'turns:openrelay.metered.ca:443?transport=tcp', username: 'openrelayproject', credential: 'openrelayproject' },
];

// Metered's shared public demo TURN pool (above) proved unreliable in
// practice -- real device testing got everything from 400 (Bad Request) to
// 701 (timeout, i.e. the server not responding at all), consistent with a
// pool anyone on the internet can hit rather than one backed by an actual
// account. A real (free-tier) Metered account gets its own dedicated TURN
// credentials via this REST endpoint instead of a shared static
// username/password -- try this first, and only fall back to the public
// pool above if the fetch itself fails (offline, endpoint down, etc.).
const METERED_TURN_CREDENTIALS_URL =
  'https://jetkvm.metered.live/api/v1/turn/credentials?apiKey=d7f3993deef09dc12b185968598055a1d992';

async function fetchIceServers(): Promise<RTCIceServer[]> {
  try {
    const res = await fetch(METERED_TURN_CREDENTIALS_URL);
    if (!res.ok) return DEFAULT_ICE;
    const servers = (await res.json()) as RTCIceServer[];
    if (!Array.isArray(servers) || servers.length === 0) return DEFAULT_ICE;
    return [{ urls: 'stun:stun.l.google.com:19302' }, ...servers];
  } catch {
    return DEFAULT_ICE; // offline, endpoint down, etc. -- fall back to the public pool
  }
}

// The request/response envelope field for /webrtc/session. JetKVM's OfferData
// uses "sd" (base64 SDP). We also read a few fallbacks when parsing the answer
// so a firmware revision that renames it still works. VERIFY on real hardware.
const SESSION_OFFER_FIELD = 'sd';

export class JetKvmClient {
  private pc: RTCPeerConnection | null = null;
  private rpc: RTCDataChannel | null = null;
  private ws: WebSocket | null = null;
  private base = '';
  private transport: JetKvmTransport | null = null;
  private rpcId = 1;
  private state: ConnectionState = 'idle';
  private lastVideoStat: { bytes: number; ts: number } | null = null;
  private lastOfferSdp: string | null = null;
  private lastAnswerSdp: string | null = null;
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

  // Trickle-ICE side channel for candidates the device finds after
  // answering (see the comment above pc.onicecandidate in openPeer). Same
  // path JetKVM's own frontend uses. Best-effort: if this can't connect
  // (e.g. the device requires auth on it that we don't have a cookie for —
  // our login goes through a native HTTP bridge, not the page's own cookie
  // jar, exactly the problem transport.ts exists to work around elsewhere)
  // candidates just never arrive and the connection times out the same way
  // it already did before this existed, no worse off.
  private openSignalingSocket(pc: RTCPeerConnection) {
    try {
      const url = new URL(this.base);
      const wsProtocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${wsProtocol}//${url.host}/webrtc/signaling/client`);
      this.ws = ws;
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as { type?: string; data?: unknown };
          if (msg.type === 'new-ice-candidate' && msg.data) {
            void pc.addIceCandidate(msg.data as RTCIceCandidateInit).catch(() => {});
          }
        } catch {
          /* not JSON / unrecognized shape -- ignore */
        }
      };
      ws.onerror = () => {
        /* best-effort; connection can still succeed on local-only candidates */
      };
    } catch {
      /* WebSocket constructor threw (bad URL etc.) -- ignore, same as onerror */
    }
  }

  async connect(opts: ConnectOptions): Promise<void> {
    this.base = JetKvmClient.normalizeBase(opts.host);
    this.transport = new JetKvmTransport(this.base);
    try {
      // Runs concurrently with authenticate() below (independent of it) so
      // fetching fresh TURN credentials doesn't add to connect time in the
      // common case.
      const icePromise = opts.iceServers ? Promise.resolve(opts.iceServers) : fetchIceServers();
      await this.authenticate(opts.password ?? '');
      await this.openPeer(await icePromise);
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

    // Diagnostic only (this stays non-trickle -- candidates found after
    // gathering "completes" still aren't sent separately). "ICE: new" that
    // never moves is ambiguous on its own: it could mean gathering produced
    // literally zero local candidates (nothing to even attempt), or that it
    // found some but checking never started. Counting by type here is the
    // difference between "no candidates at all" (points at something
    // blocking WebRTC/UDP itself) and "only a host candidate" (points at
    // STUN/TURN specifically not producing anything on that network).
    const candidateCounts = { host: 0, srflx: 0, relay: 0, prflx: 0 };
    // The device's answer to /webrtc/session comes back with ZERO
    // a=candidate lines (confirmed by inspecting a real exchange) -- it
    // trickles its own candidates separately over this signaling
    // WebSocket instead, exactly like JetKVM's own frontend
    // (ui/src/routes/devices.$id.tsx: onicecandidate sends
    // {type:"new-ice-candidate", data: candidate} over the same socket,
    // and incoming messages of that type get passed to addIceCandidate).
    // Without this, ICE has nothing on the remote side to check against at
    // all, regardless of how many *local* candidates we gather -- which is
    // exactly why iceConnectionState was stuck on "new" even with relay
    // candidates working fine locally.
    this.openSignalingSocket(pc);
    pc.onicecandidate = (ev) => {
      if (!ev.candidate) return;
      const type = ev.candidate.type as keyof typeof candidateCounts | undefined;
      if (type && type in candidateCounts) candidateCounts[type]++;
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'new-ice-candidate', data: ev.candidate.toJSON() }));
      }
    };
    const candidateSummary = () =>
      `h${candidateCounts.host}/s${candidateCounts.srflx}/r${candidateCounts.relay}`;

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
        this.setState(this.state, `ICE: ${pc.iceConnectionState} (${candidateSummary()})`);
      }
    };

    // Create the offer and wait for ICE gathering to finish so we can hand the
    // firmware a single complete SDP (non-trickle) over the HTTP endpoint.
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await this.waitForIceGathering(pc);
    // Gathering finished (or timed out) -- show what we actually got before
    // even sending the offer, since that's the earliest point something
    // could already be visibly wrong (e.g. h0/s0/r0 -- WebRTC/UDP itself
    // producing nothing at all, vs h1/s0/r0 -- only the useless-externally
    // host candidate, meaning STUN/TURN specifically found nothing).
    this.setState('signaling', `후보 수집 완료 (${candidateSummary()})`);

    this.setState('connecting');
    this.lastOfferSdp = pc.localDescription!.sdp;
    const answer = await this.exchangeSdp(pc.localDescription!);
    this.lastAnswerSdp = answer.sdp ?? null;
    await pc.setRemoteDescription(answer);

    // Without this, a peer connection that never reaches a terminal
    // connectionState (some networks just leave ICE stuck "checking"
    // forever instead of ever reporting "failed") left the UI on "연결중"
    // indefinitely with no way out except force-closing the app.
    connectTimeout = setTimeout(() => {
      if (this.state !== 'connected') {
        this.setState(
          'failed',
          `연결 시간 초과 (ICE: ${pc.iceConnectionState}, 후보 ${candidateSummary()}) — 네트워크 경로를 확인하세요`,
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
      // Safety timeout: some networks never report "complete". NOT a timing
      // issue after all -- confirmed h6/s6/r0 (zero relay candidates from
      // any of the 6 TURN entries) happened identically at both 3s and 8s
      // of gathering time, so a longer wait was never going to help; the
      // real fix is turns: (TURN-over-TLS) above, for networks that block
      // plain TURN outright. No reason to make every connection wait
      // longer than necessary for something extra time doesn't fix.
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

  /** The last offer/answer SDP exchanged, for copy-and-send-me-the-output
   *  debugging of stuck connections -- null until a connection attempt has
   *  gotten at least as far as sending an offer. */
  getDebugSdp(): { offer: string | null; answer: string | null } {
    return { offer: this.lastOfferSdp, answer: this.lastAnswerSdp };
  }

  close() {
    try {
      this.rpc?.close();
      this.pc?.close(); // also closes the other data channels created in openPeer()
      this.ws?.close();
    } catch {
      /* ignore */
    }
    this.rpc = null;
    this.pc = null;
    this.ws = null;
    if (this.state !== 'failed') this.setState('closed');
  }
}
