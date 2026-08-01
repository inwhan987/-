// Best-effort static EN -> KO dictionary for the real JetKVM device settings
// page, shown embedded in SettingsFrame. Not a live translator (no external
// API call, so it works offline/inside the local proxy with no extra
// network dependency) -- just exact-string replacement over whatever text
// nodes and common label attributes it finds. Firmware UI text that isn't
// in this list is left in English; add entries here as they're spotted.
const SETTINGS_TRANSLATIONS: Record<string, string> = {
  // Top-level nav / section headings
  General: '일반',
  Video: '영상',
  Hardware: '하드웨어',
  Access: '접근',
  Appearance: '화면',
  Network: '네트워크',
  Advanced: '고급',
  Mouse: '마우스',
  Keyboard: '키보드',
  Update: '업데이트',
  Cloud: '클라우드',
  'Back to KVM': 'KVM으로 돌아가기',

  // General
  'Device Name': '기기 이름',
  Language: '언어',
  'Time Zone': '시간대',
  Timezone: '시간대',
  'Auto Update': '자동 업데이트',
  'Check for Updates': '업데이트 확인',
  'Update Available': '업데이트 있음',
  'Up to date': '최신 상태',
  Restart: '재시작',
  Reboot: '재부팅',
  'Factory Reset': '초기화',
  Logs: '로그',
  'Developer Mode': '개발자 모드',

  // Video / Appearance
  Backlight: '백라이트',
  'Backlight Settings': '백라이트 설정',
  'Stream Quality Factor': '스트림 화질',
  'Display Rotation': '화면 회전',
  Brightness: '밝기',
  Contrast: '명암',
  Saturation: '채도',
  Resolution: '해상도',
  Framerate: '프레임레이트',
  Bitrate: '비트레이트',
  EDID: 'EDID',
  'EDID Mode': 'EDID 모드',
  'Custom EDID': '사용자 지정 EDID',

  // Hardware / Mouse
  'Mouse Jiggler': '마우스 지글러',
  'Mouse Mode': '마우스 모드',
  Absolute: '절대 좌표',
  Relative: '상대 좌표',
  'USB Emulation': 'USB 에뮬레이션',
  'ATX Power': 'ATX 전원',
  'Power Control': '전원 제어',
  'DC Power': 'DC 전원',
  'Virtual Media': '가상 미디어',
  Mount: '마운트',
  Unmount: '마운트 해제',

  // Access / Network
  'Local Auth': '로컬 인증',
  Password: '비밀번호',
  'Change Password': '비밀번호 변경',
  SSH: 'SSH',
  Register: '등록',
  Unregister: '등록 해제',
  DHCP: 'DHCP',
  'Static IP': '고정 IP',
  'IP Address': 'IP 주소',
  'Subnet Mask': '서브넷 마스크',
  Gateway: '게이트웨이',
  DNS: 'DNS',
  mDNS: 'mDNS',
  'Wake on LAN': 'Wake on LAN',
  WiFi: 'WiFi',
  Ethernet: '이더넷',
  Connected: '연결됨',
  Disconnected: '연결 끊김',
  'Not Connected': '연결 안 됨',

  // Common controls
  Save: '저장',
  'Save Changes': '변경사항 저장',
  Cancel: '취소',
  Apply: '적용',
  Confirm: '확인',
  Discard: '취소',
  Reset: '초기화',
  Enable: '사용',
  Disable: '사용 안 함',
  Enabled: '사용함',
  Disabled: '사용 안 함',
  On: '켜짐',
  Off: '꺼짐',
  Yes: '예',
  No: '아니오',
  Edit: '수정',
  Delete: '삭제',
  Add: '추가',
  Name: '이름',
  Description: '설명',
  'Loading...': '불러오는 중...',
};

// Live fallback for anything not in the static dictionary above, via the
// same unofficial (no API key, no cost) endpoint Google's own browser
// extension and most open-source translate libraries use. Not officially
// documented/supported by Google -- it could change or get rate-limited --
// but it's the only no-signup, no-key option that works from inside the
// app itself. Results are cached so the same recurring label (re-scanned
// on every DOM mutation) is only ever fetched once.
const liveCache = new Map<string, string>();
const livePending = new Set<string>();

async function translateLive(text: string): Promise<string | null> {
  if (liveCache.has(text)) return liveCache.get(text)!;
  if (livePending.has(text)) return null; // already in flight; this call's caller will get it next tick
  livePending.add(text);
  try {
    const url =
      'https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=ko&dt=t&q=' +
      encodeURIComponent(text);
    const res = await fetch(url);
    if (!res.ok) return null;
    const data = (await res.json()) as [Array<[string, string]>];
    const translated = data[0].map((seg) => seg[0]).join('');
    if (translated) liveCache.set(text, translated);
    return translated || null;
  } catch {
    return null; // offline, blocked, endpoint changed, whatever -- just leave the English text
  } finally {
    livePending.delete(text);
  }
}

// Skip obvious non-prose: numbers/IPs/versions/already-Korean text/etc. --
// no point spending a network round trip (or risking a nonsense "answer")
// translating a MAC address.
function looksTranslatable(text: string): boolean {
  if (!/[A-Za-z]/.test(text)) return false; // no letters at all
  if (/[가-힣]/.test(text)) return false; // already has Hangul
  if (text.length > 120) return false; // long paragraphs -> not worth it / risk of over-translating
  return true;
}

// Walks all text nodes + a few label-ish attributes under `doc`, replacing
// any that exactly match (after trim) an entry in SETTINGS_TRANSLATIONS.
// Exact-match only (no partial/regex replacement) to avoid mangling text
// that merely contains an English word as a substring of something else.
// Anything not in that static list falls back to translateLive, applied
// asynchronously whenever it resolves (the node may already be gone by
// then if the SPA re-rendered -- guarded by checking it's still attached).
export function translateSettingsPage(doc: Document) {
  const translateNode = (node: Node, trimmed: string) => {
    const text = node.textContent;
    if (text === null) return;
    void translateLive(trimmed).then((translated) => {
      if (!translated || !doc.contains(node)) return;
      const current = node.textContent;
      if (current?.trim() === trimmed) node.textContent = current.replace(trimmed, translated);
    });
  };

  const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
  let node: Node | null;
  while ((node = walker.nextNode())) {
    const text = node.textContent;
    if (!text) continue;
    const trimmed = text.trim();
    if (!trimmed) continue;
    const dict = SETTINGS_TRANSLATIONS[trimmed];
    if (dict) {
      node.textContent = text.replace(trimmed, dict);
    } else if (looksTranslatable(trimmed)) {
      translateNode(node, trimmed);
    }
  }

  doc.querySelectorAll('[placeholder], [aria-label], [title]').forEach((el) => {
    for (const attr of ['placeholder', 'aria-label', 'title']) {
      const value = el.getAttribute(attr);
      if (!value) continue;
      const dict = SETTINGS_TRANSLATIONS[value];
      if (dict) {
        el.setAttribute(attr, dict);
      } else if (looksTranslatable(value)) {
        void translateLive(value).then((translated) => {
          if (translated && el.getAttribute(attr) === value) el.setAttribute(attr, translated);
        });
      }
    }
  });
}
