import { useEffect, useRef, useState } from 'react';
import {
  JetKvmClient,
  type ConnectionState,
} from '../jetkvm/client';
import { KeyboardState, mouseButtonBit } from '../jetkvm/hid';
import type { SavedDevice } from '../storage/devices';

interface ViewerProps {
  device: SavedDevice;
  onDisconnect: () => void;
}

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

  // Account for contain-letterboxing: the actual picture may be smaller than
  // the element rect on one axis.
  const scale = Math.min(rect.width / vw, rect.height / vh);
  const dispW = vw * scale;
  const dispH = vh * scale;
  const offX = (rect.width - dispW) / 2;
  const offY = (rect.height - dispH) / 2;

  const px = clientX - rect.left - offX;
  const py = clientY - rect.top - offY;
  if (px < 0 || py < 0 || px > dispW || py > dispH) return null;

  return {
    x: (px / dispW) * 32767,
    y: (py / dispH) * 32767,
  };
}

export function Viewer({ device, onDisconnect }: ViewerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const clientRef = useRef<JetKvmClient | null>(null);
  const kbRef = useRef(new KeyboardState());
  const buttonsRef = useRef(0); // current mouse button bitmask
  const [state, setState] = useState<ConnectionState>('idle');
  const [detail, setDetail] = useState<string>('');
  const [showKeyboard, setShowKeyboard] = useState(false);

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

  // --- pointer input (mouse + touch, absolute mode) ---
  const emitMouse = (clientX: number, clientY: number) => {
    const v = videoRef.current;
    if (!v) return;
    const pos = toAbs(v, clientX, clientY);
    if (pos) clientRef.current?.absMouseReport(pos.x, pos.y, buttonsRef.current);
  };

  const onPointerDown = (e: React.PointerEvent) => {
    (e.target as Element).setPointerCapture?.(e.pointerId);
    buttonsRef.current |= mouseButtonBit(e.button);
    emitMouse(e.clientX, e.clientY);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    emitMouse(e.clientX, e.clientY);
  };
  const onPointerUp = (e: React.PointerEvent) => {
    buttonsRef.current &= ~mouseButtonBit(e.button);
    emitMouse(e.clientX, e.clientY);
  };
  const onWheel = (e: React.WheelEvent) => {
    clientRef.current?.wheelReport(e.deltaY > 0 ? -1 : 1);
  };

  // --- toolbar helpers ---
  const sendCtrlAltDel = () => {
    const c = clientRef.current;
    if (!c) return;
    // Ctrl(0x01)+Alt(0x04) modifier + Delete(0x4c)
    c.keyboardReport(0x05, [0x4c]);
    setTimeout(() => c.keyboardReport(0, []), 80);
  };
  const tapKey = (usage: number, modifier = 0) => {
    const c = clientRef.current;
    if (!c) return;
    c.keyboardReport(modifier, [usage]);
    setTimeout(() => c.keyboardReport(0, []), 60);
  };

  const busy = state !== 'connected';

  return (
    <div className="viewer">
      <div className="toolbar">
        <button onClick={onDisconnect}>← Disconnect</button>
        <span className={`status status-${state}`}>
          {state}
          {detail ? ` — ${detail}` : ''}
        </span>
        <div className="spacer" />
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
          ⌨ Keyboard
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
        <video
          ref={videoRef}
          className="screen"
          playsInline
          autoPlay
          muted
        />
        {busy && (
          <div className="overlay">
            <div className="spinner" />
            <p>{state === 'failed' ? `Failed: ${detail}` : `${state}…`}</p>
            {state === 'failed' && (
              <button onClick={onDisconnect}>Back</button>
            )}
          </div>
        )}
      </div>

      {showKeyboard && (
        <OnScreenKeyboard onTap={tapKey} onCtrlAltDel={sendCtrlAltDel} />
      )}
    </div>
  );
}

// Minimal on-screen special-key row for touch devices. The system soft keyboard
// (via the hidden input) handles regular typing; this covers keys phones lack.
function OnScreenKeyboard({
  onTap,
  onCtrlAltDel,
}: {
  onTap: (usage: number, modifier?: number) => void;
  onCtrlAltDel: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
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
        <button onClick={onCtrlAltDel}>Ctrl+Alt+Del</button>
        <button onClick={() => inputRef.current?.focus()}>Type…</button>
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
      {/* Hidden input lets the mobile soft keyboard drive keydown/keyup which
          the global handler already forwards as HID reports. */}
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
