import { useEffect, useRef, useState } from 'react';
import { JetKvmClient, type ConnectionState, type ConnStats } from '../jetkvm/client';
import { charToKey, KEY_CODES, KeyboardState, MOD, MOUSE_BTN, mouseButtonBit } from '../jetkvm/hid';
import { translateSettingsPage } from '../jetkvm/settingsTranslations';
import type { SavedDevice } from '../storage/devices';

// Windows' IME toggle keys (한/영, 한자) very often fire keydown without a
// matching keyup -- Windows' own input-method layer swallows the release
// before the browser sees it. Tracking them as sustained "held" state (like
// every other key) meant the first press worked, then the usage stayed
// stuck in KeyboardState.pressed forever with no keyup ever able to clear
// it, so every following key report kept resending it and the key itself
// never fired down again. Sent as an immediate tap instead, ignoring
// whatever keyup does or doesn't show up.
const IME_TOGGLE_CODES = new Set(['Lang1', 'Lang2']);

// Open the device's own settings page instead of re-implementing every
// settings screen ourselves — the real web UI already has every dropdown/
// toggle exactly right (and its own login page, so cookie auth "just
// works" the same way it does in a normal browser tab, no CORS/Fetch-spec
// workarounds needed). Picks whatever the platform has for "open a real
// browser": CapacitorHttp on Android/iOS, a proper external browser on
// Electron via IPC, plain window.open in dev.
async function openDeviceSettings(host: string) {
  const url = `${JetKvmClient.normalizeBase(host)}/settings`;
  try {
    const capacitor = await import('@capacitor/core').catch(() => null);
    if (capacitor?.Capacitor.isNativePlatform()) {
      const { Browser } = await import('@capacitor/browser');
      await Browser.open({ url });
      return;
    }
  } catch {
    /* not on a Capacitor platform — fall through */
  }
  if (window.jetkvmIpc?.openExternal) {
    void window.jetkvmIpc.openExternal(url);
    return;
  }
  window.open(url, '_blank');
}

// Android counterpart of window.jetkvmIpc.setProxyTarget: points
// JetKvmProxyServer.java (android/.../JetKvmProxyServer.java) at this
// device, the same "which device is the settings iframe for" plumbing
// Electron's local proxy needs. No-op on iOS/Electron/dev (no such native
// plugin registered there -- registerPlugin() calls just go unanswered).
async function setAndroidProxyTarget(host: string, publicIp?: string) {
  try {
    const capacitor = await import('@capacitor/core').catch(() => null);
    if (!capacitor?.Capacitor.isNativePlatform() || capacitor.Capacitor.getPlatform() !== 'android') {
      return;
    }
    const SettingsProxy = capacitor.registerPlugin<{
      setProxyTarget(opts: { base: string; publicIp?: string }): Promise<{ port: number }>;
    }>('SettingsProxy');
    await SettingsProxy.setProxyTarget({ base: JetKvmClient.normalizeBase(host), publicIp });
  } catch {
    /* not on Android / plugin unavailable -- fine, external-browser fallback still works */
  }
}

// iOS (and plain-browser dev) have none of the local-proxy tricks
// SettingsFrame relies on (cookie reuse, framing-header stripping, SDP
// video/audio rejection) — those all depend on a same-origin reverse proxy,
// which only Electron (main.cjs) and Android (JetKvmProxyServer.java) have.
// So there, skip the iframe modal entirely (it would just show the same
// blocked/blank page the very first iframe attempt did) and go straight to
// the one thing confirmed to work everywhere: opening the real settings
// page in the system/in-app browser. Capacitor's in-app browser reports
// when the user closes it, so we can reconnect our own video right then
// instead of leaving it disconnected; a plain browser tab has no such
// signal, so that path just reconnects immediately as a best effort.
async function openSettingsMobile(host: string, onDone: () => void) {
  try {
    const capacitor = await import('@capacitor/core').catch(() => null);
    if (capacitor?.Capacitor.isNativePlatform()) {
      const { Browser } = await import('@capacitor/browser');
      const sub = await Browser.addListener('browserFinished', () => {
        onDone();
        void sub.remove();
      });
      await Browser.open({ url: `${JetKvmClient.normalizeBase(host)}/settings` });
      return;
    }
  } catch {
    /* fall through */
  }
  await openDeviceSettings(host);
  onDone();
}

interface ViewerProps {
  device: SavedDevice;
  onDisconnect: () => void;
}

// 'touch' = the cursor jumps to where you touch (absolute).
// 'trackpad' = drag to nudge the cursor like a laptop touchpad (relative).
type MouseMode = 'touch' | 'trackpad';

// The on-screen keyboard exists because a phone has no other way to type.
// A desktop already has a real keyboard, which this app forwards
// (including 한/영 and the F-keys), so the button is only clutter there --
// and tapping it gave up screen space for something with nothing to do.
// Keyed off touch support rather than "is this Electron", so a Windows
// touchscreen or a tablet still gets it and an ordinary desktop doesn't.
const HAS_TOUCH = typeof navigator !== 'undefined' && navigator.maxTouchPoints > 0;

const STATE_LABELS: Record<ConnectionState, string> = {
  idle: '대기 중',
  authenticating: '인증 중',
  signaling: '연결 준비 중',
  connecting: '연결 중',
  connected: '연결됨',
  failed: '실패',
  closed: '연결 종료',
};

const CANDIDATE_LABELS: Record<string, string> = {
  host: '직접 연결',
  srflx: '공인 IP 경유',
  prflx: '피어 경유',
  relay: '중계(relay) 서버 경유',
};

// Gesture tuning
const LONG_PRESS_MS = 500; // hold this long -> right click
const MOVE_THRESHOLD = 10; // px before a touch counts as a drag (not a tap)
// Tap, lift, then press again within this window -> the second press holds
// the left button down, so moving from there drags. Same gesture a laptop
// trackpad and a phone both use; see onPointerDown.
//
// 300ms (Android's own double-tap timeout) turned out to be too tight to
// hit reliably in practice. 500ms matches what Windows uses for
// double-click, so it's the interval people already have a feel for. Not
// longer than that: this window is also how long an ordinary tap keeps
// arming a drag, so stretching it makes an unrelated cursor move a moment
// later come out as an accidental drag instead.
const DOUBLE_TAP_MS = 500;
const CLICK_RELEASE_MS = 50; // press->release gap for a synthesized click
const MOD_HOLD_MS = 350; // press this long on Ctrl/Shift/Alt/Win -> stays held for a combo
// 데스크톱: 기본 배율(1.0), 모바일: 2.2
const TRACKPAD_SENSITIVITY = HAS_TOUCH ? 2.2 : 1.0;
const SCROLL_STEP = 24; // px of two-finger travel per wheel tick
// Wheel events are a different unit from finger travel: one notch of a real
// mouse reports deltaY ~100, so reusing SCROLL_STEP's 24 fired four ticks
// per notch and scrolled about four times too far. Matching a notch to a
// tick puts it back at the speed the same mouse has locally. A precision
// trackpad sends many small deltas instead, which still accumulate here --
// just at their own natural rate rather than four times it.
const WHEEL_STEP = 100;
// deltaY isn't always in pixels (deltaMode 1 = lines, 2 = pages), and a
// browser reporting 3 lines per notch would otherwise take ~33 notches to
// reach one tick. Rough px equivalents, only needed to get the scale right.
const WHEEL_LINE_PX = 33;
const WHEEL_PAGE_PX = 400;

function toAbs(
  video: HTMLVideoElement,
  clientX: number,
  clientY: number,
): { x: number; y: number } | null {
  const rect = video.getBoundingClientRect();
  const vw = video.videoWidth || rect.width;
  const vh = video.videoHeight || rect.height;
  if (!vw || !vh) return null;

  // The <video> element is sized to the stream's own aspect ratio (see
  // .screen in styles.css), so its box IS the picture -- these offsets
  // come out 0 in practice. Kept as the general form anyway: it stays
  // correct if the element ever ends up a different shape than the stream
  // (a frame arriving before the aspect ratio updates, say), where
  // assuming zero would silently skew every tap.
  const scale = Math.min(rect.width / vw, rect.height / vh);
  const dispW = vw * scale;
  const dispH = vh * scale;
  const offX = (rect.width - dispW) / 2;
  const offY = (rect.height - dispH) / 2;

  const px = clientX - rect.left - offX;
  const py = clientY - rect.top - offY;
  if (px < 0 || py < 0 || px > dispW || py > dispH) return null;

  return { x: (px / dispW) * 32767, y: (py / dispH) * 32767 };
}

function distance(a: { x: number; y: number }, b: { x: number; y: number }): number {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

const MIN_ZOOM = 1;
const MAX_ZOOM = 3;
const PINCH_DECIDE_PX = 12; // finger-distance change before we commit to pinch vs scroll

// Remembers which IME mode this app last set the remote machine's own
// keyboard to, per device -- see OnScreenKeyboard's langMode comment for
// why this exists at all (no way to actually ask the remote what it's
// currently set to).
function langModeKey(deviceId: string) {
  return `jetkvm.langMode.${deviceId}`;
}
function loadLangMode(deviceId: string): 'en' | 'ko' {
  return localStorage.getItem(langModeKey(deviceId)) === 'ko' ? 'ko' : 'en';
}
function saveLangMode(deviceId: string, mode: 'en' | 'ko') {
  try {
    localStorage.setItem(langModeKey(deviceId), mode);
  } catch {
    /* storage full/unavailable -- just means it won't be remembered next time */
  }
}

// Keeps panning from dragging the zoomed video past its own edge -- beyond
// zoom's overflow ((zoom-1) * half the stage size in each axis, since
// transform-origin is center) there'd be empty space showing instead of
// video.
function clampPan(
  p: { x: number; y: number },
  zoom: number,
  stage: HTMLDivElement | null,
): { x: number; y: number } {
  if (!stage || zoom <= 1) return { x: 0, y: 0 };
  const maxX = ((zoom - 1) * stage.clientWidth) / 2;
  const maxY = ((zoom - 1) * stage.clientHeight) / 2;
  return {
    x: Math.min(maxX, Math.max(-maxX, p.x)),
    y: Math.min(maxY, Math.max(-maxY, p.y)),
  };
}

export function Viewer({ device, onDisconnect }: ViewerProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const clientRef = useRef<JetKvmClient | null>(null);
  const lastFailureLogRef = useRef<string | null>(null);
  const lastFailureSdpRef = useRef<{ offer: string | null; answer: string | null } | null>(null);
  const kbRef = useRef(new KeyboardState());
  const buttonsRef = useRef(0); // physical-mouse button bitmask (desktop)

  // Touch-gesture state
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  // Pinch-to-zoom: a purely local view zoom (CSS transform on the video),
  // doesn't touch the remote machine at all -- distinct from the two-finger
  // scroll gesture below, which does. "gesture" stays null until the second
  // finger has moved enough to tell pinch (fingers changing distance) apart
  // from a parallel two-finger drag (scroll), then locks in for the rest of
  // that touch.
  const pinchStart = useRef<{ dist: number; zoom: number } | null>(null);
  // Once zoomed in, a two-finger drag pans instead of remote-scrolling --
  // scrolling the remote screen isn't very useful once you're zoomed in to
  // see detail, and panning needs some two-finger gesture to claim since a
  // single finger is already the click/cursor-move gesture.
  const pinchGesture = useRef<'pinch' | 'scroll' | 'pan' | null>(null);
  // Vertical distance the fingers have covered since the two-finger gesture
  // began -- what decides pan/scroll vs pinch. Has to be cumulative: a
  // single frame's delta is only a few px and never reaches the threshold
  // at any normal speed.
  const twoFingerTravel = useRef(0);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const stageRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setPan((p) => clampPan(p, zoom, stageRef.current));
  }, [zoom]);

  const down = useRef<{
    x: number;
    y: number;
    moved: boolean;
    longPress: boolean;
    /** This press followed a tap closely enough to be a drag (see
     *  DOUBLE_TAP_MS) -- the left button is already held for its duration. */
    dragging: boolean;
  } | null>(null);
  // When the last clean tap finished, so the next press can tell whether
  // it's the second half of a tap-then-drag. 0 = no recent tap.
  const lastTapEnd = useRef(0);
  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const twoFinger = useRef(false);
  const scrollAccum = useRef(0);
  const wheelAccum = useRef(0);

  const [state, setState] = useState<ConnectionState>('idle');
  const [detail, setDetail] = useState('');
  const [showKeyboard, setShowKeyboard] = useState(false);
  const [showInfo, setShowInfo] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  // Windows key / Alt+Tab / Alt+F4 etc. are normally swallowed by the local
  // OS before the browser ever sees them, even with our own preventDefault
  // -- the Keyboard Lock API is the actual browser mechanism for a remote-
  // desktop-style page to claim those combos back, but it only works while
  // the page is in fullscreen (Chromium/Electron only; not implemented by
  // Android's WebView or Firefox/Safari, so this is a no-op there). Ctrl+Alt+
  // Del still can't be captured by any web page -- that's why the toolbar
  // button above sends it as a synthetic HID report instead.
  useEffect(() => {
    const onChange = () => {
      const fs = document.fullscreenElement != null;
      setIsFullscreen(fs);
      // Keyboard Lock is a Chromium-only, still-experimental API with no
      // TypeScript DOM lib types yet.
      const kb = (navigator as { keyboard?: { lock?: (keys?: string[]) => Promise<void>; unlock?: () => void } })
        .keyboard;
      if (fs) {
        void kb?.lock?.().catch(() => {});
      } else {
        kb?.unlock?.();
      }
    };
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  const toggleFullscreen = () => {
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    } else {
      void rootRef.current?.requestFullscreen().catch(() => {});
    }
  };

  const [showSettings, setShowSettings] = useState(false);
  // Android has its own same-origin settings proxy (JetKvmProxyServer.java),
  // unlike iOS -- detected once up front so the ⚙ 설정 click handler and
  // SettingsFrame below can both stay synchronous.
  const [isAndroid, setIsAndroid] = useState(false);
  useEffect(() => {
    void (async () => {
      const capacitor = await import('@capacitor/core').catch(() => null);
      if (capacitor?.Capacitor.isNativePlatform() && capacitor.Capacitor.getPlatform() === 'android') {
        setIsAndroid(true);
      }
    })();
  }, []);
  const [stats, setStats] = useState<ConnStats | null>(null);
  const [videoSize, setVideoSize] = useState<{ w: number; h: number } | null>(null);
  const [mouseMode] = useState<MouseMode>('touch');
  const [stickyMod, setStickyMod] = useState(0);
  // Mirrors stickyMod for the physical-keyboard effect below, which needs
  // the current value without re-subscribing its window listeners every
  // time it changes (see that effect's comment).
  const stickyModRef = useRef(0);
  useEffect(() => {
    stickyModRef.current = stickyMod;
  }, [stickyMod]);
  const [reconnectKey, setReconnectKey] = useState(0);
  const bumpReconnect = () => setReconnectKey((k) => k + 1);
  // How many times the failure effect below has auto-retried since the last
  // successful connection (or manual reconnect) -- capped at 1, so a
  // permanently-broken network (e.g. LTE without a usable path) fails fast
  // with a visible error on the second attempt instead of looping forever.
  const autoRetryCountRef = useRef(0);
  const reconnect = () => {
    autoRetryCountRef.current = 0;
    bumpReconnect();
  };
  // Set once the first connect attempt has fired -- distinguishes "initial
  // mount" from "reconnect" below.
  const hasConnectedBefore = useRef(false);

  // --- connect on mount (and whenever a manual reconnect is triggered) ---
  useEffect(() => {
    const client = new JetKvmClient({
      onState: (s, d) => {
        setState(s);
        if (d) setDetail(d);
        // Snapshot debug data at the moment of failure, not read live off
        // clientRef later -- auto-retry (below) replaces clientRef.current
        // with a fresh client 2s after a failure, so by the time someone
        // taps the debug buttons the "live" client may already be a brand
        // new attempt with an empty log and no SDP yet.
        if (s === 'failed') {
          lastFailureLogRef.current = client.getDebugLog();
          lastFailureSdpRef.current = client.getDebugSdp();
        }
      },
      onStream: (stream) => {
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          void videoRef.current.play().catch(() => {});
        }
      },
      onError: (e) => setDetail(e.message),
    });
    clientRef.current = client;
    let cancelled = false;
    const start = async () => {
      // close() below only tears down our own RTCPeerConnection locally --
      // it never tells the device the old session is gone (no such API),
      // so the device briefly still has its hardware encoder/ICE agent
      // bound to the session we just abandoned. Sending the new offer
      // immediately can land in that window and silently never bind
      // (confirmed via a debug-SDP capture: same host/srflx candidates as
      // a working connection, but this one just sat there). Give the
      // device's own session teardown a moment to happen first.
      if (hasConnectedBefore.current) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      hasConnectedBefore.current = true;
      if (cancelled) return;
      await client.connect({
        host: device.host,
        password: device.password,
        publicIp: device.publicIp,
      });
    };
    void start();
    // Point the local settings-iframe proxy at this device (Electron/Android
    // only -- both a no-op on other platforms). publicIp travels along so
    // the settings page's own separate WebRTC connection gets the same
    // fix as the main one (see client.ts's ConnectOptions.publicIp).
    void window.jetkvmIpc?.setProxyTarget(JetKvmClient.normalizeBase(device.host), device.publicIp);
    void setAndroidProxyTarget(device.host, device.publicIp);
    return () => {
      cancelled = true;
      client.close();
    };
  }, [device.host, device.password, reconnectKey]);

  // Auto-retry on failure: bumping reconnectKey re-runs the effect above
  // from scratch -- new JetKvmClient, new JetKvmTransport (so a fresh
  // cookie jar), authenticate() then openPeer() again -- the same "leave
  // and come back in" cycle as the manual 재연결 button, not just a raw
  // ICE retry. A failure here is usually a transient one-off (a dropped
  // CapacitorHttp bridge response, a stale session on the device, one bad
  // ICE round), so retrying the same way a user would is worth doing
  // automatically instead of leaving them stuck on the failed screen.
  // Wrong password is the one failure retrying can't fix, so skip it.
  // Capped at one auto-retry (autoRetryCountRef) -- a second consecutive
  // failure surfaces the error screen instead of looping forever, since at
  // that point it's more likely a real, non-transient problem (e.g. no
  // usable network path on LTE) than a one-off glitch.
  useEffect(() => {
    if (state !== 'failed' || /password/i.test(detail)) return;
    // 다른 사람이 이미 연결했거나 피어 연결 실패 시 자동 재연결 하지 않음
    if (/occupied|already|다른|연결됨|peer connection failed/i.test(detail)) return;
    if (autoRetryCountRef.current >= 1) return;
    autoRetryCountRef.current += 1;
    const timer = setTimeout(bumpReconnect, 2000);
    return () => clearTimeout(timer);
  }, [state, detail]);

  // A later successful connection resets the auto-retry budget, so a
  // failure after a long healthy session still gets its one automatic
  // retry rather than staying capped from something that happened earlier.
  useEffect(() => {
    if (state === 'connected') autoRetryCountRef.current = 0;
  }, [state]);

  // --- stats polling: connection-quality dot always while connected, plus
  // the full info panel (and video resolution) while it's open ---
  useEffect(() => {
    // Runs continuously once connected (for the quality dot below), and
    // also while still connecting/signaling if the info panel is open --
    // useful for seeing what ICE is doing during a stuck connection instead
    // of the panel just staying empty until it's too late.
    const wantStats =
      state === 'connected' || (showInfo && (state === 'connecting' || state === 'signaling'));
    if (!wantStats) return;
    const tick = () => {
      void clientRef.current?.getStats().then((s) => s && setStats(s));
      const v = videoRef.current;
      if (v?.videoWidth) setVideoSize({ w: v.videoWidth, h: v.videoHeight });
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [showInfo, state]);

  // Small always-on health indicator, separate from the opt-in info panel --
  // invisible when things are fine, so it's only ever noise when there's
  // actually something to notice. RTT is the most direct "지연" signal
  // available cheaply every second; thresholds are a starting guess, not
  // measured against a real bad connection yet.
  const connQuality: 'ok' | 'warn' | 'bad' =
    state !== 'connected' || stats?.rttMs == null
      ? 'ok'
      : stats.rttMs > 400
        ? 'bad'
        : stats.rttMs > 150
          ? 'warn'
          : 'ok';

  // --- physical keyboard (desktop / BT keyboard) ---
  useEffect(() => {
    const kb = kbRef.current;
    const send = () => {
      const r = kb.report();
      // Combine with any modifier held via the on-screen Ctrl/Shift/Alt/Win
      // buttons, so e.g. tapping 윈 on-screen then pressing R on a real
      // keyboard actually sends Win+R instead of just R.
      clientRef.current?.keyboardReport(r.modifier | stickyModRef.current, r.keys);
    };
    const onDown = (e: KeyboardEvent) => {
      if (IME_TOGGLE_CODES.has(e.code)) {
        e.preventDefault();
        const usage = KEY_CODES[e.code];
        const r = kb.report();
        clientRef.current?.keyboardReport(r.modifier | stickyModRef.current, [...r.keys, usage]);
        setTimeout(send, 60); // release -- back to whatever's actually still held
        return;
      }
      if (kb.down(e.code)) {
        e.preventDefault();
        send();
      }
    };
    const onUp = (e: KeyboardEvent) => {
      if (IME_TOGGLE_CODES.has(e.code)) {
        e.preventDefault();
        return; // already handled as a tap on keydown above
      }
      if (kb.up(e.code)) {
        e.preventDefault();
        send();
      }
    };
    window.addEventListener('keydown', onDown);
    window.addEventListener('keyup', onUp);
    return () => {
      window.removeEventListener('keydown', onDown);
      window.removeEventListener('keyup', onUp);
    };
  }, []);

  // ---------- mouse helpers ----------
  const emitAbs = (x: number, y: number, buttons: number) => {
    const v = videoRef.current;
    if (!v) return;
    const pos = toAbs(v, x, y);
    if (pos) clientRef.current?.absMouseReport(pos.x, pos.y, buttons);
  };

  // Synthesize a click (press + release) at a screen point, honoring the mode.
  const clickAt = (x: number, y: number, button: number) => {
    const c = clientRef.current;
    if (!c) return;
    if (mouseMode === 'touch') {
      const v = videoRef.current;
      if (!v) return;
      const pos = toAbs(v, x, y);
      if (!pos) return;
      c.absMouseReport(pos.x, pos.y, button);
      setTimeout(() => c.absMouseReport(pos.x, pos.y, 0), CLICK_RELEASE_MS);
    } else {
      c.relMouseReport(0, 0, button);
      setTimeout(() => c.relMouseReport(0, 0, 0), CLICK_RELEASE_MS);
    }
  };

  const clearLongPress = () => {
    if (longPressTimer.current) {
      clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
  };

  // ---------- pointer handling ----------
  // Real mouse (desktop): native press/drag/release with the actual buttons.
  // Touch/pen: gesture model (tap = left, long-press = right, drag = move,
  // two-finger = scroll) so nothing needs to cover the screen.
  const onPointerDown = (e: React.PointerEvent) => {
    (e.target as Element).setPointerCapture?.(e.pointerId);
    if (e.pointerType === 'mouse') {
      buttonsRef.current |= mouseButtonBit(e.button);
      emitAbs(e.clientX, e.clientY, buttonsRef.current);
      return;
    }

    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size === 1) {
      // Second half of a tap-then-drag? Then this press holds the left
      // button for as long as the finger stays down, so moving from here
      // drags rather than just repositioning the cursor. Pressing right
      // now (rather than waiting for MOVE_THRESHOLD) means the drag starts
      // at the point actually touched, which is what matters when grabbing
      // a title bar or the start of a text selection. If the finger lifts
      // without moving, this press+release is simply the second click of a
      // double-click -- the same thing two quick taps always produced.
      const dragging = Date.now() - lastTapEnd.current < DOUBLE_TAP_MS;
      down.current = { x: e.clientX, y: e.clientY, moved: false, longPress: false, dragging };
      clearLongPress();
      if (dragging) {
        if (mouseMode === 'touch') emitAbs(e.clientX, e.clientY, MOUSE_BTN.LEFT);
        else clientRef.current?.relMouseReport(0, 0, MOUSE_BTN.LEFT);
      } else {
        if (mouseMode === 'touch') emitAbs(e.clientX, e.clientY, 0);
        // Holding still during a drag must not turn into a right-click, so
        // this timer only runs for an ordinary press.
        longPressTimer.current = setTimeout(() => {
          if (down.current && !down.current.moved && pointers.current.size === 1) {
            down.current.longPress = true;
            clickAt(down.current.x, down.current.y, MOUSE_BTN.RIGHT);
          }
        }, LONG_PRESS_MS);
      }
    } else {
      // second finger -> two-finger gesture (scroll or pinch-zoom, decided
      // once the fingers have moved enough); cancel any pending click
      twoFinger.current = true;
      clearLongPress();
      // Putting a second finger down right after a tap would otherwise
      // scroll with the left button still held from the tap-then-drag press
      // (see above), dragging a selection across whatever it scrolled past.
      // The gesture is clearly a scroll/pinch, not a drag -- let the button go.
      if (down.current?.dragging) {
        if (mouseMode === 'touch') emitAbs(e.clientX, e.clientY, 0);
        else clientRef.current?.relMouseReport(0, 0, 0);
        down.current.dragging = false;
      }
      if (down.current) down.current.moved = true;
      if (pointers.current.size === 2) {
        const pts = [...pointers.current.values()];
        pinchStart.current = { dist: distance(pts[0], pts[1]), zoom };
        twoFingerTravel.current = 0;
        pinchGesture.current = null;
      }
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (e.pointerType === 'mouse') {
      emitAbs(e.clientX, e.clientY, buttonsRef.current);
      return;
    }

    const prev = pointers.current.get(e.pointerId);
    const dx = prev ? e.clientX - prev.x : 0;
    const dy = prev ? e.clientY - prev.y : 0;
    if (prev) {
      prev.x = e.clientX;
      prev.y = e.clientY;
    }

    if (twoFinger.current && pointers.current.size >= 2) {
      const pts = [...pointers.current.values()];
      const curDist = distance(pts[0], pts[1]);
      const start = pinchStart.current;

      twoFingerTravel.current += dy;

      // One finger always drives the remote cursor, at any zoom -- so
      // moving the zoomed-in view is two fingers' job. (A single finger
      // used to pan once zoomed, which left no way to use the mouse at all
      // while zoomed in.) Unzoomed there's nothing to pan, so a two-finger
      // drag means scroll instead.
      //
      // Both tests have to measure travel since the gesture started. This
      // compared the *per-event* dy -- typically 2-5px between two frames --
      // against a 12px threshold, so it only ever fired if you flicked hard
      // enough to jump 12px in a single frame. Panning or scrolling at any
      // normal speed never got classified at all, which is what "두 손가락
      // 인식을 안 하는지 확대하고 안 움직여" was.
      if (pinchGesture.current === null && start) {
        if (Math.abs(curDist - start.dist) > PINCH_DECIDE_PX) {
          pinchGesture.current = 'pinch';
        } else if (Math.abs(twoFingerTravel.current) > PINCH_DECIDE_PX) {
          pinchGesture.current = start.zoom > 1 ? 'pan' : 'scroll';
        }
      }

      if (pinchGesture.current === 'pinch' && start) {
        setZoom(
          Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, (start.zoom * curDist) / start.dist)),
        );
        return;
      }
      if (pinchGesture.current === 'pan') {
        setPan((p) => clampPan({ x: p.x + dx, y: p.y + dy }, zoom, stageRef.current));
        return;
      }
      if (pinchGesture.current === 'scroll') {
        scrollAccum.current += dy;
        while (Math.abs(scrollAccum.current) >= SCROLL_STEP) {
          const dir = scrollAccum.current > 0 ? 1 : -1;
          clientRef.current?.wheelReport(dir);
          scrollAccum.current -= dir * SCROLL_STEP;
        }
      }
      return;
    }

    if (down.current) {
      const totalDx = e.clientX - down.current.x;
      const totalDy = e.clientY - down.current.y;
      if (Math.abs(totalDx) + Math.abs(totalDy) > MOVE_THRESHOLD) {
        down.current.moved = true;
        clearLongPress();
      }
    }

    // An ordinary drag just moves the cursor with nothing pressed. Only a
    // tap-then-drag (see onPointerDown) holds the button, and it pressed on
    // touchdown rather than here. Holding the button for *every* drag --
    // which this briefly did -- made plain cursor movement select text and
    // drag things by accident, since a 10px wobble was enough to trigger it.
    const dragging = down.current?.dragging ?? false;
    if (mouseMode === 'touch') {
      emitAbs(e.clientX, e.clientY, dragging ? MOUSE_BTN.LEFT : 0);
    } else {
      clientRef.current?.relMouseReport(
        dx * TRACKPAD_SENSITIVITY,
        dy * TRACKPAD_SENSITIVITY,
        dragging ? MOUSE_BTN.LEFT : 0,
      );
    }
  };

  const endTouch = (e: React.PointerEvent, cancelled: boolean) => {
    clearLongPress();
    const wasTwo = twoFinger.current;
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) {
      pinchStart.current = null;
      pinchGesture.current = null;
    }
    if (pointers.current.size === 0) {
      twoFinger.current = false;
      scrollAccum.current = 0;
    }
    const d = down.current;
    down.current = null;
    if (!d) return;
    // A tap-then-drag held the left button from touchdown (see
    // onPointerDown) -- release it wherever the finger actually lifted.
    // Also released on cancel, so an interrupted gesture can't leave the
    // button stuck down on the remote machine.
    if (d.dragging) {
      if (mouseMode === 'touch') emitAbs(e.clientX, e.clientY, 0);
      else clientRef.current?.relMouseReport(0, 0, 0);
      lastTapEnd.current = 0; // consumed -- don't chain another drag off it
      return;
    }
    if (cancelled || wasTwo || d.longPress || d.moved) return;
    // A clean tap -> left click. Two quick taps become an OS double-click.
    clickAt(d.x, d.y, MOUSE_BTN.LEFT);
    // ...and arm the next press as a drag, if it comes soon enough.
    lastTapEnd.current = Date.now();
  };

  const onPointerUp = (e: React.PointerEvent) => {
    if (e.pointerType === 'mouse') {
      buttonsRef.current &= ~mouseButtonBit(e.button);
      emitAbs(e.clientX, e.clientY, buttonsRef.current);
      return;
    }
    endTouch(e, false);
  };

  const onPointerCancel = (e: React.PointerEvent) => {
    if (e.pointerType === 'mouse') return;
    endTouch(e, true);
  };

  const onWheel = (e: React.WheelEvent) => {
    // A single physical wheelReport(±1) per browser wheel event under-reports
    // real scroll distance -- trackpads and some mice fire many small-deltaY
    // events per gesture, so one unit per event felt like it barely moved (or
    // didn't, if a wheel event's deltaY happened to be tiny). Accumulate real
    // travel and emit one step per SCROLL_STEP crossed, same as the two-finger
    // touch gesture above.
    const px =
      e.deltaMode === 1
        ? e.deltaY * WHEEL_LINE_PX
        : e.deltaMode === 2
          ? e.deltaY * WHEEL_PAGE_PX
          : e.deltaY;
    wheelAccum.current += px;
    while (Math.abs(wheelAccum.current) >= WHEEL_STEP) {
      const sign = wheelAccum.current > 0 ? 1 : -1;
      clientRef.current?.wheelReport(-sign);
      wheelAccum.current -= sign * WHEEL_STEP;
    }
  };

  // ---------- keyboard helpers ----------
  const sendCtrlAltDel = () => {
    const c = clientRef.current;
    if (!c) return;
    c.keyboardReport(MOD.LCTRL | MOD.LALT, [0x4c]);
    setTimeout(() => c.keyboardReport(0, []), 80);
  };

  // extraMod is for one-off modifiers a caller needs bundled with a single
  // tap (the custom keyboard's Shift toggle, see OnScreenKeyboard) without
  // going through the sticky Ctrl/Shift/Alt/Win mechanism below, which is
  // its own separate hold-for-a-combo thing.
  const tapKey = (usage: number, extraMod = 0) => {
    const c = clientRef.current;
    if (!c) return;
    const physical = kbRef.current.report();
    c.keyboardReport(extraMod | stickyMod | physical.modifier, [usage, ...physical.keys]);
    setTimeout(() => c.keyboardReport(physical.modifier, physical.keys), 60);
    if (stickyMod) setStickyMod(0);
  };

  // Ctrl/Shift/Alt/Win buttons hold/release the modifier on the remote
  // machine the instant they're pressed, instead of only tagging along on
  // the next on-screen key tap -- otherwise tapping 윈 alone did nothing at
  // all (no report is sent by a bare state change).
  //
  // A quick tap and a hold need different endings, though. Windows opens
  // the Start menu on Win's *keyup* (with nothing else pressed in between)
  // -- sending keydown here and only ever sending keyup on some later,
  // unrelated action (the next real key tap, or a second manual toggle)
  // meant a plain tap looked like it did nothing until you happened to
  // press something else, which is what read as "떠 있다가 클릭 끝나면 뜬다"
  // (shows up only once the press "ends," on whatever ends it). A tap
  // released quickly now auto-releases the modifier right after, same as
  // any other on-screen key (see tapKey) -- so Win alone opens the Start
  // menu immediately. Holding past MOD_HOLD_MS instead leaves it stuck down
  // (sticky), same as before, so it's still there to combine with the next
  // key for a real combo (Win+D, Ctrl+Shift+Esc, ...).
  const modHoldTimers = useRef(new Map<number, ReturnType<typeof setTimeout>>());
  const modHeldLong = useRef(new Set<number>());
  const sendStickyMod = (next: number) => {
    const physical = kbRef.current.report();
    clientRef.current?.keyboardReport(next | physical.modifier, physical.keys);
  };
  const onModDown = (bit: number) => {
    if (stickyMod & bit) {
      // Already sticky from a previous hold -- this tap is the manual
      // release for it, not a fresh press.
      setStickyMod((m) => {
        const next = m & ~bit;
        sendStickyMod(next);
        return next;
      });
      return;
    }
    setStickyMod((m) => {
      const next = m | bit;
      sendStickyMod(next);
      return next;
    });
    const timer = setTimeout(() => modHeldLong.current.add(bit), MOD_HOLD_MS);
    modHoldTimers.current.set(bit, timer);
  };
  const onModUp = (bit: number) => {
    const timer = modHoldTimers.current.get(bit);
    if (timer) clearTimeout(timer);
    modHoldTimers.current.delete(bit);
    if (modHeldLong.current.delete(bit)) return; // held long enough -> stays sticky
    if (stickyMod & bit) {
      setStickyMod((m) => {
        const next = m & ~bit;
        sendStickyMod(next);
        return next;
      });
    }
  };

  // The custom keyboard's 특수기호 (symbols) tab sends actual printable
  // characters rather than usage codes directly, since charToKey already
  // knows which of them need Shift (e.g. '!' -> Shift+1) -- letters and
  // everything else go through tapKey instead (see OnScreenKeyboard).
  const sendChar = (ch: string) => {
    const c = clientRef.current;
    if (!c) return;
    const mapped = charToKey(ch);
    if (!mapped) return;
    c.keyboardReport(mapped.shift ? MOD.LSHIFT : 0, [mapped.usage]);
    setTimeout(() => c.keyboardReport(0, []), 60);
  };

  const busy = state !== 'connected';

  return (
    <div className="viewer" ref={rootRef}>
      <div className="toolbar">
        <button onClick={onDisconnect}>← 연결 끊기</button>
        <button
          onClick={toggleFullscreen}
          aria-pressed={isFullscreen}
          title="전체화면 (Win키/Alt+Tab 등을 원격 PC로 보내려면 필요)"
        >
          ⛶ 전체화면
        </button>
        <span className={`status status-${state}`}>
          {STATE_LABELS[state]}
          {/* Only the failure detail is worth surfacing here -- the
              connecting-phase detail (ICE candidate counts, "후보 수집 완료
              (h6/s6/r18)", etc.) is genuinely useful for debugging a stuck
              connection but reads as noise/jargon during an ordinary
              connect. It's still in the debug log (로그 복사) if needed. */}
          {state === 'failed' && detail ? ` — ${detail}` : ''}
        </span>
        <div className="spacer" />
        <button onClick={sendCtrlAltDel} disabled={busy}>
          Ctrl+Alt+Del
        </button>
        <button onClick={() => tapKey(0x29)} disabled={busy}>
          Esc
        </button>
        {HAS_TOUCH && (
          <button
            onClick={() => setShowKeyboard((v) => !v)}
            disabled={busy}
            aria-pressed={showKeyboard}
          >
            ⌨ 키보드
          </button>
        )}
        <button
          onClick={reconnect}
          disabled={busy}
          title="재연결"
        >
          🔄 재연결
        </button>
        <button
          onClick={() => setShowInfo((v) => !v)}
          aria-pressed={showInfo}
          title="연결 정보 / 통계"
        >
          ℹ 정보
        </button>
        <button
          onClick={() => {
            // The device only has one hardware video encoder — the real
            // settings page keeps its own live preview running behind the
            // settings panel, and that displaces our connection (confirmed:
            // opening settings knocked our video offline). Close ours first
            // so it happens on our own terms instead of a surprise drop,
            // then reconnect automatically once settings closes.
            clientRef.current?.close();
            // The embedded iframe modal works on Electron and Android (both
            // have a local same-origin proxy: main.cjs / JetKvmProxyServer.java).
            // iOS has no such proxy yet, so it still falls back to opening
            // the real settings page in the external browser.
            if (window.jetkvmIpc || isAndroid) {
              setShowSettings(true);
            } else {
              void openSettingsMobile(device.host, reconnect);
            }
          }}
          title="기기 설정"
        >
          ⚙ 설정
        </button>
      </div>

      <div
        ref={stageRef}
        className="stage"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
        onWheel={onWheel}
        onContextMenu={(e) => e.preventDefault()}
      >
        <video
          ref={videoRef}
          className="screen"
          playsInline
          autoPlay
          muted
          style={{
            ...(zoom !== 1
              ? { transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }
              : null),
          }}
          onLoadedMetadata={(e) => {
            const v = e.currentTarget;
            if (v.videoWidth) setVideoSize({ w: v.videoWidth, h: v.videoHeight });
          }}
        />
        {connQuality !== 'ok' && (
          <div className={`conn-quality conn-quality-${connQuality}`}>
            <span className="conn-quality-dot" />
            {connQuality === 'bad' ? '지연 심함' : '지연 약간'}
            {stats?.rttMs != null ? ` ${stats.rttMs}ms` : ''}
          </div>
        )}
        {zoom !== 1 && (
          <button
            className="zoom-reset"
            onClick={() => {
              setZoom(1);
              setPan({ x: 0, y: 0 });
            }}
            title="확대 초기화"
          >
            {Math.round(zoom * 100)}% ↺
          </button>
        )}

        {busy && (
          <div className="overlay">
            {state !== 'failed' && <div className="spinner" />}
            <p>
              {state === 'failed'
                ? `연결 실패: ${detail}`
                : `${STATE_LABELS[state]}…`}
            </p>
            {state === 'failed' && (
              <div style={{ display: 'flex', gap: 8 }}>
                <button onClick={onDisconnect}>뒤로</button>
                <button
                  onClick={() => {
                    const sdp = lastFailureSdpRef.current ?? clientRef.current?.getDebugSdp();
                    const text = `OFFER:\n${sdp?.offer ?? '(none)'}\n\nANSWER:\n${sdp?.answer ?? '(none)'}`;
                    void navigator.clipboard
                      .writeText(text)
                      .then(() => alert('SDP를 복사했어요. 붙여넣기 해서 보내주세요.'))
                      .catch(() => alert('복사 실패 — 지원 안 되는 환경일 수 있어요.'));
                  }}
                >
                  SDP 복사 (디버그)
                </button>
                <button
                  onClick={() => {
                    const text = lastFailureLogRef.current ?? clientRef.current?.getDebugLog() ?? '(no log)';
                    void navigator.clipboard
                      .writeText(text)
                      .then(() => alert('로그를 복사했어요. 붙여넣기 해서 보내주세요.'))
                      .catch(() => alert('복사 실패 — 지원 안 되는 환경일 수 있어요.'));
                  }}
                >
                  로그 복사 (디버그)
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {showKeyboard && (
        <OnScreenKeyboard
          onTap={tapKey}
          onChar={sendChar}
          onCtrlAltDel={sendCtrlAltDel}
          stickyMod={stickyMod}
          onModDown={onModDown}
          onModUp={onModUp}
          deviceId={device.id}
        />
      )}

      {showInfo && (
        <InfoPanel
          host={device.host}
          state={state}
          stats={stats}
          videoSize={videoSize}
        />
      )}

      {showSettings && (
        <SettingsFrame
          host={device.host}
          isAndroid={isAndroid}
          onClose={() => {
            setShowSettings(false);
            reconnect(); // resume our video now that the encoder is free again
          }}
        />
      )}
    </div>
  );
}

function InfoPanel({
  host,
  state,
  stats,
  videoSize,
}: {
  host: string;
  state: ConnectionState;
  stats: ConnStats | null;
  videoSize: { w: number; h: number } | null;
}) {
  const row = (label: string, value: string) => (
    <div className="info-row">
      <span className="info-label">{label}</span>
      <span className="info-value">{value}</span>
    </div>
  );
  return (
    <div className="info-panel">
      {row('기기 주소', host)}
      {row('상태', STATE_LABELS[state])}
      {row('해상도', videoSize ? `${videoSize.w} × ${videoSize.h}` : '—')}
      {row('초당 프레임', stats?.fps != null ? `${Math.round(stats.fps)} fps` : '—')}
      {row('비트레이트', stats?.bitrateKbps != null ? `${stats.bitrateKbps} kbps` : '—')}
      {row('지연시간', stats?.rttMs != null ? `${stats.rttMs} ms` : '—')}
      {row('손실 패킷', stats?.packetsLost != null ? `${stats.packetsLost}` : '—')}
      {row(
        '연결 경로',
        stats?.candidateType ? (CANDIDATE_LABELS[stats.candidateType] ?? stats.candidateType) : '—',
      )}
    </div>
  );
}

// Shows the device's own real settings page inside our app, in an iframe,
// instead of re-implementing every settings screen ourselves (the custom
// version kept guessing wrong about JSON-RPC parameter shapes).
//
// A plain iframe pointed straight at the device is cross-origin from our
// app, and the device's authToken cookie has no SameSite attribute
// (defaults to Lax) — confirmed on a real device to get blocked as a
// third-party cookie. On Electron, electron/main.cjs runs a local reverse
// proxy (127.0.0.1:47623) that serves our own app AND forwards everything
// else to the device, so the iframe is same-origin as our app and the
// cookie behaves normally.
//
// That alone isn't enough, though: our WebRTC connection logs in via
// JetKvmTransport, which uses a manual cookie jar in the Electron *main*
// process (see electron/main.cjs's jetkvm-request handler) — completely
// separate from the *renderer's* real browser cookie store that the iframe
// actually uses. So the renderer had never logged in from the browser's own
// point of view, and the iframe landed on an unauthenticated redirect
// (looking like "the whole app" instead of settings).
//
// The fix is NOT to log in again from the renderer: JetKVM's local auth
// keeps a single global token and overwrites it on every successful login,
// invalidating whatever session was using the old one — confirmed on a real
// device, logging in from a separate browser disconnects the app's own
// video. Instead, electron/main.cjs's jetkvm-request handler hands the
// SAME token our WebRTC login already has straight to Electron's real
// cookie store (session.cookies.set) the moment it's captured, so by the
// time this component ever mounts the iframe is already "logged in" with
// zero extra requests and nothing gets invalidated.
//
// Android has the same idea via JetKvmProxyServer.java (a NanoHTTPD server
// the whole app is served from -- see capacitor.config.ts's server.url --
// instead of a separate desktop-only process), proxying to the device and
// handing it the same already-captured token via a plain document.cookie
// set (see transport.ts) rather than a second, session-invalidating login.
//
// iOS/dev have no such proxy, so they still point at the device directly
// and may hit the same cookie wall; "새 창에서 열기" is the reliable
// fallback there (confirmed working).
function SettingsFrame({
  host,
  isAndroid,
  onClose,
}: {
  host: string;
  isAndroid: boolean;
  onClose: () => void;
}) {
  const isElectron = !!window.jetkvmIpc;
  const sameOriginProxy = isElectron || isAndroid;
  const url = isElectron
    ? 'http://127.0.0.1:47623/settings'
    : isAndroid
      ? '/settings' // relative -- the whole app already runs on JetKvmProxyServer's origin
      : `${JetKvmClient.normalizeBase(host)}/settings`;
  const iframeRef = useRef<HTMLIFrameElement>(null);

  // Our video connection is already paused while settings is open (see the
  // ⚙ 설정 button), so the live preview the real settings page renders
  // behind its panel is just visual clutter now, not a resource conflict —
  // hide it. Only possible because the iframe is same-origin (via the local
  // proxy on Electron), so we can reach into its document at all; a plain
  // <style> override (rather than hiding elements one-by-one) also covers
  // any video/canvas the page creates later once its own connection opens.
  //
  // The real page's own "← Back to KVM" link is also hidden — the only way
  // out of this modal should be our own ✕/backdrop-click, not a navigation
  // inside the iframe that would leave our app on some other in-page route.
  // No stable selector for it is known, so this searches by visible text
  // instead, with a MutationObserver since the SPA may render it after the
  // initial load.
  const onIframeLoad = () => {
    if (!sameOriginProxy) return;
    try {
      const doc = iframeRef.current?.contentDocument;
      if (!doc) return;

      const style = doc.createElement('style');
      style.textContent = 'video, canvas { display: none !important; }';
      doc.head?.appendChild(style);

      const hideBackLink = () => {
        doc.querySelectorAll('a, button').forEach((el) => {
          if (el.textContent?.trim().includes('Back to KVM')) {
            (el as HTMLElement).style.display = 'none';
          }
        });
      };
      // Best-effort EN -> KO translation of the real (English) settings UI
      // -- see settingsTranslations.ts. Static dictionary, not a live
      // translator, so anything not in that list stays in English.
      const runFixups = () => {
        hideBackLink();
        translateSettingsPage(doc);
      };
      runFixups();
      if (doc.body) {
        new MutationObserver(runFixups).observe(doc.body, {
          childList: true,
          subtree: true,
        });
      }
    } catch {
      /* cross-origin (shouldn't happen via the proxy) or not ready yet */
    }
  };

  return (
    <div className="frame-backdrop" onClick={onClose}>
      <div className="frame-panel" onClick={(e) => e.stopPropagation()}>
        <div className="frame-header">
          <h2>⚙ 설정</h2>
          <div className="frame-header-actions">
            <button onClick={() => void openDeviceSettings(host)}>새 창에서 열기</button>
            <button className="frame-close" onClick={onClose} aria-label="닫기">
              ✕
            </button>
          </div>
        </div>
        <iframe
          ref={iframeRef}
          className="frame-iframe"
          src={url}
          title="JetKVM 설정"
          onLoad={onIframeLoad}
        />
      </div>
    </div>
  );
}

// 2-벌식 (2-set) Korean layout, standard on every physical Korean keyboard:
// each QWERTY letter position doubles as a jamo. What actually gets SENT
// for a letter tap is identical in either language -- the exact same
// physical usage code as the English key at that position, exactly like a
// real 2-벌식 keyboard's own firmware works. Only the label shown here
// changes; the remote machine's own Korean IME (toggled via 한/영, see
// toggleLang below) is what turns those physical taps into Hangul, the
// same way it would for a real keyboard. [unshifted, shifted] -- shift
// only changes five of these (쌍자음 ㄲㄸㅃㅆㅉ on Q/W/E/R/T, 이중모음 ㅒㅖ on
// O/P); the rest repeat the same jamo either way.
const KO_LABELS: Record<string, [string, string]> = {
  Q: ['ㅂ', 'ㅃ'], W: ['ㅈ', 'ㅉ'], E: ['ㄷ', 'ㄸ'], R: ['ㄱ', 'ㄲ'], T: ['ㅅ', 'ㅆ'],
  Y: ['ㅛ', 'ㅛ'], U: ['ㅕ', 'ㅕ'], I: ['ㅑ', 'ㅑ'], O: ['ㅐ', 'ㅒ'], P: ['ㅔ', 'ㅖ'],
  A: ['ㅁ', 'ㅁ'], S: ['ㄴ', 'ㄴ'], D: ['ㅇ', 'ㅇ'], F: ['ㄹ', 'ㄹ'], G: ['ㅎ', 'ㅎ'],
  H: ['ㅗ', 'ㅗ'], J: ['ㅓ', 'ㅓ'], K: ['ㅏ', 'ㅏ'], L: ['ㅣ', 'ㅣ'],
  Z: ['ㅋ', 'ㅋ'], X: ['ㅌ', 'ㅌ'], C: ['ㅊ', 'ㅊ'], V: ['ㅍ', 'ㅍ'],
  B: ['ㅠ', 'ㅠ'], N: ['ㅜ', 'ㅜ'], M: ['ㅡ', 'ㅡ'],
};

const QWERTY_ROW1 = ['Q', 'W', 'E', 'R', 'T', 'Y', 'U', 'I', 'O', 'P'];
const QWERTY_ROW2 = ['A', 'S', 'D', 'F', 'G', 'H', 'J', 'K', 'L'];
const QWERTY_ROW3 = ['Z', 'X', 'C', 'V', 'B', 'N', 'M'];

const SYMBOL_ROWS = [
  ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
  ['!', '@', '#', '$', '%', '^', '&', '*', '(', ')'],
  ['-', '_', '=', '+', '[', ']', '{', '}', '\\', '|'],
  [';', ':', "'", '"', ',', '.', '<', '>', '/', '?', '~', '`'],
];

const OSK_MODS = [
  ['Ctrl', MOD.LCTRL],
  ['Shift', MOD.LSHIFT],
  ['Alt', MOD.LALT],
  ['Win', MOD.LGUI],
] as const;
const OSK_FKEYS = [
  ['F1', 0x3a], ['F2', 0x3b], ['F3', 0x3c], ['F4', 0x3d],
  ['F5', 0x3e], ['F6', 0x3f], ['F7', 0x40], ['F8', 0x41],
  ['F9', 0x42], ['F10', 0x43], ['F11', 0x44], ['F12', 0x45],
] as const;
const OSK_NAV = [
  ['Tab', 0x2b], ['↑', 0x52], ['↓', 0x51], ['←', 0x50], ['→', 0x4f],
  ['Home', 0x4a], ['End', 0x4d], ['PgUp', 0x4b], ['PgDn', 0x4e],
  ['Del', 0x4c], ['Ins', 0x49],
] as const;

// Fully custom keyboard, not the phone's native one -- built after the
// native keyboard turned out to be unfixable from here on two fronts at
// once: its own size is OS chrome we can never shrink or control, and
// Android's Korean IME composition kept losing its own state against a
// hidden input we had to keep resetting (see git history for the
// composition-guard attempt that only partially worked around it). Every
// key here is an explicit tap to a known physical usage code, so neither
// problem can recur -- there's no OS keyboard to be the wrong size, and no
// IME composition to lose since typed characters never round-trip through
// a real text input at all.
function OnScreenKeyboard({
  onTap,
  onChar,
  onCtrlAltDel,
  stickyMod,
  onModDown,
  onModUp,
  deviceId,
}: {
  onTap: (usage: number, extraMod?: number) => void;
  onChar: (ch: string) => void;
  onCtrlAltDel: () => void;
  stickyMod: number;
  onModDown: (bit: number) => void;
  onModUp: (bit: number) => void;
  deviceId: string;
}) {
  const [tab, setTab] = useState<'lang' | 'symbols' | 'special'>('lang');
  // There's no way to ask the remote machine what its IME is actually set
  // to right now (no HID feedback channel -- same fundamental limit as not
  // being able to read the phone's own keyboard language). Defaulting to
  // 'en' on every fresh connect meant that if the remote had been left in
  // Korean from a previous session, this and reality started out
  // backwards, and every tap came out the opposite of what the label said
  // until 한/영 was tapped once to resync. Remembering the last mode this
  // app itself set *for this device* and starting there instead doesn't
  // fix a true first-ever mismatch (still needs one manual 한/영 tap), but
  // means that one correction sticks instead of happening on every single
  // reconnect -- correct as long as nothing else changes the remote's IME
  // in between.
  const [langMode, setLangMode] = useState<'en' | 'ko'>(() => loadLangMode(deviceId));
  const [shift, setShift] = useState(false);

  const letterLabel = (letter: string) => {
    if (langMode === 'ko') return KO_LABELS[letter][shift ? 1 : 0];
    return shift ? letter : letter.toLowerCase();
  };
  const tapLetter = (letter: string) => {
    onTap(KEY_CODES[`Key${letter}`], shift ? MOD.LSHIFT : 0);
    setShift(false); // one-shot, like a normal mobile keyboard's shift
  };
  // Switching language here also toggles it on the remote machine's own
  // IME right away (the same Lang1 tap 한/영 always sent) -- since we
  // decide when the mode changes, there's no need to ever detect the
  // phone's own keyboard language (which isn't possible from a web app
  // anyway -- no such API exists).
  const toggleLang = () => {
    setLangMode((m) => {
      const next = m === 'en' ? 'ko' : 'en';
      saveLangMode(deviceId, next);
      return next;
    });
    onTap(KEY_CODES.Lang1);
  };

  return (
    <div className="osk">
      <div className="osk-row osk-tabs">
        <button className={tab === 'lang' ? 'mod-active' : ''} onClick={() => setTab('lang')}>
          한글/영어
        </button>
        <button className={tab === 'symbols' ? 'mod-active' : ''} onClick={() => setTab('symbols')}>
          특수기호
        </button>
        <button className={tab === 'special' ? 'mod-active' : ''} onClick={() => setTab('special')}>
          특수
        </button>
      </div>

      {tab === 'lang' && (
        <>
          <div className="osk-row osk-row-fill">
            {QWERTY_ROW1.map((l) => (
              <button className="osk-key" key={l} onClick={() => tapLetter(l)}>
                {letterLabel(l)}
              </button>
            ))}
          </div>
          <div className="osk-row osk-row-fill">
            {QWERTY_ROW2.map((l) => (
              <button className="osk-key" key={l} onClick={() => tapLetter(l)}>
                {letterLabel(l)}
              </button>
            ))}
          </div>
          {/* Shift/Backspace flank the third letter row and 한/영, space,
              and Enter get their own row below -- matches where a real
              mobile keyboard puts them (see the reference screenshot),
              instead of cramming all five into one row together with the
              letters. */}
          <div className="osk-row osk-row-fill">
            <button className={shift ? 'mod-active osk-key' : 'osk-key'} onClick={() => setShift((s) => !s)}>
              ⇧
            </button>
            {QWERTY_ROW3.map((l) => (
              <button className="osk-key" key={l} onClick={() => tapLetter(l)}>
                {letterLabel(l)}
              </button>
            ))}
            <button className="osk-key" onClick={() => onTap(KEY_CODES.Backspace)}>
              ⌫
            </button>
          </div>
          <div className="osk-row osk-row-fill">
            <button className={langMode === 'ko' ? 'mod-active osk-key' : 'osk-key'} onClick={toggleLang}>
              {langMode === 'ko' ? '한글' : '영어'}
            </button>
            <button className="osk-key osk-space" onClick={() => onTap(KEY_CODES.Space)}>
              Space
            </button>
            <button className="osk-key" onClick={() => onTap(KEY_CODES.Enter)}>
              Enter
            </button>
          </div>
        </>
      )}

      {tab === 'symbols' &&
        SYMBOL_ROWS.map((row, i) => (
          <div className="osk-row osk-row-fill" key={i}>
            {row.map((ch) => (
              <button className="osk-key" key={ch} onClick={() => onChar(ch)}>
                {ch}
              </button>
            ))}
          </div>
        ))}

      {tab === 'special' && (
        <>
          <div className="osk-row osk-row-fill">
            {/* Holding one of these past MOD_HOLD_MS arms it as sticky in
                Viewer's own stickyMod state (see onModDown) -- that state
                lives one level up, outside this tab's own local state, so
                switching to 한글/영어 afterward and tapping a letter there
                still combines with whatever's held here (Ctrl+C, Win+D,
                ...) even though this row itself is no longer on screen at
                that point. */}
            {OSK_MODS.map(([label, bit]) => (
              <button
                key={label}
                className={`osk-key${stickyMod & bit ? ' mod-active' : ''}`}
                aria-pressed={!!(stickyMod & bit)}
                onPointerDown={() => onModDown(bit)}
                onPointerUp={() => onModUp(bit)}
                onPointerCancel={() => onModUp(bit)}
              >
                {label}
              </button>
            ))}
            <button className="osk-key" onClick={onCtrlAltDel}>
              Ctrl+Alt+Del
            </button>
            <button className="osk-key" onClick={() => onTap(KEY_CODES.Lang2)}>
              한자
            </button>
          </div>
          <div className="osk-row osk-row-fill">
            {OSK_FKEYS.map(([label, code]) => (
              <button className="osk-key" key={label} onClick={() => onTap(code)}>
                {label}
              </button>
            ))}
          </div>
          <div className="osk-row osk-row-fill">
            {OSK_NAV.map(([label, code]) => (
              <button className="osk-key" key={label} onClick={() => onTap(code)}>
                {label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
