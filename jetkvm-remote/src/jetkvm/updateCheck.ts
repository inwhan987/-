// Sideloaded Android APKs can't silently self-update like the desktop
// Electron build does (no OS-level auto-install without a store, and every
// install needs an explicit user tap) -- this just checks whether a newer
// build exists so the app can prompt instead of the user never knowing.
import pkg from '../../package.json';

const VERSION_URL = 'https://github.com/inwhan987/-/releases/download/latest/android-latest.json';
export const RELEASE_PAGE_URL = 'https://github.com/inwhan987/-/releases/tag/latest';

interface CapacitorHttpModule {
  request(options: { url: string; method: string }): Promise<{ status: number; data: unknown }>;
}

// "0.1.42" -> 42. Both sides always share the "0.1." prefix (see the CI
// workflow's version bump step), so comparing the trailing run number is
// enough -- no need for full semver parsing.
function runNumber(version: string): number {
  const match = /\.(\d+)$/.exec(version.trim());
  return match ? parseInt(match[1], 10) : 0;
}

export async function checkForMobileUpdate(): Promise<boolean> {
  try {
    const capacitor = await import('@capacitor/core').catch(() => null);
    if (!capacitor?.Capacitor.isNativePlatform() || capacitor.Capacitor.getPlatform() !== 'android') {
      return false; // desktop has real auto-update; iOS has neither this nor that yet
    }
    const CapacitorHttp = (capacitor as unknown as { CapacitorHttp: CapacitorHttpModule }).CapacitorHttp;
    const res = await CapacitorHttp.request({ url: VERSION_URL, method: 'GET' });
    if (res.status !== 200) return false;
    const data = typeof res.data === 'string' ? JSON.parse(res.data) : res.data;
    const remote = (data as { version?: string })?.version;
    if (!remote) return false;
    return runNumber(remote) > runNumber(pkg.version);
  } catch {
    return false; // offline, rate-limited, whatever -- just don't nag
  }
}
