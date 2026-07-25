import { useState } from 'react';
import { DeviceList } from './components/DeviceList';
import { Viewer } from './components/Viewer';
import {
  loadDevices,
  upsertDevice,
  deleteDevice,
  touchDevice,
  type SavedDevice,
} from './storage/devices';

export default function App() {
  const [devices, setDevices] = useState<SavedDevice[]>(() => loadDevices());
  const [active, setActive] = useState<SavedDevice | null>(null);

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
    />
  );
}
