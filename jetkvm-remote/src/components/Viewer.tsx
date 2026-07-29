import { useEffect, useRef, useState } from 'react';
import { JetKvmClient, type ConnectionState, type ConnStats } from '../jetkvm/client';
import { KeyboardState, MOD, MOUSE_BTN, mouseButtonBit } from '../jetkvm/hid';
import type { SavedDevice } from '../storage/devices';

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

export function Viewer({ device, onDisconnect }: ViewerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const clientRef = useRef<JetKvmClient | null>(null);
  const kbRef = useRef(new KeyboardState());
  const buttonsRef = useRef(0); // physical-mouse button bitmask (desktop)

  // Touch-gesture state
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const down = useRef<{ x: number; y: number; moved: boolean; longPress: boolean } | null>(
    null,
  );
  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const twoFinger = useRef(false);
  const scrollAccum = useRef(0);

  const [state, setState] = useState<ConnectionState>('idle');
  const [detail, setDetail] = useState('');
  const [showKeyboard, setShowKeyboard] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [showInfo, setShowInfo] = useState(false);
  const [stats, setStats] = useState<ConnStats | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [videoSize, setVideoSize] = useState<{ w: number; h: number } | null>(null);
  const [mouseMode, setMouseMode] = useState<MouseMode>('touch');
  const [stickyMod, setStickyMod] = useState(0);
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
      clientRef.current?.keyboardReport(r.modifier, r.keys);
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
      // second finger -> two-finger gesture (scroll); cancel any pending click
      twoFinger.current = true;
      clearLongPress();
      if (down.current) down.current.moved = true;
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
      scrollAccum.current += dy;
      while (Math.abs(scrollAccum.current) >= SCROLL_STEP) {
        const dir = scrollAccum.current > 0 ? 1 : -1;
        clientRef.current?.wheelReport(dir);
        scrollAccum.current -= dir * SCROLL_STEP;
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
    clientRef.current?.wheelReport(e.deltaY > 0 ? -1 : 1);
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
    c.keyboardReport(stickyMod, [usage]);
    setTimeout(() => c.keyboardReport(0, []), 60);
    if (stickyMod) setStickyMod(0);
  };

  const toggleMod = (bit: number) =>
    setStickyMod((m) => (m & bit ? m & ~bit : m | bit));

  const busy = state !== 'connected';

  return (
    <div className="viewer">
      <div className="toolbar">
        <button onClick={onDisconnect}>← 연결 끊기</button>
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
          onClick={() => setShowSettings((v) => !v)}
          disabled={busy}
          aria-pressed={showSettings}
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
        <video ref={videoRef} className="screen" playsInline autoPlay muted />

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

      {showSettings && state === 'connected' && (
        <SettingsPanel client={clientRef.current} onClose={() => setShowSettings(false)} />
      )}
    </div>
  );
}

// Settings backed by JetKVM's own verified JSON-RPC methods — the full
// method registry was pulled straight from the firmware's jsonrpc.go, so
// nothing here is a guess about *whether* a method exists, only (in a few
// clearly-marked spots) about exact parameter shapes where the frontend
// source wasn't reachable to confirm. Deliberately left out: ATX/DC power
// control (needs the optional extension board and its exact action enum
// couldn't be confirmed), the virtual-media file browser/upload flow, and
// the per-step keyboard macro editor — all real endpoints, just too much
// bespoke UI for this pass. Ask again once you can capture their traffic
// from the real web UI and these can be added precisely.
interface BacklightSettings {
  max_brightness: number;
  dim_after: number;
  off_after: number;
}
interface JigglerConfig {
  inactivity_limit_seconds: number;
  jitter_percentage: number;
  schedule_cron_tab: string;
}
interface WakeOnLanDevice {
  name: string;
  mac_address: string;
}
interface KeyboardMacro {
  id?: string;
  name: string;
}

function SettingsPanel({
  client,
  onClose,
}: {
  client: JetKvmClient | null;
  onClose: () => void;
}) {
  const [quality, setQuality] = useState<number | null>(null);
  const [jiggler, setJiggler] = useState<boolean | null>(null);
  const [jigglerCfg, setJigglerCfg] = useState<JigglerConfig | null>(null);
  const [network, setNetwork] = useState<Record<string, unknown> | null>(null);
  const [keyboardLayout, setKeyboardLayoutState] = useState<string | null>(null);
  const [backlight, setBacklight] = useState<BacklightSettings | null>(null);
  const [edid, setEdidState] = useState<string | null>(null);
  const [deviceId, setDeviceId] = useState<string | null>(null);
  const [localVersion, setLocalVersion] = useState<string | null>(null);
  const [cloudState, setCloudState] = useState<Record<string, unknown> | null>(null);
  const [macros, setMacros] = useState<KeyboardMacro[]>([]);
  const [wolDevices, setWolDevices] = useState<WakeOnLanDevice[]>([]);
  const [updateStatus, setUpdateStatus] = useState<Record<string, unknown> | null>(null);
  const [autoUpdate, setAutoUpdateState] = useState<boolean | null>(null);
  const [usbEmulation, setUsbEmulationState] = useState<boolean | null>(null);
  const [devMode, setDevModeState] = useState<boolean | null>(null);
  const [tlsEnabled, setTlsEnabledState] = useState<boolean | null>(null);
  const [sshKey, setSshKeyState] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    if (!client) return;
    let cancelled = false;
    const c = (method: string, params?: Record<string, unknown>) =>
      client.call(method, params).catch(() => null);
    (async () => {
      try {
        const [
          q, j, jc, n, kb, bl, ed, devId, ver, cloud, mac, wol, upd, au, usb, dev, tls,
        ] = await Promise.all([
          c('getStreamQualityFactor'),
          c('getJigglerState'),
          c('getJigglerConfig'),
          c('getNetworkState'),
          c('getKeyboardLayout'),
          c('getBacklightSettings'),
          c('getEDID'),
          c('getDeviceID'),
          c('getLocalVersion'),
          c('getCloudState'),
          c('getKeyboardMacros'),
          c('getWakeOnLanDevices'),
          c('getUpdateStatus'),
          c('getAutoUpdateState'),
          c('getUsbEmulationState'),
          c('getDevModeState'),
          c('getTLSState'),
        ]);
        if (cancelled) return;
        const qNum = typeof q === 'number' ? q : (q as { factor?: number })?.factor;
        setQuality(qNum ?? 1);
        setJiggler(!!(j as { enabled?: boolean } | null)?.enabled);
        if (jc) setJigglerCfg(jc as JigglerConfig);
        setNetwork((n as Record<string, unknown>) ?? null);
        const layout =
          typeof kb === 'string' ? kb : (kb as { layout?: string } | null)?.layout;
        if (layout) setKeyboardLayoutState(layout);
        if (bl) setBacklight(bl as BacklightSettings);
        const edidStr =
          typeof ed === 'string' ? ed : (ed as { edid?: string } | null)?.edid;
        if (edidStr) setEdidState(edidStr);
        const devIdStr =
          typeof devId === 'string' ? devId : (devId as { id?: string } | null)?.id;
        if (devIdStr) setDeviceId(devIdStr);
        const verStr =
          typeof ver === 'string' ? ver : (ver as { version?: string } | null)?.version;
        if (verStr) setLocalVersion(verStr);
        if (cloud) setCloudState(cloud as Record<string, unknown>);
        if (Array.isArray(mac)) setMacros(mac as KeyboardMacro[]);
        if (Array.isArray(wol)) setWolDevices(wol as WakeOnLanDevice[]);
        if (upd) setUpdateStatus(upd as Record<string, unknown>);
        if (au) setAutoUpdateState(!!(au as { enabled?: boolean }).enabled);
        if (usb) setUsbEmulationState(!!(usb as { enabled?: boolean }).enabled);
        if (dev) setDevModeState(!!(dev as { enabled?: boolean }).enabled);
        if (tls) setTlsEnabledState(!!(tls as { enabled?: boolean }).enabled);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client]);

  // Generic "call and report" wrapper so every action shares the same
  // error/notice handling instead of repeating try/catch everywhere. Errors
  // stay on screen (no auto-hide) — they're diagnostic information, and
  // clearing them after 2.5s made it impossible to tell what actually failed.
  const run = async (label: string, method: string, params?: Record<string, unknown>) => {
    setError('');
    setNotice('');
    try {
      const result = await client?.call(method, params);
      setNotice(`✓ ${label} 완료${result != null ? ` — ${JSON.stringify(result)}` : ''}`);
    } catch (e) {
      setError(`✗ ${label} 실패 (${method}): ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const confirmRun = async (message: string, label: string, method: string, params?: Record<string, unknown>) => {
    if (!window.confirm(message)) return;
    await run(label, method, params);
  };

  const changeQuality = (v: number) => {
    setQuality(v);
    void run('화질 변경', 'setStreamQualityFactor', { factor: v });
  };
  const toggleJiggler = () => {
    const next = !jiggler;
    setJiggler(next);
    void run('지글러 설정', 'setJigglerState', { enabled: next });
  };
  const changeJigglerCfg = (patch: Partial<JigglerConfig>) => {
    if (!jigglerCfg) return;
    const next = { ...jigglerCfg, ...patch };
    setJigglerCfg(next);
    void run('지글러 세부설정', 'setJigglerConfig', { jigglerConfig: next });
  };
  const changeLayout = (layout: string) => {
    setKeyboardLayoutState(layout);
    void run('키보드 레이아웃', 'setKeyboardLayout', { layout });
  };
  const changeBacklight = (patch: Partial<BacklightSettings>) => {
    if (!backlight) return;
    const next = { ...backlight, ...patch };
    setBacklight(next);
    void run('화면 설정', 'setBacklightSettings', next);
  };
  const setRotation = (rotation: number) => void run('화면 방향', 'setDisplayRotation', { rotation });
  const saveEdid = () => void run('EDID 저장', 'setEDID', { edid });
  const renewDhcp = () => void run('DHCP 갱신', 'renewDHCPLease');
  const sendWol = (dev: WakeOnLanDevice) =>
    void run(`${dev.name} 깨우기`, 'sendWOLMagicPacket', { mac_address: dev.mac_address });
  const toggleAutoUpdate = () => {
    const next = !autoUpdate;
    setAutoUpdateState(next);
    void run('자동 업데이트', 'setAutoUpdateState', { enabled: next });
  };
  const toggleUsbEmulation = () => {
    const next = !usbEmulation;
    void confirmRun(
      next
        ? 'USB 에뮬레이션을 켤까요?'
        : '⚠ USB 에뮬레이션을 끄면 키보드/마우스 조작이 즉시 끊깁니다. 다시 켜려면 기기에 직접 접근해야 할 수 있어요. 정말 끌까요?',
      'USB 에뮬레이션',
      'setUsbEmulationState',
      { enabled: next },
    ).then(() => setUsbEmulationState(next));
  };
  const toggleDevMode = () => {
    const next = !devMode;
    setDevModeState(next);
    void run('개발자 모드', 'setDevModeState', { enabled: next });
  };
  const toggleTls = () => {
    const next = !tlsEnabled;
    setTlsEnabledState(next);
    void run('TLS 설정', 'setTLSState', { enabled: next });
  };
  const saveSshKey = () => void run('SSH 공개키 저장', 'setSSHKeyState', { sshKey });
  const doReboot = () =>
    void confirmRun('기기를 재부팅할까요? 잠시 연결이 끊깁니다.', '재부팅', 'reboot');
  const doResetConfig = () => {
    if (window.prompt('정말 초기화하려면 "초기화"를 입력하세요.') !== '초기화') return;
    void run('설정 초기화', 'resetConfig');
  };
  const doTryUpdate = () =>
    void confirmRun(
      '지금 업데이트를 시도할까요? 진행 중 전원이 끊기면 기기가 손상될 수 있어요.',
      '업데이트',
      'tryUpdate',
    );

  const mac = network?.mac_address as string | undefined;
  const hostname = network?.hostname as string | undefined;

  return (
    <div className="settings-backdrop" onClick={onClose}>
      <div className="settings-panel" onClick={(e) => e.stopPropagation()}>
        <div className="settings-header">
          <h2>⚙ 설정</h2>
          <button className="settings-close" onClick={onClose} aria-label="닫기">
            ✕
          </button>
        </div>

        {(error || notice) && (
          <p className={error ? 'settings-error' : 'settings-notice'}>
            {error || notice}
          </p>
        )}

      <details className="settings-group" open>
        <summary>일반</summary>
        <div className="info-row">
          <span className="info-label">기기 ID</span>
          <span className="info-value">{deviceId ?? '—'}</span>
        </div>
        <div className="info-row">
          <span className="info-label">펌웨어 버전</span>
          <span className="info-value">{localVersion ?? '—'}</span>
        </div>
        <div className="info-row">
          <span className="info-label">클라우드 연결</span>
          <span className="info-value">
            {cloudState?.connected ? '연결됨' : '연결 안 됨'}
          </span>
        </div>
      </details>

      <details className="settings-group" open>
        <summary>영상</summary>
        <div className="settings-section">
          <h3>화질</h3>
          <input
            type="range"
            min={0.1}
            max={1}
            step={0.1}
            value={quality ?? 1}
            onChange={(e) => changeQuality(Number(e.target.value))}
          />
          <span className="settings-value">
            {quality != null ? `${Math.round(quality * 100)}%` : '불러오는 중…'}
          </span>
        </div>
        <div className="settings-section">
          <h3>EDID (고급)</h3>
          <textarea
            className="settings-text"
            rows={2}
            value={edid ?? ''}
            onChange={(e) => setEdidState(e.target.value)}
          />
          <button onClick={saveEdid}>저장</button>
        </div>
      </details>

      <details className="settings-group" open>
        <summary>마우스</summary>
        <div className="settings-section settings-row">
          <h3>지글러</h3>
          <label className="settings-toggle">
            <input type="checkbox" checked={!!jiggler} onChange={toggleJiggler} />
            <span>화면 잠김 방지 (마우스를 미세하게 흔듦)</span>
          </label>
        </div>
        {jigglerCfg && (
          <div className="settings-section">
            <h3>지글러 세부설정</h3>
            <div className="info-row">
              <span className="info-label">비활동 기준(초)</span>
              <input
                type="number"
                className="settings-number"
                value={jigglerCfg.inactivity_limit_seconds}
                onChange={(e) =>
                  changeJigglerCfg({ inactivity_limit_seconds: Number(e.target.value) })
                }
              />
            </div>
            <div className="info-row">
              <span className="info-label">움직임 비율(%)</span>
              <input
                type="number"
                className="settings-number"
                value={jigglerCfg.jitter_percentage}
                onChange={(e) =>
                  changeJigglerCfg({ jitter_percentage: Number(e.target.value) })
                }
              />
            </div>
          </div>
        )}
      </details>

      <details className="settings-group" open>
        <summary>키보드</summary>
        <div className="settings-section">
          <h3>레이아웃</h3>
          <input
            type="text"
            className="settings-text"
            value={keyboardLayout ?? ''}
            placeholder="예: us, de, fr, ko"
            onChange={(e) => setKeyboardLayoutState(e.target.value)}
            onBlur={(e) => changeLayout(e.target.value)}
          />
        </div>
        {macros.length > 0 && (
          <div className="settings-section">
            <h3>저장된 매크로</h3>
            <ul className="settings-list">
              {macros.map((m, i) => (
                <li key={m.id ?? i}>{m.name}</li>
              ))}
            </ul>
          </div>
        )}
      </details>

      <details className="settings-group" open>
        <summary>디스플레이 (기기 앞면)</summary>
        {backlight && (
          <div className="settings-section">
            <h3>밝기 / 자동 꺼짐</h3>
            <div className="info-row">
              <span className="info-label">밝기</span>
              <input
                type="range"
                min={0}
                max={100}
                value={backlight.max_brightness}
                onChange={(e) =>
                  changeBacklight({ max_brightness: Number(e.target.value) })
                }
              />
            </div>
            <div className="info-row">
              <span className="info-label">어두워지기까지(초)</span>
              <input
                type="number"
                className="settings-number"
                value={backlight.dim_after}
                onChange={(e) => changeBacklight({ dim_after: Number(e.target.value) })}
              />
            </div>
            <div className="info-row">
              <span className="info-label">꺼지기까지(초)</span>
              <input
                type="number"
                className="settings-number"
                value={backlight.off_after}
                onChange={(e) => changeBacklight({ off_after: Number(e.target.value) })}
              />
            </div>
          </div>
        )}
        <div className="settings-section">
          <h3>화면 방향</h3>
          <div className="settings-row-buttons">
            {[0, 90, 180, 270].map((deg) => (
              <button key={deg} onClick={() => setRotation(deg)}>
                {deg}°
              </button>
            ))}
          </div>
        </div>
      </details>

      <details className="settings-group" open>
        <summary>네트워크</summary>
        <div className="info-row">
          <span className="info-label">호스트 이름</span>
          <span className="info-value">{hostname ?? '—'}</span>
        </div>
        <div className="info-row">
          <span className="info-label">MAC 주소</span>
          <span className="info-value">{mac ?? '—'}</span>
        </div>
        <button onClick={renewDhcp}>DHCP 임대 갱신</button>
      </details>

      {wolDevices.length > 0 && (
        <details className="settings-group">
          <summary>Wake-on-LAN</summary>
          {wolDevices.map((d) => (
            <div key={d.mac_address} className="settings-row">
              <span>{d.name} ({d.mac_address})</span>
              <button onClick={() => sendWol(d)}>깨우기</button>
            </div>
          ))}
        </details>
      )}

      <details className="settings-group">
        <summary>업데이트</summary>
        <div className="info-row">
          <span className="info-label">현재 버전</span>
          <span className="info-value">{localVersion ?? '—'}</span>
        </div>
        <div className="info-row">
          <span className="info-label">업데이트 가능</span>
          <span className="info-value">
            {updateStatus?.updateAvailable ? '있음' : '없음/확인 필요'}
          </span>
        </div>
        <label className="settings-toggle">
          <input type="checkbox" checked={!!autoUpdate} onChange={toggleAutoUpdate} />
          <span>자동 업데이트</span>
        </label>
        <button onClick={doTryUpdate}>지금 업데이트 확인/시도</button>
      </details>

      <details className="settings-group">
        <summary>⚠ 고급 (주의해서 사용)</summary>
        <label className="settings-toggle">
          <input type="checkbox" checked={!!usbEmulation} onChange={toggleUsbEmulation} />
          <span>USB 에뮬레이션 (끄면 키보드/마우스가 즉시 끊김)</span>
        </label>
        <label className="settings-toggle">
          <input type="checkbox" checked={!!devMode} onChange={toggleDevMode} />
          <span>개발자 모드 (SSH 접속 허용)</span>
        </label>
        <label className="settings-toggle">
          <input type="checkbox" checked={!!tlsEnabled} onChange={toggleTls} />
          <span>TLS 사용</span>
        </label>
        <div className="settings-section">
          <h3>SSH 공개키</h3>
          <textarea
            className="settings-text"
            rows={2}
            value={sshKey}
            placeholder="ssh-ed25519 AAAA..."
            onChange={(e) => setSshKeyState(e.target.value)}
          />
          <button onClick={saveSshKey}>저장</button>
        </div>
        <div className="settings-section settings-row-buttons">
          <button onClick={doReboot}>기기 재부팅</button>
          <button className="danger" onClick={doResetConfig}>
            설정 초기화 (공장 초기화)
          </button>
        </div>
      </details>
      </div>
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

function OnScreenKeyboard({
  onTap,
  onCtrlAltDel,
  stickyMod,
  onToggleMod,
}: {
  onTap: (usage: number) => void;
  onCtrlAltDel: () => void;
  stickyMod: number;
  onToggleMod: (bit: number) => void;
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
        <button onClick={() => inputRef.current?.focus()}>입력…</button>
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
      />
    </div>
  );
}
