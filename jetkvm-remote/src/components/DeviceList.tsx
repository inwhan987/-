import { useState } from 'react';
import type { SavedDevice } from '../storage/devices';
import pkg from '../../package.json';

interface DeviceListProps {
  devices: SavedDevice[];
  onConnect: (device: SavedDevice) => void;
  onSave: (device: Omit<SavedDevice, 'id'> & { id?: string }) => void;
  onDelete: (id: string) => void;
  updateAvailable?: boolean;
  onOpenUpdate?: () => void;
}

const EMPTY = { name: '', host: '', password: '', publicIp: '' };

export function DeviceList({
  devices,
  onConnect,
  onSave,
  onDelete,
  updateAvailable,
  onOpenUpdate,
}: DeviceListProps) {
  const [editing, setEditing] = useState<
    (typeof EMPTY & { id?: string }) | null
  >(null);

  const startAdd = () => setEditing({ ...EMPTY });
  const startEdit = (d: SavedDevice) =>
    setEditing({
      id: d.id,
      name: d.name,
      host: d.host,
      password: d.password,
      publicIp: d.publicIp ?? '',
    });

  const submit = () => {
    if (!editing) return;
    if (!editing.host.trim()) return;
    onSave({
      ...editing,
      name: editing.name.trim() || editing.host.trim(),
      publicIp: editing.publicIp.trim() || undefined,
    });
    setEditing(null);
  };

  return (
    <div className="device-list">
      <header className="app-header">
        <h1>원격KVM</h1>
        <button className="primary" onClick={startAdd}>
          + 기기 추가
        </button>
      </header>

      {updateAvailable && (
        <div className="update-banner" onClick={onOpenUpdate}>
          새 버전이 있습니다 — 눌러서 다운로드
        </div>
      )}

      {devices.length === 0 && !editing && (
        <p className="empty">
          등록된 기기가 없습니다. JetKVM 주소(IP, MagicDNS 이름, 또는 Tailscale
          Funnel <code>ts.net</code> 주소)를 추가해서 시작하세요.
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
                  최근 접속: {new Date(d.lastConnected).toLocaleString()}
                </span>
              )}
            </div>
            <div className="card-actions">
              <button className="primary" onClick={() => onConnect(d)}>
                접속
              </button>
              <button onClick={() => startEdit(d)}>편집</button>
              <button className="danger" onClick={() => onDelete(d.id)}>
                삭제
              </button>
            </div>
          </li>
        ))}
      </ul>

      {editing && (
        <div className="modal" onClick={() => setEditing(null)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <h2>{editing.id ? '기기 편집' : '기기 추가'}</h2>
            <label>
              이름
              <input
                value={editing.name}
                onChange={(e) =>
                  setEditing({ ...editing, name: e.target.value })
                }
                placeholder="집 서버"
              />
            </label>
            <label>
              주소 (호스트 / URL)
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
              비밀번호
              <input
                type="password"
                value={editing.password}
                onChange={(e) =>
                  setEditing({ ...editing, password: e.target.value })
                }
                placeholder="(비밀번호 없으면 비워두세요)"
              />
            </label>
            <label>
              공인 IP (선택 — DMZ/포트포워딩 대상)
              <input
                value={editing.publicIp}
                onChange={(e) =>
                  setEditing({ ...editing, publicIp: e.target.value })
                }
                placeholder="예: 121.190.100.246"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
              />
              <span className="field-hint">
                기기가 있는 곳의 공유기를 DMZ로 열거나 UDP 포트를 포워딩해뒀다면, 그
                공유기의 공인 IP를 입력하세요. LTE 등 외부망에서 접속이 안 될 때 이게
                도움이 됩니다. 모르면 비워두세요.
              </span>
            </label>
            <div className="dialog-actions">
              <button onClick={() => setEditing(null)}>취소</button>
              <button className="primary" onClick={submit}>
                저장
              </button>
            </div>
          </div>
        </div>
      )}

      <p className="app-version">v{pkg.version}</p>
    </div>
  );
}
