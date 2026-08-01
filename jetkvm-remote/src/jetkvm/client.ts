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
  /** Extra ICE servers (STUN/TURN). None by default -- see the note above
   *  DEFAULT_ICE for why. */
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

// No STUN/TURN servers at all, by design. Every real capture taken during
// the LTE investigation (both the free Metered pool and our own dedicated
// coturn box) confirmed the same thing: the device only ever advertises its
// private LAN address, which no TURN relay has a route to -- packets a relay
// forwards toward it just vanish (verified with a live tcpdump on our own
// coturn server). No STUN/TURN configuration can fix that; it was never the
// actual bottleneck. What does work is withPublicIpCandidate() below, and it
// needs nothing external at all. Dropping every third-party server removes a
// dependency, a source of multi-second gathering delay, and a maintenance
// burden (the Oracle box, Metered credentials) for zero loss of the thing
// that actually connects.
const DEFAULT_ICE: RTCIceServer[] = [];

// The request/response envelope field for /webrtc/session. JetKVM's OfferData
// uses "sd" (base64 SDP). We also read a few fallbacks when parsing the answer
// so a firmware revision that renames it still works. VERIFY on real hardware.
const SESSION_OFFER_FIELD = 'sd';

// The device only ever gathers one ICE candidate: its own private LAN host
// address (no STUN/TURN configured in its firmware -- confirmed by reading
// jetkvm/kvm's webrtc.go). That address is meaningless to any client outside
// that LAN, and no TURN server can relay to it either (a relay server has no
// route to a private IP). The one network that's actually reachable from
// outside is the router's own public IP, with the device's LAN fully exposed
// via DMZ -- but the device never advertises that address as a candidate,
// since it has no way to know it. This substitutes it ourselves: same port
// the device already told us about (DMZ forwards every port 1:1, unchanged),
// just with the public IP swapped in, added as an extra candidate alongside
// the real one. Costs nothing if wrong (ICE just never pairs it); fixes
// connectivity entirely when it's right, without touching the firmware.
const DEVICE_LAN_PUBLIC_IP = '121.190.100.246';

function withPublicIpCandidate(candidate: RTCIceCandidateInit): RTCIceCandidateInit[] {
  const raw = candidate.candidate;
  if (!raw) return [candidate];
  const parts = raw.split(' ');
  const addr = parts[4];
  if (!addr || !/^(?:10\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.)/.test(addr)) {
    return [candidate];
  }
  const publicParts = [...parts];
  publicParts[4] = DEVICE_LAN_PUBLIC_IP;
  // foundation (parts[0], "candidate:<foundation>") must differ from the
  // original so ICE treats this as a distinct candidate rather than a
  // duplicate of the one already added.
  publicParts[0] = `${parts[0]}pub`;
  return [candidate, { ...candidate, candidate: publicParts.join(' ') }];
}

export class JetKvmClient {
  private pc: RTCPeerConnection | null = null;
  private rpc: RTCDataChannel | null = null;
  private signalingWs: WebSocket | null = null;
  private localCandidates: RTCIceCandidate[] = [];
  private pendingAnswer: ((answer: RTCSessionDescriptionInit) => void) | null = null;
  private base = '';
  private transport: JetKvmTransport | null = null;
  private rpcId = 1;
  private state: ConnectionState = 'idle';
  private lastVideoStat: { bytes: number; ts: number } | null = null;
  private lastOfferSdp: string | null = null;
  private lastAnswerSdp: string | null = null;
  // Rolling buffer of timestamped lifecycle events -- state transitions,
  // ICE state, HTTP call timing -- so a failure can be diagnosed from one
  // "로그 복사" paste instead of walking the user through remote-debugging
  // setup (chrome://inspect, adb, etc.) every single time. Capped so a
  // long-lived connection doesn't grow this forever.
  private logLines: string[] = [];
  private pendingCalls = new Map<
    number,
    { resolve: (v: unknown) => void; reject: (e: Error) => void }
  >();

  constructor(private events: JetKvmClientEvents = {}) {}

  get connectionState() {
    return this.state;
  }

  private log(msg: string) {
    const t = new Date().toISOString().slice(11, 23);
    this.logLines.push(`[${t}] ${msg}`);
    if (this.logLines.length > 300) this.logLines.shift();
  }

  private setState(s: ConnectionState, detail?: string) {
    this.state = s;
    this.log(`state -> ${s}${detail ? ` (${detail})` : ''}`);
    this.events.onState?.(s, detail);
  }

  /** Normalize a user-entered host into a base URL (https by default). */
  static normalizeBase(host: string): string {
    const h = host.trim();
    if (/^https?:\/\//i.test(h)) return h.replace(/\/+$/, '');
    return `https://${h.replace(/\/+$/, '')}`;
  }

  async connect(opts: ConnectOptions): Promise<void> {
    this.logLines = [];
    this.localCandidates = [];
    this.sessionEstablished = false;
    this.candidateSendCursor = 0;
    this.pendingAnswer = null;
    this.log(`connect() host=${opts.host}`);
    this.base = JetKvmClient.normalizeBase(opts.host);
    this.transport = new JetKvmTransport(this.base);
    try {
      await this.authenticate(opts.password ?? '');
      const iceServers = opts.iceServers ?? DEFAULT_ICE;
      this.log(`iceServers: ${iceServers.length} entries`);
      await this.openPeer(iceServers);
    } catch (err) {
      const e = err instanceof Error ? err : new Error(String(err));
      this.log(`connect() failed: ${e.message}`);
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

  // The device's answer to /webrtc/session always has zero a=candidate
  // lines by design (confirmed repeatedly via debug-SDP captures) -- it
  // trickles its own candidates individually over this signaling
  // WebSocket instead, same as JetKVM's own frontend. Without this, ICE
  // has nothing on the remote side to check against at all: local
  // candidates (even a full h6/s10/r12) don't matter if the connection
  // never learns a single remote one, which is exactly why iceConnectionState
  // was confirmed (via the debug log) to sit on "new" -- not "checking",
  // "new" -- for the entire 20s timeout on a real LTE attempt.
  //
  // A previous attempt at this (bca5f85) was reverted after it broke a
  // working desktop connection -- almost certainly because it opened a
  // raw cross-origin WebSocket straight to the device, which can't carry
  // the authToken cookie (that cookie is scoped to our own app's origin,
  // set via document.cookie in transport.ts, not the device's origin) and
  // got rejected/misbehaved, the same class of bug later confirmed and
  // fixed for the settings screen's own WS ("Expected HTTP 101 response
  // but was '401 Unauthorized'"). This time it targets our OWN origin's
  // /webrtc/signaling/client instead: on Android/Electron that's a local
  // proxy (JetKvmProxyServer.java / electron/main.cjs) which already
  // forwards the same cookie to the real device -- same-origin, so the
  // browser attaches it automatically, no extra plumbing needed. iOS/dev
  // have no such proxy, so this simply fails to connect there and does
  // nothing -- no worse off than before.
  //
  // Also sends our OWN candidates over this socket now -- confirmed via
  // the device's own log (tail -f /userdata/jetkvm/last.log over SSH):
  // "pion ice Failed to ping without candidate pairs. Connection is not
  // possible yet." right after processing our offer. Despite embedding
  // every local candidate directly in the non-trickle SDP, Pion's ICE
  // agent -- because our offer advertises a=ice-options:trickle -- never
  // reads them from there at all and waits exclusively for candidates
  // delivered individually over this channel, exactly like JetKVM's own
  // frontend does. Local gathering already finished before the offer was
  // sent (that's the non-trickle wait), so there's nothing to wait for --
  // the moment this socket opens, every candidate we have gets sent at
  // once.
  //
  // BUT: sending needs to wait on a second condition too, not just the
  // socket being open. Confirmed via device logs -- opening the socket as
  // early as possible (to hide its own handshake latency, see the comment
  // by its call site) means it can finish before /webrtc/session's answer
  // does, and candidates sent before that point get silently dropped:
  // "no current session, skipping incoming ICE candidate" (session doesn't
  // exist yet) or "dropping candidate with ufrag RVtl because it doesn't
  // match the current ufrags" (stale candidates from a previous attempt
  // arriving after a new one's session replaced it). sessionEstablished
  // gates sending on BOTH the socket being open AND setRemoteDescription()
  // having actually succeeded for oneself, so nothing goes out until the
  // device has a session to associate it with.
  private sessionEstablished = false;
  private candidateSendCursor = 0;
  private trySendCandidates() {
    if (!this.sessionEstablished || this.signalingWs?.readyState !== WebSocket.OPEN) return;
    while (this.candidateSendCursor < this.localCandidates.length) {
      const c = this.localCandidates[this.candidateSendCursor++];
      this.signalingWs.send(JSON.stringify({ type: 'new-ice-candidate', data: c.toJSON() }));
    }
  }

  // Resolves once the signaling socket is open, or after timeoutMs --
  // never rejects, since platforms with no local proxy for this socket to
  // open on (iOS/dev) should just proceed without it, same as always.
  private waitForSignalingSocket(timeoutMs: number): Promise<void> {
    return new Promise((resolve) => {
      const ws = this.signalingWs;
      if (!ws || ws.readyState === WebSocket.OPEN) {
        resolve();
        return;
      }
      const timer = setTimeout(resolve, timeoutMs);
      const done = () => {
        clearTimeout(timer);
        resolve();
      };
      ws.addEventListener('open', done, { once: true });
      ws.addEventListener('error', done, { once: true });
    });
  }

  // Do the offer/answer exchange over the signaling WebSocket instead of
  // the HTTP POST endpoint. THIS IS THE FIX for the device never trickling
  // its own ICE candidates to us, confirmed by reading the firmware:
  //
  //   webrtc.go:  OnICECandidate -> `if candidate != nil && config.ws != nil`
  //   web.go:     POST /webrtc/session -> newSession(SessionConfig{MDNSMode: ...})
  //                                       ^ no ws field set, so it stays nil
  //   web.go:     ws "offer" message   -> handleSessionRequest(ctx, wsCon, ...)
  //                                       ^ receives the socket, so ws IS set
  //
  // Creating the session over HTTP leaves config.ws nil, so the device
  // physically cannot send us a single candidate -- which matches every
  // capture we took: the only message ever received on the socket was
  // device-metadata, never a new-ice-candidate.
  //
  // That also explains why relay-only failed while DMZ worked. With no
  // remote candidate ever learned, the only way a pair can form is
  // peer-reflexive: the device pings one of OUR candidates and we discover
  // it from the incoming check. That works when our address is directly
  // reachable (DMZ'd router), but through TURN it cannot -- a TURN server
  // only forwards packets from peers we've installed a permission for, and
  // we can't install one for an address we were never told. Hence r12
  // candidates sitting unused and ICE never leaving "new".
  //
  // Message shapes taken from the firmware's own frontend
  // (ui/src/routes/devices.$id.tsx): send {type:"offer", data:{sd}} where
  // sd = btoa(JSON.stringify(localDescription)); the answer arrives as
  // {type:"answer", data:"<same base64 encoding>"}.
  private exchangeSdpOverWs(
    local: RTCSessionDescriptionInit,
    timeoutMs: number,
  ): Promise<RTCSessionDescriptionInit> {
    const ws = this.signalingWs;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error('signaling ws not open'));
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingAnswer = null;
        reject(new Error('no answer over signaling ws'));
      }, timeoutMs);
      this.pendingAnswer = (answer) => {
        clearTimeout(timer);
        resolve(answer);
      };
      ws.send(
        JSON.stringify({
          type: 'offer',
          data: { sd: btoa(JSON.stringify(local)) },
        }),
      );
    });
  }

  private openSignalingSocket(pc: RTCPeerConnection) {
    try {
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${proto}//${window.location.host}/webrtc/signaling/client`);
      this.signalingWs = ws;
      ws.onopen = () => {
        this.log('signaling ws open');
        this.trySendCandidates();
      };
      ws.onerror = () => this.log('signaling ws error (no local proxy on this platform?)');
      ws.onclose = (ev) => this.log(`signaling ws closed (code ${ev.code})`);
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as { type?: string; data?: unknown };
          if (msg.type === 'new-ice-candidate' && msg.data) {
            this.log(`trickled candidate: ${JSON.stringify(msg.data).slice(0, 200)}`);
            for (const c of withPublicIpCandidate(msg.data as RTCIceCandidateInit)) {
              void pc.addIceCandidate(c).catch((e) => this.log(`addIceCandidate failed: ${e}`));
            }
          } else if (msg.type === 'answer' && typeof msg.data === 'string') {
            // Answer to an offer we sent over this same socket (see
            // exchangeSdpOverWs). data is the base64 of the JSON
            // {type,sdp}, exactly like the HTTP endpoint's "sd" field.
            this.log('answer received over signaling ws');
            const resolve = this.pendingAnswer;
            this.pendingAnswer = null;
            resolve?.(JSON.parse(atob(msg.data)) as RTCSessionDescriptionInit);
          } else {
            this.log(`signaling ws message (unhandled): ${ev.data.slice(0, 200)}`);
          }
        } catch {
          this.log(`signaling ws message (non-JSON): ${String(ev.data).slice(0, 200)}`);
        }
      };
    } catch {
      /* WebSocket constructor threw (bad URL etc.) -- ignore, best-effort */
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
    pc.onicecandidate = (ev) => {
      if (!ev.candidate) return;
      const type = ev.candidate.type as keyof typeof candidateCounts | undefined;
      if (type && type in candidateCounts) candidateCounts[type]++;
      this.localCandidates.push(ev.candidate);
      this.trySendCandidates();
    };
    const candidateSummary = () =>
      `h${candidateCounts.host}/s${candidateCounts.srflx}/r${candidateCounts.relay}`;

    // Start the signaling socket's handshake now, in parallel with ICE
    // gathering (~3s) instead of only once we're ready to send the offer.
    // Confirmed via device-side logs (SSH) that even opened alongside the
    // SDP POST, our WS handshake still took 4-5s end to end -- but Pion
    // gives up waiting for candidates after about 1s ("Failed to start
    // manager: connecting canceled by caller"), a pattern that persisted
    // even over a real Tailscale connection (ruling out Funnel/IPv6 as the
    // cause). Starting the handshake this early lets it overlap with
    // gathering + the SDP round trip instead of stacking after them, so by
    // the time we have an offer to send, the socket is far more likely to
    // already be open.
    this.openSignalingSocket(pc);

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

    // Wait for the signaling socket to actually be open before sending the
    // offer, instead of racing it. Pre-warming the connection pool didn't
    // help (device logs showed the WS still taking 7+s even after a fast
    // throwaway request to the same host -- Funnel/Tailscale most likely
    // doesn't keep idle connections around for OkHttp to reuse), so rather
    // than continuing to chase this socket's own setup latency, just
    // guarantee the ordering instead: once the socket IS open, sending
    // already-gathered candidates the instant sessionEstablished flips
    // true is a synchronous, near-zero-latency operation (see
    // trySendCandidates()) -- the device never gets a chance to give up
    // waiting because there's no gap left for it to give up during. Costs
    // a few extra seconds of total connect time on a slow network; costs
    // nothing on a fast one (socket's likely open already by this point);
    // resolves without waiting at all on platforms with no local proxy for
    // this socket to ever open on (iOS/dev), same as today.
    const wsWaitStart = Date.now();
    await this.waitForSignalingSocket(10_000);
    this.log(`waited ${Date.now() - wsWaitStart}ms for signaling ws before sending offer`);

    this.setState('connecting');
    // Strip IPv6 candidates from the offer we actually send: on a device
    // whose own network has no IPv6 route at all (confirmed via a real
    // debug-SDP capture on LTE -- our own IPv6 host/srflx candidates next
    // to a CGNAT/464XLAT-shackled IPv4 path), ICE still spends time trying
    // those pairs before ever getting to the IPv4 ones that might actually
    // work, eating into the tight budgets used elsewhere here (short local
    // gathering wait, external-network round trips). We still gather them
    // locally (harmless) -- this only narrows what we tell the remote side
    // to try.
    const offerSdp = pc.localDescription!.sdp
      .split('\r\n')
      .filter((line) => {
        if (!line.startsWith('a=candidate:')) return true;
        const addr = line.split(' ')[4];
        return !addr?.includes(':');
      })
      .join('\r\n');
    this.lastOfferSdp = offerSdp;
    const localDesc = { type: pc.localDescription!.type, sdp: offerSdp };
    // Prefer the WebSocket for the offer/answer exchange: it's the only
    // path that leaves the device able to trickle its own candidates back
    // to us (see exchangeSdpOverWs's comment). Fall back to the HTTP
    // endpoint if the socket isn't available (iOS/dev, no local proxy) or
    // if the device doesn't answer over it -- that's still exactly the
    // behaviour we had before, so the fallback can't be a regression.
    const sdpStart = Date.now();
    let answer: RTCSessionDescriptionInit;
    try {
      this.log('offer -> signaling ws ...');
      answer = await this.exchangeSdpOverWs(localDesc, 10_000);
      this.log(`ws answered in ${Date.now() - sdpStart}ms`);
    } catch (e) {
      this.log(`ws offer failed (${e instanceof Error ? e.message : e}), falling back to POST /webrtc/session`);
      answer = await this.exchangeSdp(localDesc);
      this.log(`/webrtc/session answered in ${Date.now() - sdpStart}ms`);
    }
    this.lastAnswerSdp = answer.sdp ?? null;
    await pc.setRemoteDescription(answer);
    // Only now does the device actually have a session for our candidates
    // to be associated with -- see trySendCandidates()'s comment for why
    // this gate exists at all.
    this.sessionEstablished = true;
    this.trySendCandidates();

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
      // Safety timeout: some networks never report "complete". With no
      // STUN/TURN servers configured (see DEFAULT_ICE), there's nothing left
      // to gather but the local host candidate(s), which resolve near-
      // instantly -- the 7s held over here from when this waited out a slow
      // TURN-over-TLS handshake would just be dead time now.
      setTimeout(() => {
        pc.removeEventListener('icegatheringstatechange', check);
        resolve();
      }, 2000);
    });
  }

  private async exchangeSdp(
    local: RTCSessionDescriptionInit,
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
  wheelReport(wheelY: number, wheelX = 0) {
    // The device's rpcWheelReport(wheelY, wheelX int8) requires BOTH --
    // sending wheelY alone (fire-and-forget, so no error ever surfaces)
    // silently did nothing on real hardware. wheelX defaults to 0 since
    // nothing in this app does horizontal scroll yet.
    const clamp = (v: number) => Math.max(-127, Math.min(127, v));
    this.sendHid('wheelReport', { wheelY: clamp(wheelY), wheelX: clamp(wheelX) });
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

  /** Full timestamped event log for this connection attempt, plus the
   *  offer/answer SDP -- everything needed to diagnose a failure in one
   *  paste, no remote-debugging setup required. */
  getDebugLog(): string {
    const sdp = this.getDebugSdp();
    return [
      ...this.logLines,
      '',
      '--- OFFER ---',
      sdp.offer ?? '(none)',
      '--- ANSWER ---',
      sdp.answer ?? '(none)',
    ].join('\n');
  }

  close() {
    this.log('close()');
    try {
      this.rpc?.close();
      this.pc?.close(); // also closes the other data channels created in openPeer()
      this.signalingWs?.close();
    } catch {
      /* ignore */
    }
    this.rpc = null;
    this.pc = null;
    this.signalingWs = null;
    if (this.state !== 'failed') this.setState('closed');
  }
}
