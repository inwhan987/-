// Saved-device store. Persists to localStorage (works in browser, Capacitor
// WebView, and Electron). Passwords are stored locally on-device only.
//
// NOTE: localStorage is not encrypted. For a production build, swap this for
// the platform secure store (Capacitor Preferences/Keychain, Electron
// safeStorage). The interface below is intentionally small so that swap is easy.

export interface SavedDevice {
  id: string;
  name: string;
  host: string; // host or full URL
  password: string; // stored locally; may be empty (no-password mode)
  lastConnected?: number;
  // Optional: the router's public IP that the device's own LAN is
  // DMZ'd/port-forwarded to (see client.ts's withPublicIpCandidate). Per
  // device, not hardcoded, so anyone using this app can set it up for
  // their own network -- the app has no way to know this on its own (no
  // API exposes a router's own public IP or DMZ config to a web page).
  // Without it set, ICE just has one fewer candidate to try; nothing
  // breaks, it only stops the specific "device is behind a plain router
  // with no reachable public address of its own" case from working.
  publicIp?: string;
}

const KEY = 'jetkvm.devices.v1';

export function loadDevices(): SavedDevice[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const list = JSON.parse(raw) as SavedDevice[];
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

function persist(list: SavedDevice[]) {
  localStorage.setItem(KEY, JSON.stringify(list));
}

export function upsertDevice(device: Omit<SavedDevice, 'id'> & { id?: string }) {
  const list = loadDevices();
  if (device.id) {
    const idx = list.findIndex((d) => d.id === device.id);
    if (idx >= 0) {
      list[idx] = { ...list[idx], ...device, id: device.id };
      persist(list);
      return list[idx];
    }
  }
  const created: SavedDevice = {
    ...device,
    id: crypto.randomUUID(),
  };
  list.push(created);
  persist(list);
  return created;
}

export function deleteDevice(id: string) {
  persist(loadDevices().filter((d) => d.id !== id));
}

export function touchDevice(id: string) {
  const list = loadDevices();
  const idx = list.findIndex((d) => d.id === id);
  if (idx >= 0) {
    list[idx].lastConnected = Date.now();
    persist(list);
  }
}
