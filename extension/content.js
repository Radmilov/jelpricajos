// jelpricajos — content script on youtube.com.
// Blurs the video picture while the background worker says so. Only the picture is
// affected; the page, comments and controls stay usable.
(() => {
  const CLASS = 'jelpricajos-blur';
  const STYLE_ID = 'jelpricajos-blur-style';

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      html.${CLASS} .html5-video-player video,
      html.${CLASS} #movie_player video,
      html.${CLASS} ytd-miniplayer video {
        filter: blur(64px) saturate(0.5) brightness(0.8) !important;
        transition: filter 150ms ease;
      }
      html.${CLASS} .html5-video-player { overflow: hidden !important; }
      /* "Ambient mode" glow would leak the picture's colours around the player */
      html.${CLASS} #cinematics, html.${CLASS} #cinematics-container { display: none !important; }
    `;
    (document.head || document.documentElement).appendChild(style);
  }

  function apply(on) {
    ensureStyle();
    document.documentElement.classList.toggle(CLASS, !!on);
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === 'vucicBlur') apply(msg.on);
  });

  // Ask for the current state right away (new tab, reload, YouTube page navigation).
  const ask = () => chrome.runtime.sendMessage({ type: 'blurState' })
    .then((r) => apply(r && r.on))
    .catch(() => {});
  ask();
  document.addEventListener('yt-navigate-finish', ask);
})();
