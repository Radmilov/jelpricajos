const $ = (id) => document.getElementById(id);
const msg = (k, ...a) => chrome.i18n.getMessage(k, a.map(String)) || k;
const DEFAULTS = { apiUrl: 'http://127.0.0.1:8000', enabled: true, blur: false, muteParty: false, scope: 'all' };

document.querySelectorAll('[data-i18n]').forEach((el) => { el.textContent = msg(el.dataset.i18n); });
document.documentElement.lang = chrome.i18n.getUILanguage();

const pad = (n) => String(n).padStart(2, '0');
function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

let snapshot = null;
let fetchedAt = 0;

function render() {
  const r = snapshot;
  const head = $('statusText'), sub = $('statusSub'), dot = $('dot');
  head.className = 'headline'; dot.className = 'dot';
  if (!r) { head.textContent = msg('statusLoading'); sub.textContent = ''; return; }
  const s = r.status;
  if (!r.healthy || !s) {
    head.textContent = msg('statusError');
    sub.textContent = r.lastError || r.settings.apiUrl;
    return;
  }
  const parts = [];
  if (s.vucic_speaking) {
    head.textContent = msg('statusVucic'); head.classList.add('v'); dot.classList.add('v');
    const elapsed = (s.current_session_duration_sec || 0) + (Date.now() - fetchedAt) / 1000;
    parts.push(msg('since', fmtDur(elapsed)));
  } else if (s.party_member_speaking) {
    head.textContent = msg('statusParty'); head.classList.add('p'); dot.classList.add('p');
  } else if (s.stream_status === 'OFFLINE') {
    head.textContent = msg('statusOffline');
  } else {
    head.textContent = msg('statusOther'); dot.classList.add('ok');
  }
  parts.push(r.mutedCount > 0 ? msg('mutingNow', r.mutedCount) : msg('notMuting'));
  if (r.blurring) parts.push(msg('blurringNow'));
  if (s.simulated) parts.push(msg('simulated'));
  sub.textContent = parts.join(' · ');
}

async function refresh(force) {
  try {
    snapshot = await chrome.runtime.sendMessage({ type: 'getState', refresh: !!force });
    fetchedAt = Date.now();
  } catch (_) { snapshot = null; }
  render();
}

async function loadForm() {
  const s = await chrome.storage.sync.get(DEFAULTS);
  $('enabled').checked = s.enabled;
  $('muteParty').checked = s.muteParty;
  $('blur').checked = s.blur;
  $('apiUrl').value = s.apiUrl;
  document.querySelectorAll('input[name="scope"]').forEach((r) => { r.checked = r.value === s.scope; });
  $('dash').href = s.apiUrl.replace(/\/+$/, '') + '/';
}

$('enabled').addEventListener('change', (e) => chrome.storage.sync.set({ enabled: e.target.checked }).then(() => refresh(true)));
$('blur').addEventListener('change', (e) => chrome.storage.sync.set({ blur: e.target.checked }).then(() => refresh(true)));
$('muteParty').addEventListener('change', (e) => chrome.storage.sync.set({ muteParty: e.target.checked }).then(() => refresh(true)));
document.querySelectorAll('input[name="scope"]').forEach((r) => r.addEventListener('change', (e) => {
  if (e.target.checked) chrome.storage.sync.set({ scope: e.target.value }).then(() => refresh(true));
}));

$('saveApi').addEventListener('click', async () => {
  const out = $('apiMsg');
  let url;
  try {
    url = new URL($('apiUrl').value.trim());
    if (!/^https?:$/.test(url.protocol)) throw new Error();
  } catch (_) { out.textContent = msg('invalidUrl'); return; }
  const origin = `${url.protocol}//${url.hostname}/*`;
  const builtIn = ['127.0.0.1', 'localhost'].includes(url.hostname) && url.protocol === 'http:';
  if (!builtIn) {
    // Must run inside the click handler (user gesture).
    const ok = await chrome.permissions.request({ origins: [origin] }).catch(() => false);
    if (!ok) { out.textContent = msg('permDenied'); return; }
  }
  const clean = url.href.replace(/\/+$/, '');
  await chrome.storage.sync.set({ apiUrl: clean });
  $('apiUrl').value = clean;
  $('dash').href = clean + '/';
  out.textContent = msg('saved');
  refresh(true);
});

$('dash').addEventListener('click', (e) => { e.preventDefault(); chrome.tabs.create({ url: $('dash').href }); });

loadForm();
refresh(true);
setInterval(() => refresh(false), 1000);
