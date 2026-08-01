// USB HID keyboard mapping.
// Converts browser KeyboardEvent.code values into USB HID usage IDs and
// modifier bitmask bits, matching what JetKVM's keyboardReport expects.

// Modifier bitmask (USB HID standard)
export const MOD = {
  LCTRL: 0x01,
  LSHIFT: 0x02,
  LALT: 0x04,
  LGUI: 0x08, // left Meta / Win / Cmd
  RCTRL: 0x10,
  RSHIFT: 0x20,
  RALT: 0x40,
  RGUI: 0x80,
} as const;

// KeyboardEvent.code -> modifier bit. Internal to KeyboardState below --
// callers work in terms of MOD bits directly, not browser key codes.
const MODIFIER_CODES: Record<string, number> = {
  ControlLeft: MOD.LCTRL,
  ControlRight: MOD.RCTRL,
  ShiftLeft: MOD.LSHIFT,
  ShiftRight: MOD.RSHIFT,
  AltLeft: MOD.LALT,
  AltRight: MOD.RALT,
  MetaLeft: MOD.LGUI,
  MetaRight: MOD.RGUI,
};

// KeyboardEvent.code -> USB HID usage ID
export const KEY_CODES: Record<string, number> = {
  // Letters
  KeyA: 0x04, KeyB: 0x05, KeyC: 0x06, KeyD: 0x07, KeyE: 0x08, KeyF: 0x09,
  KeyG: 0x0a, KeyH: 0x0b, KeyI: 0x0c, KeyJ: 0x0d, KeyK: 0x0e, KeyL: 0x0f,
  KeyM: 0x10, KeyN: 0x11, KeyO: 0x12, KeyP: 0x13, KeyQ: 0x14, KeyR: 0x15,
  KeyS: 0x16, KeyT: 0x17, KeyU: 0x18, KeyV: 0x19, KeyW: 0x1a, KeyX: 0x1b,
  KeyY: 0x1c, KeyZ: 0x1d,
  // Numbers
  Digit1: 0x1e, Digit2: 0x1f, Digit3: 0x20, Digit4: 0x21, Digit5: 0x22,
  Digit6: 0x23, Digit7: 0x24, Digit8: 0x25, Digit9: 0x26, Digit0: 0x27,
  // Whitespace / control
  Enter: 0x28, Escape: 0x29, Backspace: 0x2a, Tab: 0x2b, Space: 0x2c,
  // Punctuation
  Minus: 0x2d, Equal: 0x2e, BracketLeft: 0x2f, BracketRight: 0x30,
  Backslash: 0x31, Semicolon: 0x33, Quote: 0x34, Backquote: 0x35,
  Comma: 0x36, Period: 0x37, Slash: 0x38, CapsLock: 0x39,
  // Function keys
  F1: 0x3a, F2: 0x3b, F3: 0x3c, F4: 0x3d, F5: 0x3e, F6: 0x3f,
  F7: 0x40, F8: 0x41, F9: 0x42, F10: 0x43, F11: 0x44, F12: 0x45,
  // Navigation / editing
  PrintScreen: 0x46, ScrollLock: 0x47, Pause: 0x48, Insert: 0x49,
  Home: 0x4a, PageUp: 0x4b, Delete: 0x4c, End: 0x4d, PageDown: 0x4e,
  ArrowRight: 0x4f, ArrowLeft: 0x50, ArrowDown: 0x51, ArrowUp: 0x52,
  // Numpad
  NumLock: 0x53, NumpadDivide: 0x54, NumpadMultiply: 0x55,
  NumpadSubtract: 0x56, NumpadAdd: 0x57, NumpadEnter: 0x58,
  Numpad1: 0x59, Numpad2: 0x5a, Numpad3: 0x5b, Numpad4: 0x5c,
  Numpad5: 0x5d, Numpad6: 0x5e, Numpad7: 0x5f, Numpad8: 0x60,
  Numpad9: 0x61, Numpad0: 0x62, NumpadDecimal: 0x63,
  ContextMenu: 0x65,
  // Korean keyboard layout: 한/영 (Hangul/English toggle) and 한자 (Hanja).
  // Browsers report these as KeyboardEvent.code "Lang1"/"Lang2" per the W3C
  // UI Events code spec; USB HID usage IDs 0x90/0x91 ("Keyboard Lang1/Lang2").
  Lang1: 0x90,
  Lang2: 0x91,
};

/**
 * Tracks currently-pressed keys and produces a HID keyboard report
 * (modifier byte + up to 6 usage IDs).
 */
export class KeyboardState {
  private pressed = new Set<number>(); // usage IDs
  private modifier = 0;

  /** Returns true if the event was mapped and a report should be sent. */
  down(code: string): boolean {
    if (code in MODIFIER_CODES) {
      this.modifier |= MODIFIER_CODES[code];
      return true;
    }
    const usage = KEY_CODES[code];
    if (usage === undefined) return false;
    this.pressed.add(usage);
    return true;
  }

  up(code: string): boolean {
    if (code in MODIFIER_CODES) {
      this.modifier &= ~MODIFIER_CODES[code];
      return true;
    }
    const usage = KEY_CODES[code];
    if (usage === undefined) return false;
    this.pressed.delete(usage);
    return true;
  }

  report(): { modifier: number; keys: number[] } {
    return { modifier: this.modifier, keys: [...this.pressed].slice(0, 6) };
  }

  reset() {
    this.pressed.clear();
    this.modifier = 0;
  }
}

// Mobile virtual keyboards (Android/iOS) don't send usable KeyboardEvent.code
// values — most soft keyboards report code:"" (or skip keydown entirely and
// only fire an `input` event with the typed character), so the physical-key
// path above (KeyboardState.down/up, keyed on `code`) never fires there.
// This maps the actual typed *character* (from the input event) to a HID
// usage + shift requirement instead, so mobile typing works without needing
// real scan codes at all.
const SHIFT_CHARS: Record<string, string> = {
  '!': '1', '@': '2', '#': '3', $: '4', '%': '5', '^': '6', '&': '7',
  '*': '8', '(': '9', ')': '0', _: '-', '+': '=', '{': '[', '}': ']',
  '|': '\\', ':': ';', '"': "'", '<': ',', '>': '.', '?': '/', '~': '`',
};
const CHAR_CODES: Record<string, string> = {
  ' ': 'Space', '-': 'Minus', '=': 'Equal', '[': 'BracketLeft',
  ']': 'BracketRight', '\\': 'Backslash', ';': 'Semicolon', "'": 'Quote',
  ',': 'Comma', '.': 'Period', '/': 'Slash', '`': 'Backquote',
};

export function charToKey(ch: string): { usage: number; shift: boolean } | null {
  let shift = false;
  let base = ch;
  if (ch in SHIFT_CHARS) {
    shift = true;
    base = SHIFT_CHARS[ch];
  } else if (/[A-Z]/.test(ch)) {
    shift = true;
    base = ch.toLowerCase();
  }

  let code: string | undefined;
  if (/[a-z]/.test(base)) code = `Key${base.toUpperCase()}`;
  else if (/[0-9]/.test(base)) code = `Digit${base}`;
  else code = CHAR_CODES[base];

  if (!code) return null;
  const usage = KEY_CODES[code];
  if (usage === undefined) return null;
  return { usage, shift };
}

// Mouse button bitmask (matches USB HID)
export const MOUSE_BTN = { LEFT: 1, RIGHT: 2, MIDDLE: 4 } as const;

/** Map a browser MouseEvent.button to the HID button bit. */
export function mouseButtonBit(button: number): number {
  switch (button) {
    case 0:
      return MOUSE_BTN.LEFT;
    case 1:
      return MOUSE_BTN.MIDDLE;
    case 2:
      return MOUSE_BTN.RIGHT;
    default:
      return 0;
  }
}
