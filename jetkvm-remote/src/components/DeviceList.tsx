import { useState } from 'react';
import type { SavedDevice } from '../storage/devices';

interface DeviceListProps {
  devices: SavedDevice[];
  onConnect: (device: SavedDevice) => void;
  onSave: (device: Omit<SavedDevice, 'id'> & { id?: string }) => void;
  onDelete: (id: string) => void;
}

const EMPTY = { name: '', host: '', password: '' };

export function DeviceList({
  devices,
  onConnect,
  onSave,
  onDelete,
}: DeviceListProps) {
  const [editing, setEditing] = useState<
    (typeof EMPTY & { id?: string }) | null
  >(null);

  const startAdd = () => setEditing({ ...EMPTY });
  const startEdit = (d: SavedDevice) =>
    setEditing({ id: d.id, name: d.name, host: d.host, password: d.password });

  const submit = () => {
    if (!editing) return;
    if (!editing.host.trim()) return;
    onSave({
      ...editing,
      name: editing.name.trim() || editing.host.trim(),
    });
    setEditing(null);
  };

  return (
    <div className="device-list">
      <header className="app-header">
        <h1>JetKVM Remote</h1>
        <button className="primary" onClick={startAdd}>
          + Add device
        </button>
      </header>

      {devices.length === 0 && !editing && (
        <p className="empty">
          No devices yet. Add your JetKVM's address (IP, MagicDNS name, or a
          Tailscale Funnel <code>ts.net</code> URL) to get started.
        </p>
      )}

      <ul className="cards">
        {devices.map((d) => (
          <li key={d.id} className="card">
            <div className="card-main" onClick={() => onConnect(d)}>
              <span className="card-name">{d.name}</span>
              <span className="card-host">{d.host}</span>
              {d.lastConnected && (
                <span className="card-meta">
                  last: {new Date(d.lastConnected).toLocaleString()}
                </span>
              )}
            </div>
            <div className="card-actions">
              <button className="primary" onClick={() => onConnect(d)}>
                Connect
              </button>
              <button onClick={() => startEdit(d)}>Edit</button>
              <button className="danger" onClick={() => onDelete(d.id)}>
                Delete
              </button>
            </div>
          </li>
        ))}
      </ul>

      {editing && (
        <div className="modal" onClick={() => setEditing(null)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <h2>{editing.id ? 'Edit device' : 'Add device'}</h2>
            <label>
              Name
              <input
                value={editing.name}
                onChange={(e) =>
                  setEditing({ ...editing, name: e.target.value })
                }
                placeholder="Home server"
              />
            </label>
            <label>
              Host / URL
              <input
                value={editing.host}
                onChange={(e) =>
                  setEditing({ ...editing, host: e.target.value })
                }
                placeholder="jetkvm.tailnet.ts.net"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
              />
            </label>
            <label>
              Password
              <input
                type="password"
                value={editing.password}
                onChange={(e) =>
                  setEditing({ ...editing, password: e.target.value })
                }
                placeholder="(leave empty if disabled)"
              />
            </label>
            <div className="dialog-actions">
              <button onClick={() => setEditing(null)}>Cancel</button>
              <button className="primary" onClick={submit}>
                Save
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
