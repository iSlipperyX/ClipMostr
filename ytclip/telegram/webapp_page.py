"""The Mini App's page: what Telegram shows when the bot's ClipMonster button is opened (webapp.py serves it).

Three files' worth of text, kept here so they ship inside the program with nothing to install: the page, its
style and its script. The page holds nothing private: it asks the server (webapp.py's /api/...) for everything,
signed in by what Telegram hands it. Colours come from the person's own Telegram theme, light or dark.

What it does: the videos waiting to post (to review, queued, in the library) and the ones posted; a video's own
screen to watch it, OK it, post it now or delete it, and to say what's wrong with it so the AI makes it again;
what each of the two people sharing the bot said and did, by name; asking for a new video; and who shares the
bot (invite, remove, leave).
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex, nofollow">
<title>ClipMonster</title>
<link rel="stylesheet" href="app.css">
</head>
<body>
<div id="app" aria-live="polite"><div class="splash"><div class="spin"></div><p>Opening ClipMonster…</p></div></div>
<div id="toast" role="status"></div>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="app.js"></script>
</body>
</html>
"""

CSS = r""":root {
  --f-bg: #ffffff; --f-bg2: #f2f2f7; --f-text: #111114; --f-hint: #8a8a8f; --f-link: #007aff;
  --f-btn: #007aff; --f-btn-text: #ffffff; --f-danger: #e5372b; --f-line: rgba(60, 60, 67, .14);
  --good: #1f9d4c; --warn: #c57600; --other: #b0509e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --f-bg: #1c1c1e; --f-bg2: #0f0f10; --f-text: #f4f4f6; --f-hint: #98989f; --f-link: #4da3ff;
    --f-btn: #0a84ff; --f-btn-text: #ffffff; --f-danger: #ff6257; --f-line: rgba(235, 235, 245, .16);
    --good: #3ccb6c; --warn: #ffb340; --other: #e08ad0;
  }
}
:root {
  --bg: var(--tg-theme-section-bg-color, var(--tg-theme-bg-color, var(--f-bg)));
  --bg2: var(--tg-theme-secondary-bg-color, var(--f-bg2));
  --text: var(--tg-theme-text-color, var(--f-text));
  --hint: var(--tg-theme-hint-color, var(--f-hint));
  --link: var(--tg-theme-link-color, var(--f-link));
  --btn: var(--tg-theme-button-color, var(--f-btn));
  --btn-text: var(--tg-theme-button-text-color, var(--f-btn-text));
  --danger: var(--tg-theme-destructive-text-color, var(--f-danger));
  --line: var(--f-line);
  --safe-b: env(safe-area-inset-bottom, 0px);
  --r: 14px;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--bg2); color: var(--text);
  font: 16px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  -webkit-text-size-adjust: 100%; overscroll-behavior-y: contain;
}
button, textarea, select, input { font: inherit; color: inherit; }
button { cursor: pointer; border: 0; background: none; padding: 0; }
button:disabled { opacity: .5; cursor: default; }
h1, h2, h3, p { margin: 0; }
.splash { min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 14px; padding: 32px; text-align: center; color: var(--hint); }
.splash h2 { color: var(--text); font-size: 20px; }
.splash p { max-width: 30em; }
.spin { width: 26px; height: 26px; border-radius: 50%; border: 3px solid var(--line); border-top-color: var(--btn);
  animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation-duration: 2.4s; } * { scroll-behavior: auto !important; } }

.shell { max-width: 680px; margin: 0 auto; min-height: 100vh; padding-bottom: calc(72px + var(--safe-b)); }
.top { position: sticky; top: 0; z-index: 5; background: var(--bg2); padding: 12px 16px 8px; }
.top-row { display: flex; align-items: center; gap: 10px; }
.top h1 { font-size: 22px; font-weight: 700; letter-spacing: -.01em; flex: 1; min-width: 0; }
.faces { display: flex; }
.face { width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; font-size: 13px;
  font-weight: 700; color: #fff; background: var(--btn); border: 2px solid var(--bg2); }
.face + .face { margin-left: -8px; }
.face.other { background: var(--other); }
.face.lg { width: 44px; height: 44px; font-size: 18px; border: 0; flex: none; }
.face.sm { width: 26px; height: 26px; font-size: 12px; border: 0; flex: none; margin-top: 1px; }
.statusline { margin-top: 4px; font-size: 13px; color: var(--hint); display: flex; gap: 8px; align-items: center;
  min-height: 24px; }
.statusline .what { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--good); flex: none; }
.dot.paused { background: var(--warn); }
.dot.idle { background: var(--hint); }
.textbtn { color: var(--link); font-size: 13px; font-weight: 600; padding: 4px 2px; }

.seg { display: flex; gap: 6px; padding: 4px 16px 10px; overflow-x: auto; scrollbar-width: none; }
.seg::-webkit-scrollbar { display: none; }
.chip { flex: none; padding: 7px 13px; border-radius: 999px; background: var(--bg); color: var(--text);
  font-size: 14px; font-weight: 600; white-space: nowrap; }
.chip.on { background: var(--btn); color: var(--btn-text); }
.chip .n { opacity: .7; margin-left: 5px; font-weight: 500; }

.list { padding: 0 12px; display: flex; flex-direction: column; gap: 8px; }
.card { display: flex; gap: 12px; padding: 10px; background: var(--bg); border-radius: var(--r); text-align: left;
  width: 100%; align-items: stretch; }
.thumb { width: 66px; height: 100px; border-radius: 9px; background: #000 center/cover no-repeat; flex: none;
  position: relative; overflow: hidden; }
.thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.thumb .len { position: absolute; right: 4px; bottom: 4px; font-size: 11px; font-weight: 600; color: #fff;
  background: rgba(0, 0, 0, .62); border-radius: 5px; padding: 1px 5px; }
.card .body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; padding: 2px 0; }
.card .title { font-weight: 600; font-size: 15px; line-height: 1.3; display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden; }
.card .meta { font-size: 13px; color: var(--hint); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: auto; }
.tag { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 999px; background: var(--bg2);
  color: var(--hint); }
.tag.review { color: var(--warn); }
.tag.good { color: var(--good); }
.tag.bad { color: var(--danger); }
.tag.fix { color: var(--link); }
.empty { text-align: center; color: var(--hint); padding: 44px 28px; }
.empty b { display: block; color: var(--text); font-size: 17px; margin-bottom: 6px; }

.nav { position: fixed; left: 0; right: 0; bottom: 0; z-index: 6; background: var(--bg);
  border-top: 1px solid var(--line); padding-bottom: var(--safe-b); }
.nav-in { max-width: 680px; margin: 0 auto; display: flex; }
.nav button { flex: 1; padding: 8px 4px 9px; display: flex; flex-direction: column; align-items: center; gap: 2px;
  font-size: 11px; font-weight: 600; color: var(--hint); }
.nav button.on { color: var(--btn); }
.nav svg { width: 24px; height: 24px; }
.badge { position: absolute; top: -4px; right: -10px; min-width: 17px; height: 17px; border-radius: 9px;
  background: var(--danger); color: #fff; font-size: 11px; font-weight: 700; display: grid; place-items: center;
  padding: 0 4px; }
.ico { position: relative; display: block; height: 24px; }

.sheet { position: fixed; inset: 0; z-index: 10; background: var(--bg2); overflow-y: auto;
  -webkit-overflow-scrolling: touch; }
.sheet-in { max-width: 680px; margin: 0 auto; padding-bottom: calc(28px + var(--safe-b)); }
.bar { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 4px; padding: 8px 8px;
  background: var(--bg2); }
.bar .back { color: var(--link); font-weight: 600; padding: 8px 10px; }
.bar .where { flex: 1; text-align: center; font-size: 13px; color: var(--hint); }
.bar .step { color: var(--link); font-weight: 600; padding: 8px 10px; }
.stage { background: #000; display: flex; justify-content: center; }
.stage video { width: 100%; max-height: 62vh; display: block; background: #000; }
.stage .gone { color: #bbb; padding: 60px 24px; text-align: center; font-size: 14px; }
.block { background: var(--bg); border-radius: var(--r); margin: 10px 12px 0; padding: 14px; }
.block h2 { font-size: 18px; line-height: 1.25; }
.block h3 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--hint); font-weight: 600;
  margin-bottom: 10px; }
.sub { color: var(--hint); font-size: 14px; margin-top: 4px; }
.state { margin-top: 10px; font-size: 14px; font-weight: 600; }
.state.review { color: var(--warn); }
.state.bad { color: var(--danger); }
.cap { margin-top: 10px; font-size: 14px; white-space: pre-wrap; overflow-wrap: anywhere; }
.warns { margin-top: 10px; font-size: 13px; color: var(--warn); }
.note { font-size: 14px; border-left: 3px solid var(--link); padding: 2px 0 2px 10px; }
.note .q { font-style: italic; overflow-wrap: anywhere; }
.acts { display: flex; gap: 8px; margin: 10px 12px 0; }
.btn { flex: 1; padding: 13px 10px; border-radius: 12px; font-weight: 700; font-size: 15px; text-align: center;
  background: var(--bg); color: var(--link); min-height: 48px; }
.btn.primary { background: var(--btn); color: var(--btn-text); }
.btn.danger { color: var(--danger); }
.btn.slim { flex: none; padding: 9px 14px; min-height: 0; font-size: 14px; }
.btn.wide { width: 100%; }
textarea, select, input[type=text] { width: 100%; border: 1px solid var(--line); border-radius: 11px; background: var(--bg2);
  padding: 11px 12px; outline: none; }
textarea { min-height: 92px; resize: vertical; display: block; }
textarea:focus, select:focus { border-color: var(--btn); }
.hints { display: flex; gap: 6px; overflow-x: auto; margin: 10px 0; scrollbar-width: none; }
.hints::-webkit-scrollbar { display: none; }
.hint { flex: none; font-size: 13px; padding: 6px 11px; border-radius: 999px; border: 1px solid var(--line);
  color: var(--text); background: transparent; }
.small { font-size: 13px; color: var(--hint); margin-top: 8px; }
.field { margin-top: 12px; }
.field:first-of-type { margin-top: 0; }
.field > label, .field > .lbl { display: block; font-size: 13px; color: var(--hint); margin-bottom: 6px; }
.picks { display: flex; gap: 6px; flex-wrap: wrap; }
.pick { padding: 8px 13px; border-radius: 999px; border: 1px solid var(--line); background: transparent; color: var(--text);
  font-size: 14px; min-height: 36px; }
.pick.on { background: var(--btn); border-color: var(--btn); color: var(--btn-text); font-weight: 600; }
input[type=text]:focus { border-color: var(--btn); }
.field.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.field.pair label { display: block; font-size: 13px; color: var(--hint); margin-bottom: 6px; }
.picks.two { margin-bottom: 14px; }
.picks.two .pick { flex: 1; text-align: center; }
.textbtn.inl { float: right; padding: 0 2px; }
.games { display: flex; flex-direction: column; border: 1px solid var(--line); border-radius: 11px; overflow: hidden; }
.game { display: flex; gap: 10px; align-items: center; padding: 10px 12px; text-align: left; background: transparent;
  border-top: 1px solid var(--line); min-height: 48px; width: 100%; }
.game:first-child { border-top: 0; }
.game .em { flex: none; width: 22px; text-align: center; }
.game .col { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.game .line { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.game .lg { font-size: 12px; color: var(--hint); }
.game .when { flex: none; font-size: 12px; color: var(--hint); max-width: 38%; text-align: right; }
.game .when.live { color: var(--danger); font-weight: 600; }
.game.wait { opacity: .55; }
.game.on { background: color-mix(in srgb, var(--btn) 14%, transparent); box-shadow: inset 3px 0 0 var(--btn); }
.warnline { color: var(--warn); }
.notes { margin: 8px 0 0 18px; font-size: 13px; color: var(--hint); }
.top .meter { margin-top: 6px; }
.order { padding: 12px 0; border-top: 1px solid var(--line); }
.order:first-of-type { border-top: 0; padding-top: 0; }
.order:last-child { padding-bottom: 0; }
.order .head { display: flex; gap: 10px; align-items: flex-start; }
.order .head .col { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.order .title { font-weight: 600; font-size: 15px; }
.order .who { color: var(--hint); font-size: 12px; margin-top: 1px; }
.order .now { font-size: 14px; margin-top: 6px; overflow-wrap: anywhere; }
.order .now.good { color: var(--good); }
.order .now.bad { color: var(--danger); }
.order ul { margin: 6px 0 0 18px; padding: 0; font-size: 14px; }
.meter { height: 4px; border-radius: 2px; background: var(--line); margin-top: 8px; overflow: hidden; }
.meter > i { display: block; height: 100%; background: var(--btn); border-radius: 2px; }
.count { float: right; }
.said { display: flex; gap: 10px; padding: 10px 0; border-top: 1px solid var(--line); }
.said:first-of-type { border-top: 0; padding-top: 0; }
.said .who { font-size: 13px; font-weight: 700; }
.said .when { font-weight: 400; color: var(--hint); margin-left: 6px; }
.said .txt { font-size: 15px; margin: 2px 0 4px; white-space: pre-wrap; overflow-wrap: anywhere; }
.said .st { font-size: 13px; color: var(--hint); }
.said .st.good { color: var(--good); }
.said .st.bad { color: var(--danger); }
.said .st.going { color: var(--link); }
.said .col { flex: 1; min-width: 0; }
.row { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-top: 1px solid var(--line); }
.row:first-of-type { border-top: 0; padding-top: 0; }
.row:last-child { padding-bottom: 0; }
.row .col { flex: 1; min-width: 0; }
.row .name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row .sub { margin: 0; font-size: 13px; }
.act { display: flex; gap: 10px; padding: 9px 0; border-top: 1px solid var(--line); font-size: 14px; }
.act:first-of-type { border-top: 0; padding-top: 0; }
.act .col { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.act .when { color: var(--hint); font-size: 12px; margin-top: 1px; }
.act .q { color: var(--hint); font-style: italic; }
.code { font: 600 24px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; letter-spacing: .14em;
  text-align: center; padding: 14px; background: var(--bg2); border-radius: 11px; margin: 10px 0; user-select: all; }
.pad { padding: 0 12px; }
.gap { height: 10px; }
#toast { position: fixed; left: 50%; bottom: calc(86px + var(--safe-b)); transform: translate(-50%, 20px); z-index: 30;
  background: #2c2c30; color: #fff; padding: 11px 16px; border-radius: 12px; font-size: 14px; line-height: 1.35;
  width: max-content; max-width: min(88vw, 520px); opacity: 0; pointer-events: none;
  transition: opacity .18s, transform .18s; text-align: center; border: 1px solid rgba(255, 255, 255, .1);
  box-shadow: 0 8px 28px rgba(0, 0, 0, .38); }
#toast.on { opacity: 1; transform: translate(-50%, 0); }
#toast.bad { background: #b3261e; }
"""

JS = r"""(function () {
'use strict';
var tg = (window.Telegram && window.Telegram.WebApp) || null;
var S = { token: '', media: '', me: null, people: [], pc: '', videos: null, posted: null, status: null, activity: null,
          cats: null, tab: 'videos', filter: 'review', who: 'all', open: null, busy: false, invite: null,
          draft: {}, idea: '', cat: '', last: {}, make: 'video', sports: null,
          sp: { sport: '', league: '', teams: '', since: 'week', want: 'reel', games: 1, plays: 5, cat: '',
                how: 'pick', psport: '', pleague: '', game: '', all: false },
          played: null, playedAt: 0, playedBusy: false };
var app = document.getElementById('app');
var sheet = null;       // the open video's screen
var timer = null;

// --- small helpers -------------------------------------------------------------------------------
function h(tag, attrs) {
  var el = document.createElement(tag), i, k;
  attrs = attrs || {};
  for (k in attrs) {
    if (!Object.prototype.hasOwnProperty.call(attrs, k) || attrs[k] == null || attrs[k] === false) continue;
    if (k === 'class') el.className = attrs[k];
    else if (k === 'text') el.textContent = attrs[k];
    else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), attrs[k]);
    else if (k === 'style') el.style.cssText = attrs[k];
    else el.setAttribute(k, attrs[k] === true ? '' : attrs[k]);
  }
  for (i = 2; i < arguments.length; i++) add(el, arguments[i]);
  return el;
}
function add(el, kid) {
  if (kid == null || kid === false) return;
  if (Array.isArray(kid)) { kid.forEach(function (x) { add(el, x); }); return; }
  el.appendChild(typeof kid === 'string' || typeof kid === 'number' ? document.createTextNode(String(kid)) : kid);
}
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
function svg(path) {
  var s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('fill', 'none'); s.setAttribute('stroke', 'currentColor');
  s.setAttribute('stroke-width', '1.9'); s.setAttribute('stroke-linecap', 'round'); s.setAttribute('stroke-linejoin', 'round');
  s.setAttribute('aria-hidden', 'true');
  path.split('|').forEach(function (d) {
    var p = document.createElementNS('http://www.w3.org/2000/svg', 'path'); p.setAttribute('d', d); s.appendChild(p);
  });
  return s;
}
var ICON = {
  videos: 'M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v11a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 17.5z|M10 9l5 3-5 3z',
  make: 'M12 3l1.8 4.9L19 9.7l-4.2 3.1L16.2 18 12 15l-4.2 3 1.4-5.2L5 9.7l5.2-1.8z',
  activity: 'M4 12h4l2.5-6 3.5 12 2.5-6H20',
  people: 'M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z|M2.5 20c.6-3.4 3.2-5.5 6.5-5.5s5.9 2.1 6.5 5.5|M16 4.2a3.5 3.5 0 0 1 0 6.6|M18.5 14.8c1.7.8 2.7 2.5 3 5.2'
};
function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }
function clip(t, n) { t = String(t || '').replace(/\s+/g, ' ').trim(); return t.length <= n ? t : t.slice(0, n - 1).trim() + '…'; }
function len(sec) { sec = Math.round(sec || 0); return sec ? Math.floor(sec / 60) + ':' + ('0' + (sec % 60)).slice(-2) : ''; }
function ago(ts) {
  var d = Date.now() / 1000 - ts;
  if (d < 50) return 'just now';
  if (d < 3600) return Math.round(d / 60) + ' min ago';
  if (d < 86400) return Math.round(d / 3600) + ' h ago';
  if (d < 7 * 86400) return plural(Math.round(d / 86400), 'day') + ' ago';
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
function when(ts) {
  if (!ts) return '';
  var d = new Date(ts * 1000), now = new Date(), t = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  var day = function (x) { return new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime(); };
  var diff = Math.round((day(d) - day(now)) / 86400000);
  if (diff === 0) return 'today ' + t;
  if (diff === 1) return 'tomorrow ' + t;
  if (diff === -1) return 'yesterday ' + t;
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }) + ' ' + t;
}
function big(n) { n = n || 0; return n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'K' : String(n); }
var toastTimer = 0;
function toast(text, bad) {
  var t = document.getElementById('toast');
  t.textContent = text; t.className = 'on' + (bad ? ' bad' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { t.className = ''; }, bad ? 5200 : 3200);
  if (tg && tg.HapticFeedback) { try { tg.HapticFeedback.notificationOccurred(bad ? 'error' : 'success'); } catch (e) {} }
}
function ask(text, yes) {
  if (tg && tg.showConfirm && tg.isVersionAtLeast && tg.isVersionAtLeast('6.2')) tg.showConfirm(text, function (ok) { if (ok) yes(); });
  else if (window.confirm(text)) yes();
}
function face(p, cls) {
  var name = (p && (p.first || p.name)) || '?';
  return h('span', { class: 'face ' + (cls || '') + (p && !isMe(p.id != null ? p.id : p.by) ? ' other' : ''), title: name,
                     text: name.replace(/^@/, '').charAt(0).toUpperCase() || '?' });
}
function isMe(id) { return !!S.me && id === S.me.id; }
function nameOf(id, fallback) {
  if (isMe(id)) return 'You';
  var p = S.people.filter(function (x) { return x.id === id; })[0];
  return (p && p.first) || fallback || 'Someone';
}
function mediaUrl(v, what) { return 'api/videos/' + encodeURIComponent(v.id) + '/' + what + '?m=' + encodeURIComponent(S.media) + (what === 'thumb' ? '&v=' + (v.thumb_v || 0) : ''); }

// --- talking to the PC ---------------------------------------------------------------------------
function Problem(message, status, code) { this.message = message; this.status = status; this.code = code || ''; }
function raw(method, path, body) {
  return fetch(path, { method: method, headers: { 'Content-Type': 'application/json', 'Authorization': 'tma ' + S.token },
                       body: body == null ? undefined : JSON.stringify(body) })
    .then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Problem(data.error || ('The PC answered ' + r.status + '.'), r.status, data.code);
        return data;
      });
    }, function () { throw new Problem("Can't reach the PC. Is ClipMonster's automation running?", 0); });
}
function signIn() {
  var init = tg ? tg.initData : '';
  if (!init) return Promise.reject(new Problem('', 401, 'outside'));
  return raw('POST', 'api/session', { init_data: init }).then(function (d) {
    S.token = d.token; S.media = d.media; takeMe(d);
  });
}
function takeMe(d) { S.me = d.me; S.people = d.people || []; S.pc = d.pc || ''; S.most = d.most || 2; }
function api(method, path, body) {
  return raw(method, path, body).catch(function (e) {
    if (e.code === 'expired') return signIn().then(function () { return raw(method, path, body); });
    throw e;
  });
}
function act(promise, done) {
  if (S.busy) return;
  S.busy = true; paint(true);
  promise.then(function (r) {
    S.busy = false;
    if (r && r.message) toast(r.message);
    if (done) done(r);
    paint(true);
    return refresh();
  }).catch(function (e) {
    S.busy = false; toast(e.message || 'Something went wrong.', true);
    if (e.status === 401) return locked(e);
    paint(true);
    refresh();
  });
}
function same(key, value) {
  var j = JSON.stringify(value);
  if (S.last[key] === j) return true;
  S.last[key] = j; return false;
}
function refresh() {
  var jobs = [api('GET', 'api/videos').then(function (d) { S.videos = d.items || []; }),
              api('GET', 'api/status').then(function (d) { S.status = d; })];
  if (S.tab === 'activity') jobs.push(api('GET', 'api/activity').then(function (d) { S.activity = d; }));
  if (S.tab === 'people') jobs.push(api('GET', 'api/me').then(takeMe));
  if (S.tab === 'make' && S.make === 'sports') {
    jobs.push(api('GET', 'api/sports').then(function (d) { S.sports = d; }));
    loadPlayed(false);
  }
  if (S.tab === 'videos' && S.filter === 'posted') jobs.push(api('GET', 'api/posted').then(function (d) { S.posted = d.items || []; }));
  return Promise.all(jobs).then(function () { paint(); }, function (e) {
    if (e.status === 401) locked(e);
    else if (!S.videos) problem(e.message);
  });
}

// The games of the last 24 hours (the PC reads the scoreboards): asked for when the sports form opens, and again
// every few minutes while it stays open, so a game that just ended can be picked.
function loadPlayed(force) {
  if (S.playedBusy || (!force && S.played && Date.now() - S.playedAt < 180000)) return;
  S.playedBusy = true;
  api('GET', 'api/sports/played').then(function (d) {
    S.played = d; S.playedAt = Date.now(); S.playedBusy = false; paint();
  }, function (e) {
    S.playedBusy = false;
    if (e.status === 401) return locked(e);
    if (!S.played) { S.played = { games: [], leagues: [], problems: [e.message || 'The scoreboards couldn’t be read.'], covered: true }; S.playedAt = Date.now(); }
    paint();
  });
}

// --- screens that aren't the app -----------------------------------------------------------------
function locked(e) {
  clearInterval(timer); closeSheet(true); S.me = null;
  clear(app).appendChild(h('div', { class: 'splash' },
    h('h2', { text: e.code === 'unlinked' ? 'Not linked' : e.code === 'left' ? 'You left' : 'Open ClipMonster again' }),
    h('p', { text: e.message })));
}
function problem(message) {
  clear(app).appendChild(h('div', { class: 'splash' }, h('h2', { text: "Can't open ClipMonster" }), h('p', { text: message }),
    h('button', { class: 'btn primary slim', text: 'Try again', onclick: start })));
}

// --- the shell ---------------------------------------------------------------------------------------
function videosIn(filter) {
  var v = S.videos || [];
  if (filter === 'review') return v.filter(function (x) { return x.state === 'review'; });
  if (filter === 'queue') return v.filter(function (x) { return x.state === 'queued' || x.state === 'posting' || x.state === 'failed'; });
  if (filter === 'library') return v.filter(function (x) { return x.state === 'library'; });
  return S.posted || [];
}
function paint(force) {
  if (!S.me || !S.videos) return;
  var key = [S.tab, S.filter, S.who, S.busy, S.invite, S.videos, S.status, S.people, S.me,
             S.tab === 'activity' ? S.activity : 0, S.filter === 'posted' ? S.posted : 0, S.cats,
             S.tab === 'make' ? [S.make, S.sports, S.sp, S.played, S.playedBusy] : 0];
  // Someone typing isn't interrupted by news from the PC: the screen catches up when they're done.
  var el = document.activeElement;
  var typing = !!el && app.contains(el) && /^(TEXTAREA|SELECT|INPUT)$/.test(el.tagName);
  if ((force || !typing) && !same('shell', key)) {
    var y = window.scrollY;
    var shell = h('div', { class: 'shell' }, top(), S.tab === 'videos' ? videosTab() : S.tab === 'make' ? makeTab() :
                  S.tab === 'activity' ? activityTab() : peopleTab());
    clear(app).appendChild(shell);
    app.appendChild(nav());
    window.scrollTo(0, y);
  }
  if (sheet) paintSheet();
}
function top() {
  var st = S.status || {}, what, dot = 'dot';
  if (st.paused) { what = 'Paused: nothing new is clipped or made'; dot += ' paused'; }
  else if (st.job) what = (st.job.making ? 'Making “' : 'Clipping “') + clip(st.job.title, 60) + '”' + (st.job.stage ? ' · ' + st.job.stage : '');
  else if (st.sports) what = sportsNow(st.sports, 44);
  else if (st.sports_waiting) what = plural(st.sports_waiting, 'sports job') + ' waiting to start';
  else { what = st.doing || 'Nothing being made right now'; dot += ' idle'; }
  var frac = !st.paused && !st.job && st.sports && st.sports.frac != null ? st.sports.frac : null;
  return h('div', { class: 'top' },
    h('div', { class: 'top-row' }, h('h1', { text: 'ClipMonster' }),
      h('div', { class: 'faces', title: S.people.map(function (p) { return p.first; }).join(' and ') },
        S.people.map(function (p) { return face(p); }))),
    h('div', { class: 'statusline' }, h('span', { class: dot }), h('span', { class: 'what', text: what }),
      h('button', { class: 'textbtn', text: st.paused ? 'Resume' : 'Pause', disabled: S.busy, onclick: function () {
        if (st.paused) act(api('POST', 'api/pause', { paused: false }));
        else ask('Pause automation? Nothing new is clipped or made until one of you resumes. Posting carries on.',
                 function () { act(api('POST', 'api/pause', { paused: true })); });
      } })),
    frac != null ? meter(frac) : null);
}
function sportsNow(d, n) {
  var pct = d.frac != null && String(d.stage || '').indexOf('%') < 0 ? ' · ' + Math.round(d.frac * 100) + '%' : '';
  return (d.emoji ? d.emoji + ' ' : '') + d.what + ' “' + clip(d.title, n) + '”' + (d.stage ? ' · ' + d.stage : '') + pct;
}
function meter(frac) {
  var pct = Math.max(0, Math.min(100, Math.round(frac * 100)));
  return h('div', { class: 'meter', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': pct },
    h('i', { style: 'width:' + pct + '%' }));
}
function nav() {
  var review = videosIn('review').length;
  function tab(id, label) {
    return h('button', { class: S.tab === id ? 'on' : '', 'aria-current': S.tab === id ? 'page' : null, onclick: function () {
      S.tab = id; window.scrollTo(0, 0); paint(true); refresh();
      if (id === 'make' && !S.cats) api('GET', 'api/categories').then(function (d) { S.cats = d.items || []; paint(); }, function () {});
    } }, h('span', { class: 'ico' }, svg(ICON[id]), id === 'videos' && review ? h('span', { class: 'badge', text: review }) : null), label);
  }
  return h('nav', { class: 'nav' }, h('div', { class: 'nav-in' }, tab('videos', 'Videos'), tab('make', 'Make'),
           tab('activity', 'Activity'), tab('people', 'People')));
}

// --- videos ----------------------------------------------------------------------------------------
var FILTERS = [['review', 'To review'], ['queue', 'Queue'], ['library', 'Library'], ['posted', 'Posted']];
var EMPTY = {
  review: ['Nothing to review', 'Videos that wait for an OK before they post show up here.'],
  queue: ['The queue is empty', 'Videos OK’d to post wait here for their time.'],
  library: ['Nothing in the library', 'Clips whose category has no TikTok account yet wait here.'],
  posted: ['Nothing posted yet', 'Posts and their numbers show up here.']
};
function videosTab() {
  var items = videosIn(S.filter);
  return [h('div', { class: 'seg', role: 'tablist' }, FILTERS.map(function (f) {
    var n = f[0] === 'posted' ? null : videosIn(f[0]).length;
    return h('button', { class: 'chip' + (S.filter === f[0] ? ' on' : ''), role: 'tab', 'aria-selected': S.filter === f[0], onclick: function () {
      S.filter = f[0]; paint(true); if (f[0] === 'posted') refresh();
    } }, f[1], n ? h('span', { class: 'n', text: n }) : null);
  })),
  S.filter === 'posted' && !S.posted ? h('div', { class: 'empty' }, h('div', { class: 'spin', style: 'margin:0 auto' })) :
  items.length ? h('div', { class: 'list' }, items.map(card)) :
    h('div', { class: 'empty' }, h('b', { text: EMPTY[S.filter][0] }), EMPTY[S.filter][1])];
}
function stateTag(v) {
  if (v.state === 'review') return h('span', { class: 'tag review', text: 'Waits for an OK' });
  if (v.state === 'posting') return h('span', { class: 'tag good', text: 'Posting now' });
  if (v.state === 'failed') return h('span', { class: 'tag bad', text: "Didn't post" });
  if (v.state === 'queued') return h('span', { class: 'tag', text: v.planned_at ? 'Posts ' + when(v.planned_at) : 'In the queue' });
  if (v.state === 'posted') return h('span', { class: 'tag good', text: 'Posted ' + when(v.posted_at) });
  return h('span', { class: 'tag', text: 'Library' });
}
function card(v) {
  var said = v.critiques || [];
  var meta = [v.account_handle, v.category, v.part].filter(Boolean).join(' · ');
  return h('button', { class: 'card', onclick: function () { openVideo(v.id); } },
    h('span', { class: 'thumb' }, v.has_thumb ? h('img', { src: mediaUrl(v, 'thumb'), alt: '', loading: 'lazy' }) : null,
      len(v.duration) ? h('span', { class: 'len', text: len(v.duration) }) : null),
    h('span', { class: 'body' },
      h('span', { class: 'title', text: v.headline }),
      meta ? h('span', { class: 'meta', text: meta }) : null,
      v.stats ? h('span', { class: 'meta', text: big(v.stats.views) + ' views · ' + big(v.stats.likes) + ' likes' }) : null,
      h('span', { class: 'tags' }, stateTag(v),
        v.fix_of ? h('span', { class: 'tag fix', text: 'Made again' }) : null,
        v.fixing ? h('span', { class: 'tag fix', text: 'Being made again' }) : null,
        said.length && !v.fix_of ? h('span', { class: 'tag', text: plural(said.length, 'critique') }) : null)));
}

// --- one video ---------------------------------------------------------------------------------------
function find(id) {
  return (S.videos || []).concat(S.posted || []).filter(function (x) { return x.id === id; })[0] || null;
}
function openVideo(id) {
  var v = find(id);
  if (!v) { toast("That video isn't in the library any more.", true); return; }
  closeSheet(true);
  S.open = id;
  var video = v.exists ? h('video', { src: mediaUrl(v, 'file'), poster: v.has_thumb ? mediaUrl(v, 'thumb') : null, controls: true,
                                      playsinline: true, 'webkit-playsinline': true, preload: 'metadata' })
                       : h('div', { class: 'gone', text: "Its file isn't on the PC any more." });
  sheet = { id: id, bar: h('div', { class: 'bar' }), info: h('div'), acts: h('div'), fix: h('div'), thread: h('div'),
            root: null, text: null };
  sheet.root = h('div', { class: 'sheet', role: 'dialog', 'aria-label': v.headline },
    h('div', { class: 'sheet-in' }, sheet.bar, h('div', { class: 'stage' }, video), sheet.info, sheet.acts, sheet.fix, sheet.thread));
  document.body.appendChild(sheet.root);
  document.body.style.overflow = 'hidden';
  if (tg && tg.BackButton) { try { tg.BackButton.show(); } catch (e) {} }
  S.last.sheet = S.last.sheetfix = null;
  paintSheet();
}
function closeSheet(quiet) {
  if (!sheet) return;
  var vid = sheet.root.querySelector('video');
  if (vid) { try { vid.pause(); vid.removeAttribute('src'); vid.load(); } catch (e) {} }
  document.body.removeChild(sheet.root);
  document.body.style.overflow = '';
  sheet = null; S.open = null;
  if (tg && tg.BackButton) { try { tg.BackButton.hide(); } catch (e) {} }
  if (!quiet) paint();
}
function paintSheet() {
  var v = find(sheet.id);
  if (!v) { closeSheet(); toast('That video is gone from the library.'); return; }
  if (same('sheet', [v, S.busy, S.people])) return;
  var group = videosIn(v.state === 'review' ? 'review' : v.state === 'library' ? 'library' : v.state === 'posted' ? 'posted' : 'queue');
  var at = group.map(function (x) { return x.id; }).indexOf(v.id);
  clear(sheet.bar);
  add(sheet.bar, [h('button', { class: 'back', text: '‹ Back', onclick: function () { closeSheet(); } }),
    h('span', { class: 'where', text: at >= 0 && group.length > 1 ? (at + 1) + ' of ' + group.length : '' }),
    h('button', { class: 'step', text: 'Next ›', disabled: !(at >= 0 && group.length > 1), onclick: function () {
      openVideo(group[(at + 1) % group.length].id);
    } })]);

  var stateLine, cls = 'state';
  if (v.state === 'review') { stateLine = 'Waits for an OK before it posts' + (v.account_handle ? ' to ' + v.account_handle : ''); cls += ' review'; }
  else if (v.state === 'queued') stateLine = (v.planned_at ? 'Posts ' + when(v.planned_at) : 'In the queue') + (v.account_handle ? ' to ' + v.account_handle : '');
  else if (v.state === 'posting') stateLine = 'Posting now…';
  else if (v.state === 'failed') { stateLine = "Didn't post: " + (v.last_error || 'something went wrong'); cls += ' bad'; }
  else if (v.state === 'posted') stateLine = 'Posted ' + when(v.posted_at);
  else stateLine = 'In the library, not queued' + (v.account_handle ? '' : ' (its category has no TikTok account)');
  var bits = [v.account_handle, v.category, v.part, len(v.duration), v.ai_generated ? 'AI-generated, labelled as such' : ''].filter(Boolean);
  var from = v.prompt ? (v.prompt === 'your script' ? 'Made from your own script' : 'Made from “' + clip(v.prompt, 160) + '”')
           : v.source_title ? 'From “' + clip(v.source_title, 90) + '”' + (v.source_channel ? ' (' + clip(v.source_channel, 40) + ')' : '') : '';
  var cap = [v.caption, v.hashtags].filter(Boolean).join(' ');
  var origin = (v.critiques || []).filter(function (c) { return c.fixed === v.id; })[0];
  clear(sheet.info);
  add(sheet.info, [
    origin ? h('div', { class: 'block' }, h('div', { class: 'note' },
      h('div', { text: 'Made again from ' + (isMe(origin.by) ? 'your' : origin.name + '’s') + ' critique of the first version:' }),
      h('div', { class: 'q', text: '“' + origin.text + '”' })),
      find(origin.item) ? h('button', { class: 'textbtn', style: 'margin-top:8px', text: 'Watch the first version ›', onclick: function () { openVideo(origin.item); } }) : null) : null,
    h('div', { class: 'block' }, h('h2', { text: v.headline }),
      bits.length ? h('p', { class: 'sub', text: bits.join(' · ') }) : null,
      h('p', { class: cls, text: stateLine }),
      v.stats ? h('p', { class: 'sub', text: big(v.stats.views) + ' views · ' + big(v.stats.likes) + ' likes · ' + big(v.stats.comments) + ' comments · ' + big(v.stats.shares) + ' shares' }) : null,
      from ? h('p', { class: 'sub', text: from }) : null,
      cap && cap.replace(/\s+/g, ' ') !== String(v.headline).replace(/\s+/g, ' ') ? h('p', { class: 'cap', text: cap }) : null,
      v.warnings && v.warnings.length ? h('p', { class: 'warns', text: '⚠ ' + v.warnings.join('; ') }) : null)]);

  var can = v.can || {}, btns = [];
  if (can.ok) btns.push(h('button', { class: 'btn primary', text: 'OK to post', disabled: S.busy, onclick: function () {
    act(api('POST', 'api/videos/' + v.id + '/ok'));
  } }));
  if (can.retry) btns.push(h('button', { class: 'btn primary', text: 'Try again', disabled: S.busy, onclick: function () {
    act(api('POST', 'api/videos/' + v.id + '/retry'));
  } }));
  if (can.post_now) btns.push(h('button', { class: 'btn', text: 'Post now', disabled: S.busy, onclick: function () {
    ask('Post “' + clip(v.headline, 70) + '” to ' + v.account_handle + ' right now?', function () { act(api('POST', 'api/videos/' + v.id + '/post-now')); });
  } }));
  if (can['delete']) btns.push(h('button', { class: 'btn danger', text: 'Delete', disabled: S.busy, onclick: function () {
    ask('Delete “' + clip(v.headline, 70) + '” from the PC?', function () {
      act(api('POST', 'api/videos/' + v.id + '/delete'), function () { closeSheet(true); });
    });
  } }));
  if (v.tiktok_url) btns.push(h('button', { class: 'btn', text: 'Open on TikTok', onclick: function () {
    if (tg && tg.openLink) tg.openLink(v.tiktok_url); else window.open(v.tiktok_url, '_blank', 'noopener');
  } }));
  clear(sheet.acts);
  if (btns.length) add(sheet.acts, h('div', { class: 'acts' }, btns));

  // What's wrong with it? The AI makes it again. (Left alone while it's being typed in.)
  if (!same('sheetfix', [v.id, v.state, v.fixing, v.fixable, v.ai_generated, S.busy])) paintFix(v);
  paintThread(v);
}
function paintFix(v) {
  var keep = sheet.text ? sheet.text.value : (S.draft[v.id] || '');
  clear(sheet.fix); sheet.text = null;
  if (v.state !== 'posted' && v.state !== 'posting') {
    var box = h('div', { class: 'block' }, h('h3', { text: 'What should be different?' }));
    if (v.fixing) {
      box.appendChild(h('p', { class: 'sub', style: 'margin:0', text: 'It’s being made again from the critique below. When the new version is ready it waits for an OK beside this one.' }));
    } else if (v.fixable) {
      var count = h('span', { class: 'count', text: keep.length + ' / 2000' });
      sheet.text = h('textarea', { maxlength: 2000, placeholder: 'Say it in your own words. The hook is flat, open with the twist. The third picture doesn’t fit what’s said.',
                                   'aria-label': 'What should be different about this video', oninput: function () {
        S.draft[v.id] = sheet.text.value; count.textContent = sheet.text.value.length + ' / 2000'; send.disabled = S.busy || sheet.text.value.trim().length < 3;
      } });
      sheet.text.value = keep;
      var send = h('button', { class: 'btn primary wide', text: 'Make it again', disabled: S.busy || keep.trim().length < 3, onclick: function () {
        var text = sheet.text.value.trim();
        act(api('POST', 'api/videos/' + v.id + '/critique', { text: text }), function () {
          S.draft[v.id] = ''; if (sheet && sheet.text) sheet.text.value = ''; S.last.sheet = S.last.sheetfix = null;
        });
      } });
      add(box, [sheet.text, h('div', { class: 'hints' }, ['Stronger hook', 'Slower pace', 'Shorter', 'Pictures don’t match', 'Clearer ending', 'Less hype'].map(function (t) {
        return h('button', { class: 'hint', text: t, onclick: function () {
          var cur = sheet.text.value.trim();
          sheet.text.value = (cur ? cur.replace(/[.\s]*$/, '. ') : '') + t + '.';
          sheet.text.dispatchEvent(new Event('input')); sheet.text.focus();
        } });
      })), send, h('p', { class: 'small' }, 'The AI rewrites what you name and keeps the rest. The new version waits for an OK beside this one.', count)]);
    } else {
      box.appendChild(h('p', { class: 'sub', style: 'margin:0', text: v.ai_generated
        ? 'This one can’t be made again: its storyboard isn’t beside it on the PC any more.'
        : 'A clip is cut from someone’s video as it is, so there’s nothing for the AI to rewrite. Delete it if it’s no good.' }));
    }
    sheet.fix.appendChild(box);
  }
}
function paintThread(v) {
  var said = (v.critiques || []).filter(function (c) { return c.item === v.id; });
  clear(sheet.thread);
  if (said.length) {
    sheet.thread.appendChild(h('div', { class: 'block' }, h('h3', { text: 'What was said' }), said.map(function (c) {
      var st, cl = 'st';
      if (c.status === 'queued') { st = 'Waiting to be made again'; cl += ' going'; }
      else if (c.status === 'working') { st = 'Being made again' + (c.stage ? ': ' + c.stage : '…'); cl += ' going'; }
      else if (c.status === 'done') { st = 'Made again'; cl += ' good'; }
      else { st = 'Couldn’t be made again' + (c.error ? ': ' + c.error : ''); cl += ' bad'; }
      return h('div', { class: 'said' }, face({ id: c.by, first: c.name }, 'sm'),
        h('div', { class: 'col' },
          h('div', { class: 'who' }, nameOf(c.by, c.name), h('span', { class: 'when', text: ago(c.at) })),
          h('div', { class: 'txt', text: c.text }),
          h('div', { class: cl, text: st }),
          c.status === 'done' && c.fixed && find(c.fixed) ? h('button', { class: 'textbtn', text: 'Watch the new version ›', onclick: function () { openVideo(c.fixed); } }) : null));
    })));
  }
}

// --- make --------------------------------------------------------------------------------------------
function makeTab() {
  var kinds = h('div', { class: 'seg', role: 'tablist' }, [['video', 'A video'], ['sports', 'Sports highlights']].map(function (k) {
    return h('button', { class: 'chip' + (S.make === k[0] ? ' on' : ''), role: 'tab', 'aria-selected': S.make === k[0], onclick: function () {
      S.make = k[0]; paint(true); if (k[0] === 'sports') refresh();
    } }, k[1]);
  }));
  return [kinds].concat(S.make === 'sports' ? sportsForm() : videoForm());
}
function picks(label, options, value, set) {
  return h('div', { class: 'field' }, h('label', { text: label }), h('div', { class: 'picks', role: 'group', 'aria-label': label }, options.map(function (o) {
    return h('button', { class: 'pick' + (String(value) === String(o[0]) ? ' on' : ''), 'aria-pressed': String(value) === String(o[0]) ? 'true' : 'false',
                         onclick: function () { set(o[0]); paint(true); } }, o[1]);
  })));
}
function onPC() {
  var st = S.status || {}, d = st.sports;
  if (!d && !st.sports_waiting) return null;
  return h('div', { class: 'block' }, h('h3', { text: 'On the PC now' }),
    d ? h('p', { text: sportsNow(d, 80) }) : h('p', { text: plural(st.sports_waiting, 'sports job') + ' waiting to start' }),
    d && d.frac != null ? meter(d.frac) : null,
    d && (d.by || d.waiting) ? h('p', { class: 'sub', text: [d.by ? d.by + ' asked for it' : '', d.waiting ? plural(d.waiting, 'more job') + ' waiting' : ''].filter(Boolean).join(' · ') }) : null);
}
function wantPicks(d, f) {
  var need = (d.requires || {}).min_seconds;
  return [picks('Make', [['reel', 'A recap reel'], ['both', 'Reel and single plays'], ['clips', 'Single plays']], f.want, function (v) { f.want = v; }),
    f.want !== 'reel' && need ? h('p', { class: 'small warnline', text: 'Single plays run 15 to 40 seconds, and a request’s videos have to be over ' + Math.round(need - 1) + ' s: they’ll wait for an OK instead of posting.' }) : null];
}
function catPick(d, f, sportId) {
  var cats = d.categories || [];
  var own = sportId ? cats.filter(function (c) { return c.sport === sportId; }) : [];
  var pickCat = h('select', { 'aria-label': 'Category', onchange: function () { f.cat = pickCat.value; } },
    h('option', { value: '', text: own.length === 1 ? 'Automatic: ' + own[0].name : 'No category (kept in the Library)' }),
    cats.map(function (c) { return h('option', { value: c.id, selected: f.cat === c.id, text: (c.sport ? '🏆 ' : '') + c.name + (c.account ? ' · ' + c.account : ' (no account)') }); }));
  return h('div', { class: 'field' }, h('label', { text: 'For' }), pickCat);
}
function sportsForm() {
  var d = S.sports, f = S.sp;
  if (!d) return [h('div', { class: 'empty' }, h('div', { class: 'spin', style: 'margin:0 auto' }))];
  var open = (d.orders || []).filter(function (o) { return o.status === 'queued' || o.status === 'searching' || o.status === 'working'; });
  var full = open.length >= ((d.most || {}).open || 4);
  var how = h('div', { class: 'picks two', role: 'tablist' }, [['pick', 'Pick a game'], ['say', 'Describe it']].map(function (k) {
    return h('button', { class: 'pick' + (f.how === k[0] ? ' on' : ''), role: 'tab', 'aria-selected': f.how === k[0], onclick: function () { f.how = k[0]; paint(true); } }, k[1]);
  }));
  var form = f.how === 'pick' ? pickForm(d, f, full) : sayForm(d, f, full);
  var orders = d.orders || [];
  return [onPC(), h('div', { class: 'block' }, how, form,
      full ? h('p', { class: 'small', text: plural(open.length, 'request') + ' are waiting or under way: wait for one to finish, or cancel one.' }) : null,
      h('p', { class: 'small', text: 'Automation finds the game’s highlights on YouTube, picks its best plays and makes them on the PC, with your name on the request. What it makes is checked before it’s handed over: over a minute, upright, with sound. League footage often gets copyright claims on TikTok.' })),
    orders.length ? h('div', { class: 'block' }, h('h3', { text: 'Sports requests' }), orders.map(orderRow)) : null];
}
// Pick a game: the last 24 hours from the scoreboards, by drop-down or from the list under it.
function pickForm(d, f, full) {
  var p = S.played;
  if (!p) return h('div', { class: 'empty' }, h('div', { class: 'spin', style: 'margin:0 auto 10px' }), 'Reading the scoreboards…');
  var all = p.games || [];
  var sportsIn = (d.sports || []).filter(function (x) { return all.some(function (g) { return g.sport === x.id; }); });
  if (f.psport && !sportsIn.some(function (x) { return x.id === f.psport; })) f.psport = '';
  var bySport = all.filter(function (g) { return !f.psport || g.sport === f.psport; });
  var leagues = (p.leagues || []).filter(function (r) { return !f.psport || r.sport === f.psport; });
  if (f.pleague && !leagues.some(function (r) { return r.league === f.pleague; })) f.pleague = '';
  var games = bySport.filter(function (g) { return !f.pleague || g.league === f.pleague; });
  var chosen = all.filter(function (g) { return g.id === f.game && g.ready; })[0] || null;
  if (chosen && games.indexOf(chosen) < 0) chosen = null;
  if (!chosen) f.game = '';
  var pickSport = h('select', { 'aria-label': 'Sport', onchange: function () { f.psport = pickSport.value; f.pleague = ''; paint(true); } },
    h('option', { value: '', text: 'All sports' }),
    sportsIn.map(function (x) { return h('option', { value: x.id, selected: f.psport === x.id, text: x.emoji + '  ' + x.label }); }));
  var pickLeague = h('select', { 'aria-label': 'League', onchange: function () { f.pleague = pickLeague.value; paint(true); } },
    h('option', { value: '', text: 'All leagues' }),
    leagues.map(function (r) { return h('option', { value: r.league, selected: f.pleague === r.league, text: r.league + ' (' + r.games + ')' }); }));
  var pickGame = h('select', { 'aria-label': 'Game', onchange: function () { f.game = pickGame.value; paint(true); } },
    h('option', { value: '', text: games.length ? 'Pick a game…' : 'No games here in the last 24 hours' }),
    games.map(function (g) {
      return h('option', { value: g.id, selected: f.game === g.id, disabled: !g.ready,
                           text: (f.pleague ? '' : g.league + ': ') + g.line + ' · ' + g.when });
    }));
  var shown = f.all ? games : games.slice(0, 12);
  var list = h('div', { class: 'games', role: 'listbox', 'aria-label': 'Games played in the last 24 hours' }, shown.map(function (g) {
    return h('button', { class: 'game' + (f.game === g.id ? ' on' : '') + (g.ready ? '' : ' wait'), role: 'option', 'aria-selected': f.game === g.id ? 'true' : 'false',
                         disabled: !g.ready, onclick: function () { f.game = f.game === g.id ? '' : g.id; paint(true); } },
      h('span', { class: 'em', text: g.emoji }),
      h('span', { class: 'col' }, h('span', { class: 'line', text: g.line }), h('span', { class: 'lg', text: g.league })),
      h('span', { class: 'when' + (g.state === 'live' ? ' live' : ''), text: g.when }));
  }));
  var go = h('button', { class: 'btn primary wide', text: chosen ? (f.want === 'clips' ? 'Make its best plays' : 'Make its reel') : 'Pick a game first',
                         disabled: S.busy || full || !chosen, onclick: function () {
    var body = { game: f.game, want: f.want, plays: f.plays };
    if (f.cat) body.category = f.cat;
    act(api('POST', 'api/sports/order', body), function () { f.game = ''; });
  } });
  var finals = games.filter(function (g) { return g.ready; }).length;
  return h('div', {},
    h('div', { class: 'field pair' },
      h('div', {}, h('label', { text: 'Sport' }), pickSport), h('div', {}, h('label', { text: 'League' }), pickLeague)),
    h('div', { class: 'field' }, h('label', { text: 'Game' }), pickGame),
    h('div', { class: 'field' },
      h('div', { class: 'lbl' }, 'All games, last 24 hours · ' + plural(finals, 'finished game'),
        h('button', { class: 'textbtn inl', text: S.playedBusy ? 'Loading…' : 'Refresh', disabled: S.playedBusy, onclick: function () { loadPlayed(true); paint(true); } })),
      games.length ? list : h('p', { class: 'sub', text: all.length ? 'None for this sport and league.' : p.covered === false ? 'The scoreboards don’t list this sport.' : 'No games on the scoreboards in the last 24 hours.' }),
      games.length > shown.length ? h('button', { class: 'textbtn', text: 'Show all ' + games.length, onclick: function () { f.all = true; paint(true); } }) : null,
      (p.problems || []).length ? h('p', { class: 'small warnline', text: 'Not read: ' + p.problems.slice(0, 3).join(' ') }) : null),
    wantPicks(d, f),
    f.want !== 'reel' ? picks('Single plays', [3, 5, 7].map(function (n) { return [n, 'Top ' + n]; }), f.plays, function (v) { f.plays = v; }) : null,
    catPick(d, f, chosen ? chosen.sport : f.psport),
    h('div', { class: 'gap' }), go,
    h('p', { class: 'small', text: 'A game still being played can be picked once it’s over. The scoreboards cover soccer, basketball, American football, hockey and baseball; for anything else, describe it.' }));
}
// Describe it: any sport, any league, an older game.
function sayForm(d, f, full) {
  var sport = d.sports.filter(function (x) { return x.id === f.sport; })[0] || null;
  var pickSport = h('select', { 'aria-label': 'Sport', onchange: function () { f.sport = pickSport.value; f.league = ''; paint(true); } },
    h('option', { value: '', text: 'Any sport' }),
    d.sports.filter(function (x) { return x.id !== 'any'; }).map(function (x) {
      return h('option', { value: x.id, selected: f.sport === x.id, text: x.emoji + '  ' + x.label });
    }));
  var known = sport ? sport.leagues : [];
  var custom = !!f.league && known.indexOf(f.league) < 0;
  var pickLeague = h('select', { 'aria-label': 'League', onchange: function () {
      if (pickLeague.value === '+') { f.league = ''; f.other = true; } else { f.league = pickLeague.value; f.other = false; }
      paint(true);
    } },
    h('option', { value: '', text: 'Any league' }),
    known.map(function (name) { return h('option', { value: name, selected: f.league === name, text: name }); }),
    h('option', { value: '+', selected: !!f.other || custom, text: 'Another…' }));
  var league = h('input', { type: 'text', maxlength: 60, placeholder: 'The league or the event', 'aria-label': 'League', autocomplete: 'off',
                            oninput: function () { f.league = league.value; ready(); } });
  league.value = f.league;
  var teams = h('input', { type: 'text', maxlength: 200, placeholder: 'Lakers vs Celtics', 'aria-label': 'Teams', autocomplete: 'off',
                           oninput: function () { f.teams = teams.value; ready(); } });
  teams.value = f.teams;
  function can() { return !S.busy && !full && !!(f.sport || f.league.trim() || f.teams.trim()); }
  function ready() { go.disabled = !can(); }
  var go = h('button', { class: 'btn primary wide', text: f.want === 'clips' ? 'Make the best plays' : 'Make the reel', disabled: !can(), onclick: function () {
    var body = { sport: f.sport, league: f.league.trim(), teams: f.teams.trim(), since: f.since, want: f.want, games: f.games, plays: f.plays };
    if (f.cat) body.category = f.cat;
    act(api('POST', 'api/sports/order', body), function () { f.teams = ''; });
    teams.blur(); league.blur();
  } });
  var most = d.most || { games: 3, plays: 7 };
  return h('div', {},
    h('div', { class: 'field pair' },
      h('div', {}, h('label', { text: 'Sport' }), pickSport), h('div', {}, h('label', { text: 'League' }), pickLeague)),
    f.other || custom || !known.length ? h('div', { class: 'field' }, h('label', { text: 'League or event' }), league) : null,
    h('div', { class: 'field' }, h('label', { text: 'Teams (leave empty for the newest games)' }), teams),
    picks('Played in the last', (d.since || []).filter(function (x) { return x.id !== 'any'; }).map(function (x) { return [x.id, x.label]; }), f.since, function (v) { f.since = v; }),
    wantPicks(d, f),
    f.want !== 'reel' ? picks('Single plays per game', [3, 5, 7].filter(function (n) { return n <= most.plays; }).map(function (n) { return [n, 'Top ' + n]; }), f.plays, function (v) { f.plays = v; }) : null,
    picks('Games', [1, 2, 3].filter(function (n) { return n <= most.games; }).map(function (n) { return [n, n === 1 ? 'The newest' : 'Newest ' + n]; }), f.games, function (v) { f.games = v; }),
    catPick(d, f, sport ? sport.id : ''),
    h('div', { class: 'gap' }), go);
}
function orderRow(o) {
  var open = o.status === 'queued' || o.status === 'searching' || o.status === 'working';
  var made = (o.steps || []).filter(function (s) { return s.status === 'done' && s.kind !== 'highlights'; });
  var now = open ? h('div', { class: 'now', text: o.doing || 'Waiting its turn on the PC' })
    : o.status === 'done' ? h('div', { class: 'now good' }, made.length ? ['Ready under Videos', h('ul', {}, made.map(function (s) { return h('li', { text: s.said || s.kind }); }))] : (o.message || 'Done'))
    : o.status === 'cancelled' ? h('div', { class: 'now', text: 'Cancelled' })
    : h('div', { class: 'now bad', text: 'Couldn’t be done: ' + (o.error || 'it failed') });
  var rv = o.review || {}, check = null;
  if (o.status === 'done' && rv.checked) {
    check = rv.held ? h('div', { class: 'now warnline', text: '⏸ ' + (rv.held === 1 ? '1 video is' : rv.held + ' videos are') + ' under the minimums and ' + (rv.held === 1 ? 'waits' : 'wait') + ' for an OK: ' + (rv.why || []).join('; ') })
      : h('div', { class: 'now good', text: '✓ Checked: ' + (rv.checked === 1 ? 'it meets' : 'all ' + rv.checked + ' meet') + ' the minimums' });
  }
  return h('div', { class: 'order' },
    h('div', { class: 'head' }, face({ id: o.by_id, first: o.by || '?' }, 'sm'), h('div', { class: 'col' },
      h('div', { class: 'title', text: (o.emoji ? o.emoji + ' ' : '') + o.title }),
      h('div', { class: 'who', text: (o.line ? o.line + ' · ' : '') + nameOf(o.by_id, o.by || 'Someone') + ' · ' + ago(o.created) + (o.category ? ' · for ' + o.category : '') })),
      open ? h('button', { class: 'btn danger slim', text: 'Cancel', disabled: S.busy, onclick: function () {
        ask('Stop “' + clip(o.title, 70) + '”?', function () { act(api('POST', 'api/sports/orders/' + o.id + '/cancel')); });
      } }) : null),
    now,
    open && o.frac != null ? meter(o.frac) : null,
    check,
    open && (o.notes || []).length ? h('ul', { class: 'notes' }, o.notes.map(function (n) { return h('li', { text: n }); })) : null,
    o.status === 'done' && o.error ? h('div', { class: 'now bad', text: o.error }) : null);
}
function videoForm() {
  var st = S.status || {}, q = st.queue || {};
  var idea = h('textarea', { maxlength: 1000, placeholder: 'The real story of the Bermuda Triangle', 'aria-label': 'What the video is about',
                             oninput: function () { S.idea = idea.value; go.disabled = S.busy || !idea.value.trim(); } });
  idea.value = S.idea;
  var pick = h('select', { 'aria-label': 'Category', onchange: function () { S.cat = pick.value; } },
    h('option', { value: '', text: 'No category' }),
    (S.cats || []).map(function (c) {
      return h('option', { value: c.id, selected: S.cat === c.id, text: c.name + (c.paused ? ' (paused)' : c.account ? ' · ' + c.account : ' (no account)') });
    }));
  var go = h('button', { class: 'btn primary wide', text: 'Make this video', disabled: S.busy || !S.idea.trim(), onclick: function () {
    act(api('POST', 'api/make', { idea: idea.value.trim(), category: pick.value }), function () { S.idea = ''; S.last.shell = null; });
    idea.blur();
  } });
  var next = (q.next || []).filter(function (j) { return j.making; });
  return [h('div', { class: 'block' }, h('h3', { text: 'A new video' }), idea, h('div', { class: 'gap' }), pick, h('div', { class: 'gap' }), go,
    h('p', { class: 'small', text: 'The AI writes it, voices it and cuts it on the PC. It shows up under Videos with your name on it.' })),
    h('div', { class: 'block' }, h('h3', { text: 'On the PC now' }),
      h('p', { text: st.job ? (st.job.making ? 'Making “' : 'Clipping “') + clip(st.job.title, 80) + '”' : st.paused ? 'Paused' : st.sports ? sportsNow(st.sports, 80) : 'Nothing right now' }),
      st.job && st.job.stage ? h('p', { class: 'sub', text: st.job.stage }) : null,
      h('p', { class: 'sub', text: plural(q.to_make || 0, 'video') + ' to make · ' + plural(q.to_clip || 0, 'video') + ' to clip' }),
      next.length ? h('p', { class: 'sub', text: 'Next: ' + next.slice(0, 3).map(function (j) { return '“' + clip(j.title, 40) + '”'; }).join(', ') }) : null)];
}

// --- activity ----------------------------------------------------------------------------------------
var DID = {
  ok: ['OK’d', ' to post'], 'delete': ['deleted', ''], post_now: ['posted', ' right away'], retry: ['queued', ' to post again'],
  critique: ['asked for', ' to be made again'], make: ['asked for a video about', ''], clip: ['asked to clip', ''],
  request: ['asked for', ''], sports: ['asked for sports highlights:', ''], cancel: ['cancelled the request for', ''], pause: ['paused automation', ''],
  resume: ['resumed automation', ''], join: ['joined', ''], leave: ['left', ''], remove: ['removed', '']
};
function activityTab() {
  var a = S.activity;
  if (!a) return h('div', { class: 'empty' }, h('div', { class: 'spin', style: 'margin:0 auto' }));
  var other = S.people.filter(function (p) { return !p.you; })[0];
  var chips = [['all', 'Everyone'], ['me', 'You']].concat(other ? [['other', other.first]] : []);
  var items = a.items.filter(function (x) { return S.who === 'all' || (S.who === 'me') === !!x.mine; });
  var going = (a.going || []).filter(function (c) { return S.who === 'all' || (S.who === 'me') === isMe(c.by); });
  return [h('div', { class: 'seg' }, chips.map(function (c) {
    return h('button', { class: 'chip' + (S.who === c[0] ? ' on' : ''), onclick: function () { S.who = c[0]; paint(true); } }, c[1]);
  })),
  going.length ? h('div', { class: 'block', style: 'margin-top:0' }, h('h3', { text: 'Being made again' }), going.map(function (c) {
    return h('div', { class: 'act' }, face({ id: c.by, first: c.name }, 'sm'), h('div', { class: 'col' },
      h('div', {}, h('b', { text: nameOf(c.by, c.name) }), ': “' + clip(c.title, 60) + '”'),
      h('div', { class: 'q', text: '“' + clip(c.text, 160) + '”' }),
      h('div', { class: 'when', text: c.status === 'working' ? (c.stage || 'Being made now') : 'Waiting its turn' })));
  })) : null,
  items.length ? h('div', { class: 'block', style: going.length ? '' : 'margin-top:0' }, items.map(function (x) {
    var d = DID[x.what] || [x.what, ''], title = x.title;
    var quoted = title && ['clip', 'request', 'sports', 'cancel', 'remove'].indexOf(x.what) < 0;
    return h('div', { class: 'act' }, face({ id: x.by, first: x.name }, 'sm'), h('div', { class: 'col' },
      h('div', {}, h('b', { text: nameOf(x.by, x.name) }), ' ' + d[0] + (title ? ' ' + (quoted ? '“' + clip(title, 70) + '”' : title) : '') + d[1]),
      x.text ? h('div', { class: 'q', text: '“' + clip(x.text, 200) + '”' }) : null,
      h('div', { class: 'when', text: ago(x.at) })));
  })) : h('div', { class: 'empty' }, h('b', { text: 'Nothing yet' }), 'What each of you asks for, says about a video and does shows up here, with your names.')];
}

// --- people ------------------------------------------------------------------------------------------
function peopleTab() {
  var mine = S.me.owner, full = S.people.length >= (S.most || 2), inv = S.invite;
  var out = [h('div', { class: 'block', style: 'margin-top:0' }, h('h3', { text: 'Who shares this bot' }), S.people.map(function (p) {
    return h('div', { class: 'row' }, face(p, 'lg'), h('div', { class: 'col' },
      h('div', { class: 'name', text: p.name + (p.you ? ' (you)' : '') }),
      h('div', { class: 'sub', text: (p.owner ? 'Linked first' : 'Invited') + (p.since ? ' · since ' + new Date(p.since * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '') })),
      mine && !p.you ? h('button', { class: 'btn danger slim', text: 'Remove', disabled: S.busy, onclick: function () {
        ask('Remove ' + p.first + '? They can’t use this bot until they’re invited again.', function () { act(api('POST', 'api/people/remove', { id: p.id })); });
      } }) : null);
  }), h('p', { class: 'small', text: 'Two people at most. You both see every video. What each of you asks for, says about a video and does carries your name, and the other is told. The person linked first invites and removes.' }))];
  if (mine && !full) {
    out.push(h('div', { class: 'block' }, h('h3', { text: 'Invite one person' }),
      inv ? [h('p', { text: 'Send them this link. It works once, for ' + inv.minutes + ' minutes.' }),
             h('div', { class: 'code', text: inv.code }),
             h('div', { class: 'acts', style: 'margin:0' },
               h('button', { class: 'btn primary', text: 'Send in Telegram', onclick: function () {
                 var url = 'https://t.me/share/url?url=' + encodeURIComponent(inv.link) + '&text=' + encodeURIComponent('Join my ClipMonster bot');
                 if (tg && tg.openTelegramLink) tg.openTelegramLink(url); else window.open(url, '_blank', 'noopener');
               } }),
               h('button', { class: 'btn', text: 'Copy link', onclick: function () {
                 (navigator.clipboard ? navigator.clipboard.writeText(inv.link) : Promise.reject()).then(function () { toast('Copied.'); }, function () { toast(inv.link); });
               } })),
             h('p', { class: 'small', text: 'Or they open @' + inv.bot + ' and send it the code above.' })]
          : [h('p', { class: 'sub', style: 'margin:0 0 12px', text: 'They get the same videos and their own name on what they do.' }),
             h('button', { class: 'btn primary wide', text: 'Make an invite', disabled: S.busy, onclick: function () {
               act(api('POST', 'api/people/invite'), function (r) { S.invite = r; });
             } })]));
  } else if (!mine && !full) {
    out.push(h('div', { class: 'pad' }, h('p', { class: 'small', text: 'Only the person linked first can invite someone.' })));
  }
  out.push(h('div', { class: 'acts' }, h('button', { class: 'btn danger', text: 'Leave this bot', disabled: S.busy, onclick: function () {
    ask('Leave this bot? You’ll need a new invite, or the code from the PC, to use it again.', function () {
      api('POST', 'api/people/leave').then(function (r) { locked({ code: 'left', message: r.message }); }, function (e) { toast(e.message, true); });
    });
  } })));
  out.push(h('div', { class: 'pad' }, h('p', { class: 'small', text: 'ClipMonster on ' + (S.pc || 'your PC') })));
  return out;
}

// --- start -------------------------------------------------------------------------------------------
function start() {
  clear(app).appendChild(h('div', { class: 'splash' }, h('div', { class: 'spin' }), h('p', { text: 'Opening ClipMonster…' })));
  signIn().then(function () {
    return refresh();
  }).then(function () {
    if (!S.me || !S.videos) return;
    if (S.invite && S.people.length > 1) S.invite = null;
    var want = new URLSearchParams(window.location.search).get('v') || (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';
    if (want && find(want)) openVideo(want);
    else if (!videosIn('review').length && videosIn('queue').length) { S.filter = 'queue'; paint(); }
    clearInterval(timer);
    timer = setInterval(function () { if (!document.hidden && !S.busy) refresh().then(function () { if (S.invite && S.people.length > 1) { S.invite = null; paint(); } }); }, 8000);
  }).catch(function (e) {
    if (e.code === 'outside') {
      clear(app).appendChild(h('div', { class: 'splash' }, h('h2', { text: 'Open this in Telegram' }),
        h('p', { text: 'This is ClipMonster’s app for Telegram. Open your ClipMonster bot’s chat and tap the ClipMonster button left of the message box.' })));
    } else if (e.status === 401) locked(e);
    else problem(e.message);
  });
}
if (tg) {
  try { tg.ready(); tg.expand(); } catch (e) {}
  if (tg.BackButton) { try { tg.BackButton.onClick(function () { closeSheet(); }); } catch (e) {} }
}
document.addEventListener('visibilitychange', function () { if (!document.hidden && S.me) refresh(); });
window.__clipmonster = { state: S, open: openVideo };
start();
})();
"""
