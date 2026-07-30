import { useEffect, useRef, useState } from 'react';
import { JetKvmClient, type ConnectionState, type ConnStats } from '../jetkvm/client';
import { charToKey, hangulToTaps, KeyboardState, MOD, MOUSE_BTN, mouseButtonBit } from '../jetkvm/hid';
import type { SavedDevice } from '../storage/devices';

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

// Android/iOS (and plain-browser dev) have none of the local-proxy tricks
// SettingsFrame relies on (cookie reuse, framing-header stripping, SDP
// video/audio rejection) — those all depend on electron/main.cjs's
// same-origin reverse proxy, which only exists on desktop. So on those
// platforms, skip the iframe modal entirely (it would just show the same
// blocked/blank page the very first iframe attempt did) and go straight to
// the one thing already confirmed to work everywhere: opening the real
// settings page in the system/in-app browser. Capacitor's in-app browser
// reports when the user closes it, so we can reconnect our own video right
// then instead of leaving it disconnected; a plain browser tab has no such
// signal, so that path just reconnects immediately as a best effort.
// Android counterpart of window.jetkvmIpc.setProxyTarget: points
// JetKvmProxyServer.java (android/.../JetKvmProxyServer.java) at this
// device, the same "which device is the settings iframe for" plumbing
// Electron's local proxy needs. No-op on iOS/Electron/dev (no such native
// plugin registered there -- registerPlugin() calls just go unanswered).
async function setAndroidProxyTarget(host: string) {
  try {
    const capacitor = await import('@capacitor/core').catch(() => null);
    if (!capacitor?.Capacitor.isNativePlatform() || capacitor.Capacitor.getPlatform() !== 'android') {
      return;
    }
    const SettingsProxy = capacitor.registerPlugin<{
      setProxyTarget(opts: { base: string }): Promise<{ port: number }>;
    }>('SettingsProxy');
    await SettingsProxy.setProxyTarget({ base: JetKvmClient.normalizeBase(host) });
  } catch {
    /* not on Android / plugin unavailable -- fine, external-browser fallback still works */
  }
}

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
const CLICK_RELEASE_MS = 50; // press->release gap for a synthesized click
const TRACKPAD_SENSITIVITY = 1.4;
const SCROLL_STEP = 24; // px of two-finger travel per wheel tick

function toAbs(
  video: HTMLVideoElement,
  clientX: number,
  clientY: number,
): { x: number; y: number } | null {
  const rect = video.getBoundingClientRect();
  const vw = video.videoWidth || rect.width;
  const vh = video.videoHeight || rect.height;
  if (!vw || !vh) return null;

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

export function Viewer({ device, onDisconnect }: ViewerProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const clientRef = useRef<JetKvmClient | null>(null);
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
  const pinchGesture = useRef<'pinch' | 'scroll' | null>(null);
  const [zoom, setZoom] = useState(1);
  const down = useRef<{ x: number; y: number; moved: boolean; longPress: boolean } | null>(
    null,
  );
  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const twoFinger = useRef(false);
  const scrollAccum = useRef(0);
  const wheelAccum = useRef(0);

  const [state, setState] = useState<ConnectionState>('idle');
  const [detail, setDetail] = useState('');
  const [showKeyboard, setShowKeyboard] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
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
  const [mouseMode, setMouseMode] = useState<MouseMode>('touch');
  const [stickyMod, setStickyMod] = useState(0);
  // Mirrors stickyMod for the physical-keyboard effect below, which needs
  // the current value without re-subscribing its window listeners every
  // time it changes (see that effect's comment).
  const stickyModRef = useRef(0);
  useEffect(() => {
    stickyModRef.current = stickyMod;
  }, [stickyMod]);
  const [reconnectKey, setReconnectKey] = useState(0);
  const reconnect = () => setReconnectKey((k) => k + 1);

  // --- connect on mount (and whenever a manual reconnect is triggered) ---
  useEffect(() => {
    const client = new JetKvmClient({
      onState: (s, d) => {
        setState(s);
        if (d) setDetail(d);
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
    void client.connect({ host: device.host, password: device.password });
    // Point the local settings-iframe proxy at this device (Electron/Android
    // only -- both a no-op on other platforms).
    void window.jetkvmIpc?.setProxyTarget(JetKvmClient.normalizeBase(device.host));
    void setAndroidProxyTarget(device.host);
    return () => client.close();
  }, [device.host, device.password, reconnectKey]);

  // --- connection-info panel: poll stats + track video resolution while open ---
  useEffect(() => {
    if (!showInfo || state !== 'connected') return;
    const tick = () => {
      void clientRef.current?.getStats().then((s) => s && setStats(s));
      const v = videoRef.current;
      if (v?.videoWidth) setVideoSize({ w: v.videoWidth, h: v.videoHeight });
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [showInfo, state]);

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
      if (kb.down(e.code)) {
        e.preventDefault();
        send();
      }
    };
    const onUp = (e: KeyboardEvent) => {
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
      down.current = { x: e.clientX, y: e.clientY, moved: false, longPress: false };
      if (mouseMode === 'touch') emitAbs(e.clientX, e.clientY, 0);
      clearLongPress();
      longPressTimer.current = setTimeout(() => {
        if (down.current && !down.current.moved && pointers.current.size === 1) {
          down.current.longPress = true;
          clickAt(down.current.x, down.current.y, MOUSE_BTN.RIGHT);
        }
      }, LONG_PRESS_MS);
    } else {
      // second finger -> two-finger gesture (scroll or pinch-zoom, decided
      // once the fingers have moved enough); cancel any pending click
      twoFinger.current = true;
      clearLongPress();
      if (down.current) down.current.moved = true;
      if (pointers.current.size === 2) {
        const pts = [...pointers.current.values()];
        pinchStart.current = { dist: distance(pts[0], pts[1]), zoom };
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

      if (pinchGesture.current === null && start) {
        if (Math.abs(curDist - start.dist) > PINCH_DECIDE_PX) {
          pinchGesture.current = 'pinch';
        } else if (Math.abs(dy) > PINCH_DECIDE_PX) {
          pinchGesture.current = 'scroll';
        }
      }

      if (pinchGesture.current === 'pinch' && start) {
        setZoom(
          Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, (start.zoom * curDist) / start.dist)),
        );
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

    if (mouseMode === 'touch') {
      emitAbs(e.clientX, e.clientY, 0);
    } else {
      clientRef.current?.relMouseReport(
        dx * TRACKPAD_SENSITIVITY,
        dy * TRACKPAD_SENSITIVITY,
        0,
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
    if (cancelled || wasTwo || !d || d.longPress || d.moved) return;
    // A clean tap -> left click. Two quick taps become an OS double-click.
    clickAt(d.x, d.y, MOUSE_BTN.LEFT);
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
    wheelAccum.current += e.deltaY;
    while (Math.abs(wheelAccum.current) >= SCROLL_STEP) {
      const sign = wheelAccum.current > 0 ? 1 : -1;
      clientRef.current?.wheelReport(-sign);
      wheelAccum.current -= sign * SCROLL_STEP;
    }
  };

  // ---------- keyboard helpers ----------
  const sendCtrlAltDel = () => {
    const c = clientRef.current;
    if (!c) return;
    c.keyboardReport(MOD.LCTRL | MOD.LALT, [0x4c]);
    setTimeout(() => c.keyboardReport(0, []), 80);
  };

  const tapKey = (usage: number) => {
    const c = clientRef.current;
    if (!c) return;
    const physical = kbRef.current.report();
    c.keyboardReport(stickyMod | physical.modifier, [usage, ...physical.keys]);
    setTimeout(() => c.keyboardReport(physical.modifier, physical.keys), 60);
    if (stickyMod) setStickyMod(0);
  };

  // Ctrl/Shift/Alt/Win buttons actually hold/release the modifier on the
  // remote machine the instant they're toggled, instead of only tagging
  // along on the next on-screen key tap -- otherwise tapping 윈 alone did
  // nothing at all (no report is sent by a bare state change), which read
  // as "the button doesn't work" / "it's stuck" since the UI still showed
  // it highlighted with nothing to show for it on the remote screen.
  const toggleMod = (bit: number) => {
    setStickyMod((m) => {
      const next = m & bit ? m & ~bit : m | bit;
      const physical = kbRef.current.report();
      clientRef.current?.keyboardReport(next | physical.modifier, physical.keys);
      return next;
    });
  };

  // Mobile virtual keyboards don't fire the physical-key events the window
  // keydown/keyup listener above relies on — this drives typing from the
  // "입력…" hidden-input's `input` events instead, one character at a time.
  const sendChar = (ch: string) => {
    const c = clientRef.current;
    if (!c) return;

    // Hangul syllable: no single HID usage exists for it, so send the
    // 2-벌식 physical key sequence instead and let the target's own Korean
    // IME compose it back — same trick real remote-desktop tools use. The
    // target machine must have a Korean (2-벌식) layout/IME active.
    const hangulTaps = hangulToTaps(ch);
    if (hangulTaps) {
      hangulTaps.forEach((tap, i) => {
        setTimeout(() => {
          c.keyboardReport(tap.shift ? MOD.LSHIFT : 0, [tap.usage]);
          setTimeout(() => c.keyboardReport(0, []), 40);
        }, i * 70);
      });
      return;
    }

    let usage: number | undefined;
    let shift = false;
    if (ch === '\n') usage = 0x28; // Enter
    else if (ch === '\b') usage = 0x2a; // Backspace
    else if (ch === '\t') usage = 0x2b; // Tab
    else {
      const mapped = charToKey(ch);
      if (!mapped) return;
      usage = mapped.usage;
      shift = mapped.shift;
    }
    c.keyboardReport(shift ? MOD.LSHIFT : 0, [usage]);
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
          {detail ? ` — ${detail}` : ''}
        </span>
        <div className="spacer" />
        <button
          onClick={() =>
            setMouseMode((m) => (m === 'touch' ? 'trackpad' : 'touch'))
          }
          disabled={busy}
          title="터치=누른 위치로 커서 / 트랙패드=끌어서 커서 이동"
        >
          🖱 {mouseMode === 'touch' ? '터치' : '트랙패드'}
        </button>
        <button onClick={sendCtrlAltDel} disabled={busy}>
          Ctrl+Alt+Del
        </button>
        <button onClick={() => tapKey(0x29)} disabled={busy}>
          Esc
        </button>
        <button
          onClick={() => setShowKeyboard((v) => !v)}
          disabled={busy}
          aria-pressed={showKeyboard}
        >
          ⌨ 키보드
        </button>
        <button
          onClick={() => setShowHelp((v) => !v)}
          aria-pressed={showHelp}
          title="조작 도움말"
        >
          ❔
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
          style={zoom !== 1 ? { transform: `scale(${zoom})` } : undefined}
        />
        {zoom !== 1 && (
          <button className="zoom-reset" onClick={() => setZoom(1)} title="확대 초기화">
            {Math.round(zoom * 100)}% ↺
          </button>
        )}

        {showHelp && (
          <div className="help-legend" onClick={() => setShowHelp(false)}>
            <b>손가락 조작</b>
            <span>한 번 탭 = 좌클릭</span>
            <span>두 번 탭 = 더블클릭</span>
            <span>길게 누르기 = 우클릭</span>
            <span>끌기 = 커서 이동</span>
            <span>두 손가락 = 스크롤</span>
            <em>탭하면 닫힘</em>
          </div>
        )}

        {busy && (
          <div className="overlay">
            {state !== 'failed' && <div className="spinner" />}
            <p>
              {state === 'failed'
                ? `연결 실패: ${detail}`
                : `${STATE_LABELS[state]}…`}
            </p>
            {state === 'failed' && <button onClick={onDisconnect}>뒤로</button>}
          </div>
        )}
      </div>

      {showKeyboard && (
        <OnScreenKeyboard
          onTap={tapKey}
          onCtrlAltDel={sendCtrlAltDel}
          stickyMod={stickyMod}
          onToggleMod={toggleMod}
          onChar={sendChar}
        />
      )}

      {showInfo && (
        <InfoPanel
          host={device.host}
          state={state}
          stats={stats}
          videoSize={videoSize}
          onReconnect={reconnect}
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
  onReconnect,
}: {
  host: string;
  state: ConnectionState;
  stats: ConnStats | null;
  videoSize: { w: number; h: number } | null;
  onReconnect: () => void;
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
      <button className="info-reconnect" onClick={onReconnect}>
        🔄 재연결
      </button>
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
      hideBackLink();
      if (doc.body) {
        new MutationObserver(hideBackLink).observe(doc.body, {
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

function OnScreenKeyboard({
  onTap,
  onCtrlAltDel,
  stickyMod,
  onToggleMod,
  onChar,
}: {
  onTap: (usage: number) => void;
  onCtrlAltDel: () => void;
  stickyMod: number;
  onToggleMod: (bit: number) => void;
  onChar: (ch: string) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const mods = [
    ['Ctrl', MOD.LCTRL],
    ['Shift', MOD.LSHIFT],
    ['Alt', MOD.LALT],
    ['Win', MOD.LGUI],
  ] as const;
  const fkeys = [
    ['F1', 0x3a], ['F2', 0x3b], ['F3', 0x3c], ['F4', 0x3d],
    ['F5', 0x3e], ['F6', 0x3f], ['F7', 0x40], ['F8', 0x41],
    ['F9', 0x42], ['F10', 0x43], ['F11', 0x44], ['F12', 0x45],
  ] as const;
  const nav = [
    ['Tab', 0x2b], ['↑', 0x52], ['↓', 0x51], ['←', 0x50], ['→', 0x4f],
    ['Home', 0x4a], ['End', 0x4d], ['PgUp', 0x4b], ['PgDn', 0x4e],
    ['Del', 0x4c], ['Ins', 0x49],
  ] as const;

  return (
    <div className="osk">
      <div className="osk-row">
        {mods.map(([label, bit]) => (
          <button
            key={label}
            className={stickyMod & bit ? 'mod-active' : ''}
            aria-pressed={!!(stickyMod & bit)}
            onClick={() => onToggleMod(bit)}
          >
            {label}
          </button>
        ))}
        <button onClick={onCtrlAltDel}>Ctrl+Alt+Del</button>
        <button
          onClick={() => {
            inputRef.current?.focus();
            // Focusing an <input> is enough to bring up the OS keyboard on
            // Android/iOS on its own; Windows doesn't reliably do the same
            // for a touchscreen PC running an Electron app, so ask for its
            // on-screen keyboard explicitly (no-op on mac/Linux/other
            // platforms -- see electron/main.cjs).
            void window.jetkvmIpc?.showTouchKeyboard?.();
          }}
        >
          입력…
        </button>
      </div>
      <div className="osk-row">
        {fkeys.map(([label, code]) => (
          <button key={label} onClick={() => onTap(code)}>
            {label}
          </button>
        ))}
      </div>
      <div className="osk-row">
        {nav.map(([label, code]) => (
          <button key={label} onClick={() => onTap(code)}>
            {label}
          </button>
        ))}
      </div>
      <input
        ref={inputRef}
        className="osk-hidden-input"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        aria-label="keyboard capture"
        onChange={(e) => {
          // Mobile soft keyboards mostly skip keydown/keyup and only fire
          // this `input` event, so this — not the window keydown listener —
          // is what actually drives typing on Android/iOS.
          const native = e.nativeEvent as InputEvent;
          const el = e.currentTarget;
          if (native.inputType === 'deleteContentBackward') {
            onChar('\b');
          } else if (native.inputType === 'insertLineBreak') {
            onChar('\n');
          } else if (native.data) {
            for (const ch of native.data) onChar(ch);
          } else if (el.value) {
            // Some IMEs (autocomplete taps, etc.) don't set inputType/data.
            for (const ch of el.value) onChar(ch);
          }
          el.value = '';
        }}
      />
    </div>
  );
}
