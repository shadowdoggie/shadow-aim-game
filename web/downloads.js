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

export async function loadDownloads({doc = document, nav = navigator, fetcher = fetch, timeoutMs = 7000} = {}) {
  const primary = doc.getElementById('primary-download');
  const label = doc.getElementById('primary-label');
  const status = doc.getElementById('download-status');
  if (!primary || !label || !status) return;
  const os = detectOS(nav);
  const platformName = os === 'windows' ? 'Windows' : 'Linux';
  const manual = Object.fromEntries(Object.keys(ASSET_NAMES).map(key => [key, doc.getElementById(`${key}-download`)]));
  primary.href = RELEASE_PAGE;
  label.textContent = os === 'unsupported' ? 'View desktop downloads' : `Get the ${platformName} download`;
  status.textContent = os === 'unsupported' ? 'For Windows and Linux PCs (x86-64). No macOS or mobile build.' : 'Checking the latest release…';
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), timeoutMs);
  try {
    const response = await fetcher(RELEASE_API, {headers: {Accept: 'application/vnd.github+json'}, signal: abort.signal, credentials: 'omit'});
    if (!response.ok) throw new Error('Release unavailable');
    const release = await response.json();
    const assets = Object.fromEntries(Object.keys(ASSET_NAMES).map(key => [key, selectAsset(release, key)]));
    for (const [key, element] of Object.entries(manual)) {
      if (!element) continue;
      element.href = assets[key] || RELEASE_PAGE;
      element.title = assets[key] ? `Download the latest ${key === 'windows' ? 'Windows' : 'Linux'} archive` : 'Open GitHub Releases to check available builds';
    }
    if (os === 'unsupported') return;
    if (assets[os]) {
      primary.href = assets[os];
      label.textContent = `Download for ${platformName}`;
      const version = typeof release.tag_name === 'string' ? release.tag_name.slice(0, 64) : 'Latest release';
      status.textContent = `${version} · ${platformName} · 64-bit x86`;
    } else {
      label.textContent = `Check ${platformName} releases`;
      status.textContent = `The latest release has no ${platformName} archive yet. Check GitHub for available builds.`;
    }
  } catch {
    if (os !== 'unsupported') label.textContent = `Get ${platformName} on GitHub`;
    status.textContent = os === 'unsupported'
      ? 'For Windows and Linux PCs (x86-64). Open GitHub to choose a desktop build.'
      : 'Couldn’t check the latest release. Choose your archive on GitHub.';
  } finally { clearTimeout(timer); }
}

if (typeof document !== 'undefined' && document.getElementById('primary-download')) loadDownloads();
