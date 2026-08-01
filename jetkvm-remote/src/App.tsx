import { useEffect, useState } from 'react';
import { DeviceList } from './components/DeviceList';
import { Viewer } from './components/Viewer';
import {
  loadDevices,
  upsertDevice,
  deleteDevice,
  touchDevice,
  type SavedDevice,
} from './storage/devices';
import { checkForMobileUpdate, downloadAndInstallUpdate, RELEASE_PAGE_URL } from './jetkvm/updateCheck';

async function openReleasePage() {
  try {
    const capacitor = await import('@capacitor/core').catch(() => null);
    if (capacitor?.Capacitor.isNativePlatform()) {
      const { Browser } = await import('@capacitor/browser');
      await Browser.open({ url: RELEASE_PAGE_URL });
      return;
    }
  } catch {
    /* fall through */
  }
  window.open(RELEASE_PAGE_URL, '_blank');
}

export default function App() {
  const [devices, setDevices] = useState<SavedDevice[]>(() => loadDevices());
  const [active, setActive] = useState<SavedDevice | null>(null);
  const [updateAvailable, setUpdateAvailable] = useState(false);
  const [updating, setUpdating] = useState(false);

  useEffect(() => {
    void checkForMobileUpdate().then(setUpdateAvailable);
  }, []);

  const refresh = () => setDevices(loadDevices());

  const handleConnect = (d: SavedDevice) => {
    touchDevice(d.id);
    refresh();
    setActive(d);
  };

  if (active) {
    return (
      <Viewer
        device={active}
        onDisconnect={() => {
          setActive(null);
          refresh();
        }}
      />
    );
  }

  return (
    <DeviceList
      devices={devices}
      onConnect={handleConnect}
      onSave={(d) => {
        upsertDevice(d);
        refresh();
      }}
      onDelete={(id) => {
        deleteDevice(id);
        refresh();
      }}
      updateAvailable={updateAvailable}
      updating={updating}
      onOpenUpdate={() => {
        if (updating) return;
        setUpdating(true);
        void downloadAndInstallUpdate()
          .then((handled) => {
            // Android: the install screen is now up (or the user will see
            // an error toast from the OS/plugin) -- either way there's
            // nothing left for this banner to do, whether they actually
            // install it or back out is on them from here.
            if (!handled) void openReleasePage();
          })
          .finally(() => setUpdating(false));
      }}
    />
  );
}
