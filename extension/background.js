// Vučić Mute — service worker.
// Polls the monitor's public API and mutes YouTube tabs while Vučić (and optionally
// MPs of his party group) is speaking. It only ever unmutes tabs that it muted itself,
// so tabs you muted by hand stay muted.

const DEFAULTS = {
  apiUrl: 'http://127.0.0.1:8000',
  enabled: true,     // mute while he speaks
  blur: false,       // blur the video picture while he speaks
  muteParty: false,  // apply the same to MPs of his parliamentary group
  scope: 'all', // 'all' | 'parliament'
};
const POLL_MS = 2000;
const FETCH_TIMEOUT_MS = 4000;
// If the monitor can't be reached for this long, restore sound (fail open).
const FAIL_OPEN_MS = 20000;
const YT_PATTERNS = ['*://*.youtube.com/*'];

let settings = { ...DEFAULTS };
let status = null;      // last /api/v1/status payload
let lastOkAt = 0;
let lastError = null;
let mutedCount = 0;
let loopRunning = false;
let settingsLoaded = null;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const apiBase = () => settings.apiUrl.replace(/\/+$/, '');

function loadSettings() {
  settingsLoaded = chrome.storage.sync.get(DEFAULTS).then((s) => { settings = { ...DEFAULTS, ...s }; });
  return settingsLoaded;
}

async function fetchStatus() {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const r = await fetch(`${apiBase()}/api/v1/status`, { cache: 'no-store', signal: ctrl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

function monitorHealthy() {
  return status && Date.now() - lastOkAt < FAIL_OPEN_MS;
}

// The current speaker triggers our actions: Vučić, or (optionally) an MP of his group.
function speakerTriggers() {
  if (!monitorHealthy()) return false;
  return status.vucic_speaking || (settings.muteParty && status.party_member_speaking);
}
const shouldMute = () => settings.enabled && speakerTriggers();
const shouldBlur = () => settings.blur && speakerTriggers();

function inScope(tab) {
  if (settings.scope === 'all') return true;
  const url = tab.url || '';
  if (/youtube\.com\/@ParlamentSrbija/i.test(url)) return true;
  return !!(status && status.video_id && url.includes(status.video_id));
}

const isOurs = (tab) => !!(tab.mutedInfo && tab.mutedInfo.muted &&
  tab.mutedInfo.reason === 'extension' && tab.mutedInfo.extensionId === chrome.runtime.id);

const blurSent = new Map(); // tabId -> last blur state sent to its content script

async function applyMute() {
  const mute = shouldMute();
  const blur = shouldBlur();
  const tabs = await chrome.tabs.query({ url: YT_PATTERNS });
  let count = 0;
  await Promise.all(tabs.map(async (tab) => {
    const want = mute && inScope(tab);
    const wantBlur = blur && inScope(tab);
    if (blurSent.get(tab.id) !== wantBlur) {
      try {
        await chrome.tabs.sendMessage(tab.id, { type: 'vucicBlur', on: wantBlur });
        blurSent.set(tab.id, wantBlur);
      } catch (_) { /* no content script yet (tab opened before install): it asks on load */ }
    }
    try {
      if (want) {
        if (!tab.mutedInfo || !tab.mutedInfo.muted) await chrome.tabs.update(tab.id, { muted: true });
        if (!tab.mutedInfo || !tab.mutedInfo.muted || isOurs(tab)) count += 1;
      } else if (isOurs(tab)) {
        await chrome.tabs.update(tab.id, { muted: false });
      }
    } catch (_) { /* tab closed meanwhile */ }
  }));
  mutedCount = count;
  await updateBadge();
}

async function updateBadge() {
  let text = '', color = '#666666', title = chrome.i18n.getMessage('extName');
  if (!monitorHealthy()) {
    text = lastError ? '!' : '';
    title += ` — ${chrome.i18n.getMessage('statusError')}`;
  } else if (status.vucic_speaking) {
    text = 'V'; color = '#D92D20';
    title += ` — ${chrome.i18n.getMessage('statusVucic')}`;
  } else if (status.party_member_speaking) {
    text = 'P'; color = '#2557D6';
    title += ` — ${chrome.i18n.getMessage('statusParty')}`;
  }
  if (mutedCount > 0) title += ` · ${chrome.i18n.getMessage('mutingNow', String(mutedCount))}`;
  if (!settings.enabled && text) color = '#999999';
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color });
  if (chrome.action.setBadgeTextColor) await chrome.action.setBadgeTextColor({ color: '#FFFFFF' });
  await chrome.action.setTitle({ title });
}

async function tick() {
  try {
    status = await fetchStatus();
    lastOkAt = Date.now();
    lastError = null;
  } catch (err) {
    lastError = String(err && err.message || err);
  }
  await applyMute();
}

// Poll loop. Each iteration calls extension APIs, which keeps the service worker alive;
// the alarm below restarts the loop if Chrome ever shuts the worker down.
async function loop() {
  if (loopRunning) return;
  loopRunning = true;
  try {
    await (settingsLoaded || loadSettings());
    for (;;) {
      await tick();
      await sleep(POLL_MS);
    }
  } finally {
    loopRunning = false;
  }
}

chrome.alarms.create('watchdog', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'watchdog') loop(); });
chrome.runtime.onStartup.addListener(() => loop());
chrome.runtime.onInstalled.addListener(() => loop());

chrome.storage.onChanged.addListener(async (_changes, area) => {
  if (area !== 'sync') return;
  await loadSettings();
  await tick();
});

// New or navigated YouTube tabs during a speech get muted immediately.
chrome.tabs.onUpdated.addListener((_id, info, tab) => {
  if ((info.url || info.status === 'loading') && /youtube\.com/.test(tab.url || '')) applyMute();
});

chrome.tabs.onRemoved.addListener((id) => blurSent.delete(id));

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === 'blurState') {
    // A YouTube page (re)loaded: tell its content script whether to blur.
    (async () => {
      await (settingsLoaded || loadSettings());
      const on = !!(sender.tab && shouldBlur() && inScope(sender.tab));
      if (sender.tab) blurSent.set(sender.tab.id, on);
      sendResponse({ on });
    })();
    return true;
  }
  if (msg && msg.type === 'getState') {
    (async () => {
      await (settingsLoaded || loadSettings());
      if (msg.refresh) await tick();
      sendResponse({
        status, lastOkAt, lastError, mutedCount, settings,
        healthy: !!monitorHealthy(), muting: shouldMute(), blurring: shouldBlur(),
      });
    })();
    return true; // async response
  }
  return false;
});

loadSettings().then(loop);
