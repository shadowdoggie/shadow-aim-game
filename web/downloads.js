const REPOSITORY = 'shadowdoggie/shadow-aim-game';
export const RELEASE_PAGE = `https://github.com/${REPOSITORY}/releases/latest`;
export const RELEASE_API = `https://api.github.com/repos/${REPOSITORY}/releases/latest`;
const ASSET_NAMES = {
  windows: 'shadow-aim-windows-x86_64.zip',
  linux: 'shadow-aim-linux-x86_64.tar.gz',
};

export function detectOS(nav = navigator) {
  const ua = nav.userAgent || '';
  const platform = nav.userAgentData?.platform || nav.platform || '';
  if (/Android|iPhone|iPad|iPod|CrOS/i.test(ua) || (/Mac/i.test(platform) && nav.maxTouchPoints > 1)) return 'unsupported';
  if (/Win/i.test(platform) || /Windows/i.test(ua)) return 'windows';
  if (/Linux/i.test(platform) || /X11.*Linux/i.test(ua)) return 'linux';
  return 'unsupported';
}

export function directAssetURL(os) {
  return ASSET_NAMES[os] ? `${RELEASE_PAGE}/download/${ASSET_NAMES[os]}` : null;
}

export function selectAsset(release, os) {
  if (!ASSET_NAMES[os] || release?.draft || release?.prerelease || !Array.isArray(release?.assets)) return null;
  const asset = release.assets.find(item => item.name === ASSET_NAMES[os] && item.state === 'uploaded');
  if (!asset || typeof asset.browser_download_url !== 'string') return null;
  try {
    const url = new URL(asset.browser_download_url);
    if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.username || url.password ||
        !url.pathname.startsWith(`/${REPOSITORY}/releases/download/`)) return null;
    return url.href;
  } catch { return null; }
}

function setLink(element, href) {
  if (!element) return;
  if (href) {
    element.href = href;
    element.removeAttribute('aria-disabled');
    element.removeAttribute('tabindex');
  } else {
    element.removeAttribute('href');
    element.setAttribute('aria-disabled', 'true');
    element.setAttribute('tabindex', '-1');
  }
}

export async function loadDownloads({doc = document, nav = navigator, fetcher = fetch, timeoutMs = 7000} = {}) {
  const status = doc.getElementById('download-status');
  const controls = [['primary-download', 'primary-label'], ['hero-download', 'hero-label']]
    .map(([button, label]) => ({button: doc.getElementById(button), label: doc.getElementById(label)}))
    .filter(item => item.button && item.label);
  if (!controls.length || !status) return;
  const os = detectOS(nav);
  const supported = os !== 'unsupported';
  const platformName = os === 'windows' ? 'Windows' : 'Linux';
  const manual = Object.fromEntries(Object.keys(ASSET_NAMES).map(key => [key, doc.getElementById(`${key}-download`)]));
  const setPrimary = (href, label) => controls.forEach(item => {setLink(item.button, href); item.label.textContent = label;});
  // Direct aliases work before the API responds, and survive rate limits/offline API failures.
  setPrimary(supported ? directAssetURL(os) : '#platform-downloads', supported ? `Download for ${platformName}` : 'Choose a desktop download');
  for (const [key, element] of Object.entries(manual)) setLink(element, directAssetURL(key));
  status.textContent = supported ? `${platformName} · 64-bit x86 · Checking release details…`
    : 'For Windows and Linux PCs (x86-64). No macOS or mobile build.';
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), timeoutMs);
  try {
    const response = await fetcher(RELEASE_API, {headers: {Accept: 'application/vnd.github+json'}, signal: abort.signal, credentials: 'omit'});
    if (!response.ok) throw new Error('Release unavailable');
    const release = await response.json();
    if (!Array.isArray(release?.assets) || release.draft || release.prerelease) throw new Error('Release metadata unavailable');
    const assets = Object.fromEntries(Object.keys(ASSET_NAMES).map(key => [key, selectAsset(release, key)]));
    for (const [key, element] of Object.entries(manual)) {
      if (!element) continue;
      setLink(element, assets[key]);
      if (!assets[key]) element.textContent = `${key === 'windows' ? 'Windows' : 'Linux'} unavailable`;
      element.title = assets[key] ? 'Download the latest archive' : 'This platform has no ready archive in the latest release. Check release notes.';
    }
    if (!supported) return;
    if (assets[os]) {
      setPrimary(assets[os], `Download for ${platformName}`);
      const version = typeof release.tag_name === 'string' ? release.tag_name.slice(0, 64) : 'Latest release';
      status.textContent = `${version} · ${platformName} · 64-bit x86`;
    } else {
      setPrimary(null, `${platformName} build unavailable`);
      status.textContent = `The latest release has no ready ${platformName} archive. Check release notes below for available builds.`;
    }
  } catch {
    // Do not turn the download into a repository/release-page navigation on API failure.
    status.textContent = supported ? `Direct ${platformName} download · Release details unavailable right now.`
      : 'For Windows and Linux PCs (x86-64). Choose a compatible desktop archive below.';
  } finally { clearTimeout(timer); }
}

const DRILLS = {
  clicking: ['CONTROL / PLACEMENT', 'Move, settle, and place the shot. Build control as you acquire each target.'],
  tracking: ['SMOOTHNESS / CONTROL', 'Stay with a moving target. Work on smooth corrections instead of chasing every movement.'],
  switching: ['ACQUISITION / TRANSITIONS', 'Find the next target and make the transition. Balance your pace with clean hits.'],
  reactive: ['REACTION / ADAPTATION', 'Stay ready for direction changes. Respond, recover, and get back on target.'],
};

export function setupDrills(doc = document) {
  const stage = doc.querySelector('.drill-stage');
  if (!stage) return;
  const tabs = [...stage.querySelectorAll('[role="tab"]')];
  const select = tab => {
    const mode = tab.dataset.drill;
    if (!DRILLS[mode]) return;
    stage.dataset.mode = mode;
    for (const item of tabs) {item.setAttribute('aria-selected', String(item === tab)); item.tabIndex = item === tab ? 0 : -1;}
    doc.getElementById('drill-preview').setAttribute('aria-labelledby', tab.id);
    doc.getElementById('drill-category').textContent = DRILLS[mode][0];
    doc.getElementById('drill-description').textContent = DRILLS[mode][1];
  };
  for (const tab of tabs) {
    tab.addEventListener('click', () => select(tab));
    tab.addEventListener('keydown', event => {
      let index = tabs.indexOf(tab);
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') index = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') index = (index + tabs.length - 1) % tabs.length;
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = tabs.length - 1;
      else return;
      event.preventDefault(); select(tabs[index]); tabs[index].focus();
    });
  }
}

if (typeof document !== 'undefined') {
  if (document.getElementById('primary-download')) loadDownloads();
  setupDrills();
}
