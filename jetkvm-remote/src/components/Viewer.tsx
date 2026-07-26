import { useEffect, useRef, useState } from 'react';
import { JetKvmClient, type ConnectionState } from '../jetkvm/client';
import { KeyboardState, MOD, MOUSE_BTN, mouseButtonBit } from '../jetkvm/hid';
import type { SavedDevice } from '../storage/devices';

interface ViewerProps {
  device: SavedDevice;
  onDisconnect: () => void;
}

type MouseMode = 'touch' | 'trackpad';

// Korean labels for each connection state.
const STATE_LABELS: Record<ConnectionState, string> = {
  idle: '대기 중',
  authenticating: '인증 중',
  signaling: '연결 준비 중',
  connecting: '연결 중',
  connected: '연결됨',
  failed: '실패',
  closed: '연결 종료',
};

// Trackpad-mode cursor sensitivity (screen px -> HID relative delta).
const TRACKPAD_SENSITIVITY = 1.4;
// Movement (px) below which a press+release counts as a tap (= click).
const TAP_THRESHOLD = 8;

// Map a pointer position within the displayed <video> content box (which is
// letterboxed by object-fit: contain) to the 0..32767 absolute HID range.
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
  const buttonsRef = useRef(0); // current mouse button bitmask

  const [state, setState] = useState<ConnectionState>('idle');
  const [detail, setDetail] = useState<string>('');
  const [showKeyboard, setShowKeyboard] = useState(false);
  const [mouseMode, setMouseMode] = useState<MouseMode>('touch');
  // Sticky modifiers latched from the on-screen keyboard (bitmask).
  const [stickyMod, setStickyMod] = useState(0);

  // Trackpad-mode gesture tracking.
  const lastPos = useRef<{ x: number; y: number } | null>(null);
  const movedDist = useRef(0);

  // --- establish the connection on mount ---
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
  }, [device.host, device.password]);

  // --- physical keyboard capture (desktop / attached BT keyboard) ---
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

  // ---------- pointer input ----------
  const emitAbs = (clientX: number, clientY: number) => {
    const v = videoRef.current;
    if (!v) return;
    const pos = toAbs(v, clientX, clientY);
    if (pos) clientRef.current?.absMouseReport(pos.x, pos.y, buttonsRef.current);
  };

  const onPointerDown = (e: React.PointerEvent) => {
    (e.target as Element).setPointerCapture?.(e.pointerId);
    if (mouseMode === 'touch') {
      buttonsRef.current |= mouseButtonBit(e.button);
      emitAbs(e.clientX, e.clientY);
    } else {
      // trackpad: start tracking relative movement
      lastPos.current = { x: e.clientX, y: e.clientY };
      movedDist.current = 0;
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (mouseMode === 'touch') {
      emitAbs(e.clientX, e.clientY);
    } else if (lastPos.current) {
      const dx = e.clientX - lastPos.current.x;
      const dy = e.clientY - lastPos.current.y;
      lastPos.current = { x: e.clientX, y: e.clientY };
      movedDist.current += Math.abs(dx) + Math.abs(dy);
      clientRef.current?.relMouseReport(
        dx * TRACKPAD_SENSITIVITY,
        dy * TRACKPAD_SENSITIVITY,
        buttonsRef.current,
      );
    }
  };

  const onPointerUp = (e: React.PointerEvent) => {
    if (mouseMode === 'touch') {
      buttonsRef.current &= ~mouseButtonBit(e.button);
      emitAbs(e.clientX, e.clientY);
    } else {
      // trackpad: a tap (little movement) = left click
      if (lastPos.current && movedDist.current < TAP_THRESHOLD) {
        clickRel(MOUSE_BTN.LEFT);
      }
      lastPos.current = null;
    }
  };

  const onWheel = (e: React.WheelEvent) => {
    clientRef.current?.wheelReport(e.deltaY > 0 ? -1 : 1);
  };

  // Fire a relative click (used by trackpad taps + the L/R click buttons).
  const clickRel = (button: number) => {
    const c = clientRef.current;
    if (!c) return;
    c.relMouseReport(0, 0, button);
    setTimeout(() => c.relMouseReport(0, 0, 0), 60);
  };

  // ---------- keyboard helpers ----------
  const sendCtrlAltDel = () => {
    const c = clientRef.current;
    if (!c) return;
    c.keyboardReport(MOD.LCTRL | MOD.LALT, [0x4c]); // Delete
    setTimeout(() => c.keyboardReport(0, []), 80);
  };

  // Tap a key, applying (and then clearing) any latched sticky modifiers so a
  // touch user can do combos like Ctrl+C.
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
          title="마우스 입력 방식 전환"
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
      </div>

      <div
        className="stage"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onWheel={onWheel}
        onContextMenu={(e) => e.preventDefault()}
      >
        <video ref={videoRef} className="screen" playsInline autoPlay muted />
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

      {/* Trackpad-mode click bar: drag to move the cursor, then click here. */}
      {mouseMode === 'trackpad' && !busy && (
        <div className="click-bar">
          <span className="hint">드래그로 커서 이동 · 탭 = 좌클릭</span>
          <button onClick={() => clickRel(MOUSE_BTN.LEFT)}>좌클릭</button>
          <button onClick={() => clickRel(MOUSE_BTN.MIDDLE)}>가운데</button>
          <button onClick={() => clickRel(MOUSE_BTN.RIGHT)}>우클릭</button>
        </div>
      )}

      {showKeyboard && (
        <OnScreenKeyboard
          onTap={tapKey}
          onCtrlAltDel={sendCtrlAltDel}
          stickyMod={stickyMod}
          onToggleMod={toggleMod}
        />
      )}
    </div>
  );
}

// On-screen keyboard for touch devices: sticky modifier keys (for combos),
// F-keys, and navigation keys. Regular typing comes from the system soft
// keyboard via the hidden input, which the global keydown/keyup handler
// forwards as HID reports.
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
