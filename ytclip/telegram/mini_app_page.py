"""The Mini App's page: what Telegram shows when the bot's ClipMonster button is opened (mini_app.py serves it).

Three files' worth of text, kept here so they ship inside the program with nothing to install: the page, its
style and its script. The page holds nothing private: it asks the server (mini_app.py's /api/...) for everything,
signed in by what Telegram hands it. Colours come from the person's own Telegram theme, light or dark.

What it does: the videos waiting to post (to review, queued, in the library) and the ones posted; a video's own
screen to watch it, OK it, post it now or delete it, and to say what's wrong with it so the AI makes it again;
what each of the two people sharing the bot said and did, by name; asking for a new video; and who shares the
bot (invite, remove, leave).

Sports clips and reels asked for here are downloads (Videos → Downloads): the full file saved to the phone, or
sent into the chat, and nothing queued to post. Each opens in the editor (openEditor): the plays of a reel
trimmed against the game's own footage, reordered, dropped or added, its words, narrator, music and framing;
a single clip's cut, words, captions and shape. The page sends the whole draft; the PC says what changed and
cuts the new version (sportsdesk/editor.py).
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
.tag.ver { color: var(--btn-text, #fff); background: var(--link); }
.vers { display: flex; flex-direction: column; gap: 8px; }
.vrow { display: flex; gap: 10px; align-items: center; padding: 10px 12px; border: 1px solid var(--line); border-radius: 12px; }
.vrow.on { border-color: var(--link); }
.vrow .col { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.vrow .name { font-weight: 700; font-size: 15px; }
.vrow .why { color: var(--hint); font-size: 13px; margin-top: 1px; }
.vrow .btn { flex: none; }
.moment { padding: 10px 0; border-top: 1px solid var(--line); }
.moment .lab { font-weight: 600; font-size: 14px; }
.moment .at { color: var(--hint); font-size: 12px; margin-top: 1px; }
.moment .q { font-style: italic; font-size: 14px; margin-top: 4px; overflow-wrap: anywhere; }
.moment .acts { margin: 8px 0 0; }
.ways { margin: 8px 0 0 18px; padding: 0; font-size: 13px; color: var(--hint); }
.ways li { margin: 2px 0; }
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
.picks.two { margin-bottom: 14px; flex-wrap: nowrap; }
.picks.two .pick { flex: 1; text-align: center; padding-left: 4px; padding-right: 4px; white-space: nowrap; font-size: 14px; }
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
.acts.wrap { flex-wrap: wrap; }
.acts.wrap .btn { flex: 1 1 30%; }
.small.pad { padding: 0 16px; }
.sheet.editor { z-index: 12; display: flex; flex-direction: column; }
.sheet.editor .sheet-in { flex: 1 0 auto; width: 100%; padding-bottom: 12px; }
.ed-stage video { max-height: 30vh; }
.ed-stage.pin { position: sticky; top: 54px; z-index: 2; box-shadow: 0 1px 0 var(--line, rgba(128,128,128,.25)); }
.ed-stage .gone { padding: 26px 22px; }
.ed-foot { position: sticky; bottom: 0; z-index: 3; display: flex; gap: 12px; align-items: center; background: var(--bg);
  border-top: 1px solid var(--line); padding: 10px 14px calc(10px + var(--safe-b)); }
.ed-foot .sum { flex: 1; min-width: 0; font-weight: 700; font-size: 15px; }
.ed-foot .sum small { display: block; font-weight: 400; font-size: 12px; color: var(--hint); overflow-wrap: anywhere; }
.ed-foot .sum.bad small { color: var(--danger); }
.play { padding: 10px 0; border-top: 1px solid var(--line); }
.play:first-of-type { border-top: 0; padding-top: 0; }
.play .head { display: flex; align-items: center; gap: 8px; }
.play .num { flex: none; width: 24px; height: 24px; border-radius: 50%; background: var(--bg2); color: var(--hint);
  font-size: 12px; font-weight: 700; display: grid; place-items: center; }
.play.on .num { background: var(--btn); color: var(--btn-text); }
.play .name { flex: 1; min-width: 0; text-align: left; min-height: 40px; }
.play .name b { display: block; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.play .name span { display: block; font-size: 12px; color: var(--hint); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.icon { flex: none; width: 36px; height: 36px; border-radius: 10px; background: var(--bg2); color: var(--text);
  font-size: 13px; display: grid; place-items: center; }
.icon.danger { color: var(--danger); }
.trim { margin-top: 10px; padding: 12px; background: var(--bg2); border-radius: 12px; }
.trim .line { display: flex; align-items: center; gap: 6px; margin-top: 10px; }
.trim .line:first-child { margin-top: 0; }
.trim .line .lbl { flex: none; width: 38px; font-size: 13px; color: var(--hint); }
.trim .line .t { flex: 1; min-width: 0; text-align: center; font: 600 15px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.trim .line .btn { background: var(--bg); padding-left: 8px; padding-right: 8px; flex: 1; white-space: nowrap; }
.trim .line .btn.primary { background: var(--btn); }
.trim .textbtn { margin-top: 8px; }
.nudge { flex: none; min-width: 42px; height: 36px; border-radius: 9px; background: var(--bg); color: var(--link);
  font-size: 13px; font-weight: 600; }
input[type=range] { width: 100%; margin: 8px 0 0; height: 28px; accent-color: var(--btn); }
.play input[type=text], .trim + .field input[type=text] { background: var(--bg2); }
.ends { display: flex; justify-content: space-between; font-size: 12px; color: var(--hint); }
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
          draft: {}, idea: '', cat: 'auto', last: {}, make: 'video', sports: null,
          sp: { sport: '', league: '', teams: '', since: 'week', want: 'reel', games: 1, plays: 5, cat: '',
                how: 'pick', psport: '', pleague: '', game: '', all: false, query: '', fgame: '', deliver: 'download' },
          secs: 0, qcat: {}, catsAsked: false,
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
  if (filter === 'download') return v.filter(function (x) { return x.state === 'download'; });
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
var FILTERS = [['review', 'To review'], ['download', 'Downloads'], ['queue', 'Queue'], ['library', 'Library'], ['posted', 'Posted']];
var EMPTY = {
  review: ['Nothing to review', 'Videos that wait for an OK before they post show up here.'],
  download: ['Nothing to download', 'Sports clips and reels you ask for land here: yours to save, edit or queue to post. No account is tied to them.'],
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
  if (v.state === 'download') return h('span', { class: 'tag good', text: 'To download' });
  return h('span', { class: 'tag', text: 'Library' });
}
function editing(v) {
  return (v.critiques || []).some(function (c) { return c.item === v.id && c.how === 'edit' && (c.status === 'queued' || c.status === 'working'); });
}
function card(v) {
  var said = v.critiques || [];
  var words = said.filter(function (c) { return c.how !== 'edit'; });
  var meta = [v.account_handle, v.category, v.part].filter(Boolean).join(' · ');
  return h('button', { class: 'card', onclick: function () { openVideo(v.id); } },
    h('span', { class: 'thumb' }, v.has_thumb ? h('img', { src: mediaUrl(v, 'thumb'), alt: '', loading: 'lazy' }) : null,
      len(v.duration) ? h('span', { class: 'len', text: len(v.duration) }) : null),
    h('span', { class: 'body' },
      h('span', { class: 'title', text: v.headline }),
      meta ? h('span', { class: 'meta', text: meta }) : null,
      v.stats ? h('span', { class: 'meta', text: big(v.stats.views) + ' views · ' + big(v.stats.likes) + ' likes' }) : null,
      h('span', { class: 'tags' }, stateTag(v),
        v.version ? h('span', { class: 'tag ver', text: v.version }) : null,
        v.fix_of && !v.version ? h('span', { class: 'tag fix', text: 'Made again' }) : null,
        v.editable && v.state !== 'download' ? h('span', { class: 'tag', text: 'Sports' }) : null,
        v.fixing ? h('span', { class: 'tag fix', text: editing(v) ? 'Being edited' : 'Being made again' }) : null,
        words.length && !v.fix_of ? h('span', { class: 'tag', text: plural(words.length, 'critique') }) : null)));
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
  if (ed) closeEditor(true);
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
  if (same('sheet', [v, S.busy, S.people])) return;  // (v carries its versions)
  var group = videosIn(v.state === 'review' ? 'review' : v.state === 'library' ? 'library' : v.state === 'download' ? 'download' : v.state === 'posted' ? 'posted' : 'queue');
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
  else if (v.state === 'download') stateLine = 'Yours to download: not queued to post, no account tied to it';
  else stateLine = 'In the library, not queued' + (v.account_handle ? '' : ' (its category has no TikTok account)');
  var bits = [v.account_handle, v.category, v.part, len(v.duration), v.ai_generated ? 'AI-generated, labelled as such' : ''].filter(Boolean);
  var from = v.prompt ? (v.prompt === 'your script' ? 'Made from your own script' : 'Made from “' + clip(v.prompt, 160) + '”')
           : v.source_title ? 'From “' + clip(v.source_title, 90) + '”' + (v.source_channel ? ' (' + clip(v.source_channel, 40) + ')' : '') : '';
  var cap = [v.caption, v.hashtags].filter(Boolean).join(' ');
  var origin = (v.critiques || []).filter(function (c) { return c.fixed === v.id; })[0];
  clear(sheet.info);
  add(sheet.info, [
    origin ? h('div', { class: 'block' }, h('div', { class: 'note' },
      h('div', { text: origin.how === 'edit' ? (isMe(origin.by) ? 'Your' : origin.name + '’s') + ' edit of the first version:'
                                            : 'Made again from ' + (isMe(origin.by) ? 'your' : origin.name + '’s') + ' critique of the first version:' }),
      h('div', { class: 'q', text: origin.how === 'edit' ? origin.text : '“' + origin.text + '”' })),
      find(origin.item) ? h('button', { class: 'textbtn', style: 'margin-top:8px', text: 'Watch the first version ›', onclick: function () { openVideo(origin.item); } }) : null) : null,
    h('div', { class: 'block' }, h('h2', { text: v.headline }),
      bits.length ? h('p', { class: 'sub', text: bits.join(' · ') }) : null,
      h('p', { class: cls, text: stateLine }),
      v.stats ? h('p', { class: 'sub', text: big(v.stats.views) + ' views · ' + big(v.stats.likes) + ' likes · ' + big(v.stats.comments) + ' comments · ' + big(v.stats.shares) + ' shares' }) : null,
      from ? h('p', { class: 'sub', text: from }) : null,
      cap && cap.replace(/\s+/g, ' ') !== String(v.headline).replace(/\s+/g, ' ') ? h('p', { class: 'cap', text: cap }) : null,
      v.warnings && v.warnings.length ? h('p', { class: 'warns', text: '⚠ ' + v.warnings.join('; ') }) : null)]);

  var vers = v.versions || [];
  if (vers.length > 1) {
    var keeping = v.state === 'download';
    add(sheet.info, h('div', { class: 'block' }, h('h3', { text: keeping ? 'Versions' : 'Pick the version to post' }),
      h('div', { class: 'vers' }, vers.map(function (x) {
        var others = vers.filter(function (o) { return o.id !== x.id; }).map(function (o) { return 'v' + o.version; }).join(' and ');
        return h('div', { class: 'vrow' + (x.id === v.id ? ' on' : '') },
          h('div', { class: 'col' },
            h('div', { class: 'name', text: x.label || 'v' + x.version }),
            h('div', { class: 'why', text: x.critique ? (x.how === 'edit' ? clip(x.critique, 110) : '“' + clip(x.critique, 110) + '”') + (x.by ? ' · ' + x.by : '') : 'As it was first made' }),
            x.id !== v.id ? h('button', { class: 'textbtn', text: 'Watch this one ›', onclick: function () { openVideo(x.id); } }) : null),
          h('button', { class: 'btn slim' + (keeping ? '' : ' primary'), text: (keeping ? 'Keep only v' : 'Approve v') + x.version, disabled: S.busy || v.fixing, onclick: function () {
            ask((keeping ? 'Keep v' : 'Post v') + x.version + ' and delete ' + others + ' from the PC?', function () {
              act(api('POST', 'api/videos/' + x.id + '/approve'), function () { if (x.id !== v.id) closeSheet(true); });
            });
          } }));
      })),
      h('p', { class: 'small', text: keeping ? 'Each version is yours to download. Keeping only one deletes the others from the PC; there’s no need to pick.'
                                              : 'Neither posts until you pick. Approving one queues it and deletes the other from the PC.' })));
  }

  var can = v.can || {}, btns = [], more = [];
  if (v.exists && v.state !== 'posting') {
    btns.push(h('button', { class: 'btn' + (v.state === 'download' ? ' primary' : ''), text: '⬇ Download', onclick: function () { download(v); } }));
    more.push(h('button', { class: 'btn', text: 'Send the file to our chat', disabled: S.busy, onclick: function () {
      act(api('POST', 'api/videos/' + v.id + '/send'));
    } }));
  }
  if (v.editable && v.state !== 'posted' && v.state !== 'posting') {
    btns.push(h('button', { class: 'btn' + (v.state === 'download' ? '' : ' primary'), text: '✂️ Edit', disabled: S.busy || v.fixing, onclick: function () { openEditor(v.id); } }));
  }
  if (can.ok && vers.length < 2) btns.push(h('button', { class: 'btn primary', text: 'OK to post', disabled: S.busy, onclick: function () {
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
  if (btns.length) add(sheet.acts, h('div', { class: 'acts wrap' }, btns));
  if (more.length) add(sheet.acts, h('div', { class: 'acts' }, more));
  if (v.exists && v.state !== 'posting') add(sheet.acts, h('p', { class: 'small pad', text: 'Download saves the full-quality file' + (v.size ? ' (' + mb(v.size) + ')' : '') + ' to this device. If your Telegram doesn’t save it, have the file sent to our chat and save it from there (up to 50 MB).' }));
  if (can.queue) add(sheet.acts, queueBlock(v));

  // What's wrong with it? The AI makes it again. (Left alone while it's being typed in.)
  if (!same('sheetfix', [v.id, v.state, v.fixing, v.fixable, v.ai_generated, v.editable, S.busy])) paintFix(v);
  paintThread(v);
}
function paintFix(v) {
  var keep = sheet.text ? sheet.text.value : (S.draft[v.id] || '');
  clear(sheet.fix); sheet.text = null;
  if (v.state !== 'posted' && v.state !== 'posting') {
    var box = h('div', { class: 'block' }, h('h3', { text: 'What should be different?' }));
    if (v.fixing && editing(v)) {
      box.appendChild(h('p', { class: 'sub', style: 'margin:0', text: 'The PC is cutting your edit now. The new version shows up beside this one when it’s ready.' }));
    } else if (v.fixing) {
      box.appendChild(h('p', { class: 'sub', style: 'margin:0', text: 'A new version is being made from the critique below. When it’s ready, both wait here and you approve the one to post.' }));
    } else if (v.editable) {
      add(box, [h('p', { class: 'sub', style: 'margin:0 0 12px', text: v.editable === 'reel'
          ? 'Open the editor to trim, reorder, add or drop plays, change the words, the narrator, the music and the framing. The PC cuts a new version from the game’s footage.'
          : 'Open the editor to move where the clip starts and ends, change its words, turn captions on or off and choose how the picture is framed.' }),
        h('button', { class: 'btn primary wide', text: v.editable === 'reel' ? '✂️ Edit this reel' : '✂️ Edit this clip', disabled: S.busy, onclick: function () { openEditor(v.id); } })]);
    } else if (v.fixable) {
      var count = h('span', { class: 'count', text: keep.length + ' / 2000' });
      sheet.text = h('textarea', { maxlength: 2000, placeholder: 'Say it in your own words. Make the captions yellow. The music is too loud. Use the British voice. The hook is flat, open with the twist.',
                                   'aria-label': 'What should be different about this video', oninput: function () {
        S.draft[v.id] = sheet.text.value; count.textContent = sheet.text.value.length + ' / 2000'; send.disabled = S.busy || sheet.text.value.trim().length < 3;
      } });
      sheet.text.value = keep;
      var send = h('button', { class: 'btn primary wide', text: 'Critique / Regenerate', disabled: S.busy || keep.trim().length < 3, onclick: function () {
        var text = sheet.text.value.trim();
        act(api('POST', 'api/videos/' + v.id + '/critique', { text: text }), function () {
          S.draft[v.id] = ''; if (sheet && sheet.text) sheet.text.value = ''; S.last.sheet = S.last.sheetfix = null;
        });
      } });
      add(box, [sheet.text, h('div', { class: 'hints' }, ['Captions yellow', 'Music quieter', 'Bigger captions', 'British voice', 'Stronger hook', 'Slower pace', 'Shorter', 'Pictures don’t match', 'Clearer ending'].map(function (t) {
        return h('button', { class: 'hint', text: t, onclick: function () {
          var cur = sheet.text.value.trim();
          sheet.text.value = (cur ? cur.replace(/[.\s]*$/, '. ') : '') + t + '.';
          sheet.text.dispatchEvent(new Event('input')); sheet.text.focus();
        } });
      })), send, h('p', { class: 'small' }, 'It comes back as v2 beside this one, and you approve the one to post.', count),
        h('ul', { class: 'ways' },
          h('li', { text: 'Captions and levels: only that changes, in one render.' }),
          h('li', { text: 'Another voice: the same script, voiced again.' }),
          h('li', { text: 'Anything about the script or pictures: the AI rewrites that and keeps the rest.' }))]);
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
    sheet.thread.appendChild(h('div', { class: 'block' }, h('h3', { text: said.every(function (c) { return c.how === 'edit'; }) ? 'Edits' : 'What was said' }), said.map(function (c) {
      var st, cl = 'st';
      var ed = c.how === 'edit';
      if (c.status === 'queued') { st = ed ? 'Waiting for the PC to cut it' : 'Waiting to be made again'; cl += ' going'; }
      else if (c.status === 'working') { st = (ed ? 'Being cut' : 'Being made again') + (c.stage ? ': ' + c.stage : '…'); cl += ' going'; }
      else if (c.status === 'done') { st = (ed ? 'Edited' : c.how === 'retouch' ? 'Changed on its timeline' : c.how === 'voice' ? 'Voiced again' : 'Made again') + ': v' + (c.version || 2); cl += ' good'; }
      else { st = (ed ? 'The edit couldn’t be made' : 'Couldn’t be made again') + (c.error ? ': ' + c.error : ''); cl += ' bad'; }
      return h('div', { class: 'said' }, face({ id: c.by, first: c.name }, 'sm'),
        h('div', { class: 'col' },
          h('div', { class: 'who' }, nameOf(c.by, c.name), h('span', { class: 'when', text: ago(c.at) })),
          h('div', { class: 'txt', text: c.text }),
          h('div', { class: cl, text: st }),
          c.status === 'done' && c.fixed && find(c.fixed) ? h('button', { class: 'textbtn', text: 'Watch the new version ›', onclick: function () { openVideo(c.fixed); } }) : null));
    })));
  }
}

// --- saving a video to this device -------------------------------------------------------------------
function mb(n) { return n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + ' MB' : Math.max(1, Math.round(n / 1e3)) + ' KB'; }
function fileName(v) {
  var s = String(v.headline || v.title || 'video').replace(/[\\\/:*?"<>|\u0000-\u001f]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 80) || 'video';
  return s + '.mp4';
}
function download(v) {
  var url = new URL(mediaUrl(v, 'file') + '&dl=1', window.location.href).href, name = fileName(v);
  if (tg && tg.downloadFile && tg.isVersionAtLeast && tg.isVersionAtLeast('8.0')) {
    // Telegram asks the person, then saves it with the phone's own downloads
    try { tg.downloadFile({ url: url, file_name: name }, function (ok) { if (ok) toast('Downloading “' + clip(v.headline, 50) + '”'); }); return; } catch (e) {}
  }
  var a = h('a', { href: url, download: name, rel: 'noopener', style: 'display:none' });
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  toast('Downloading “' + clip(v.headline, 50) + '”');
}
// A download, posted after all: the queue of the account its category (or another one) posts to.
function queueBlock(v) {
  if (!S.cats && !S.catsAsked) {
    S.catsAsked = true;
    api('GET', 'api/categories').then(function (d) { S.cats = d.items || []; S.last.sheet = null; if (sheet) paintSheet(); }, function () { S.catsAsked = false; });
  }
  var cats = S.cats || [];
  var pick = h('select', { 'aria-label': 'Category to post it for', onchange: function () { S.qcat[v.id] = pick.value; } },
    h('option', { value: '', text: v.category ? 'Its own category: ' + v.category : 'Pick a category…' }),
    cats.map(function (c) { return h('option', { value: c.id, selected: S.qcat[v.id] === c.id, disabled: !c.account, text: c.name + (c.account ? ' · ' + c.account : ' (no account)') }); }));
  return h('div', { class: 'block' }, h('h3', { text: 'Post it after all' }),
    h('div', { class: 'field' }, h('label', { text: 'Queue it for' }), pick),
    h('div', { class: 'gap' }),
    h('button', { class: 'btn wide', style: 'background:var(--bg2)', text: '📤 Queue it to post', disabled: S.busy || v.fixing, onclick: function () {
      ask('Queue “' + clip(v.headline, 60) + '” to post? It joins the queue of that category’s TikTok account.', function () {
        act(api('POST', 'api/videos/' + v.id + '/queue', { category: pick.value }));
      });
    } }),
    h('p', { class: 'small', text: 'Only if you want ClipMonster to post it. Until then it stays a download: no account, no schedule.' }));
}

// --- the sports editor -------------------------------------------------------------------------------
// A made reel or clip, cut again: the page keeps a draft of what the PC says can change (GET …/edit), and sends
// the whole draft back (POST …/edit). The PC works out what changed and cuts the new version from the footage.
var ed = null;
function stamp(t) {
  t = Math.max(0, Number(t) || 0);
  var m = Math.floor(t / 60), s = t - m * 60;
  return m + ':' + (s < 10 ? '0' : '') + s.toFixed(1);
}
function secs(t) { return (Math.round(t * 10) / 10).toFixed(1).replace(/\.0$/, '') + ' s'; }
function round1(x) { return Math.round(x * 10) / 10; }
function copy(x) { return JSON.parse(JSON.stringify(x)); }
function draftOf(doc) {
  if (doc.kind === 'reel') {
    return { plays: doc.plays.map(function (p) { return { id: p.id, start: p.start, end: p.end, label: p.label || '', badge: p.badge || '', title: p.title || '' }; }),
             opening: copy(doc.opening), words: copy(doc.words), look: copy(doc.look) };
  }
  return { cut: copy(doc.cut), words: copy(doc.words), look: copy(doc.look) };
}
function changesOf() {
  var d = ed.d;
  if (ed.doc.kind === 'reel') {
    return { plays: d.plays.map(function (p) { return { id: p.id, start: p.start, end: p.end, label: p.label, badge: p.badge }; }),
             opening: d.opening, words: d.words, look: d.look };
  }
  return { cut: d.cut, words: d.words, look: d.look };
}
function edDirty() { return !!ed && !!ed.doc && JSON.stringify(ed.d) !== JSON.stringify(draftOf(ed.doc)); }
function reelLength() {
  var d = ed.d, lim = ed.doc.limits, n = d.plays.length, tr = lim.transition || 0.25;
  var total = d.plays.reduce(function (a, p) { return a + (p.end - p.start); }, 0);
  var teaser = d.opening.on && n > 1;
  if (teaser) total += lim.teaser || 3.2;
  return Math.max(0, total - Math.max(0, n + (teaser ? 1 : 0) - 1) * tr);
}
// What stops this draft from being made, in words ('' = nothing).
function edProblem() {
  var doc = ed.doc, d = ed.d, lim = doc.limits;
  if (doc.why) return doc.why;
  if (doc.kind === 'reel') {
    if (!d.plays.length) return 'A reel needs at least one play.';
    var total = reelLength();
    if (total > lim.longest) return 'That’s ' + len(total) + ': ' + len(lim.longest) + ' at most. Trim a play or take one out.';
    if (total < 5) return 'That’s under 5 seconds.';
  }
  return '';
}
function openEditor(id) {
  var v = find(id);
  if (!v) { toast("That video isn't in the library any more.", true); return; }
  closeEditor(true);
  if (sheet) { var playing = sheet.root.querySelector('video'); if (playing) { try { playing.pause(); } catch (e) {} } }
  ed = { id: id, doc: null, d: null, open: -1, adding: false, stop: null, busy: false, video: null,
         title: h('span', { class: 'where', text: 'Editor' }), stage: h('div', { class: 'stage ed-stage' }), body: h('div'),
         foot: h('div', { class: 'ed-foot' }), root: null };
  ed.root = h('div', { class: 'sheet editor', role: 'dialog', 'aria-label': 'Edit ' + v.headline },
    h('div', { class: 'sheet-in' },
      h('div', { class: 'bar' }, h('button', { class: 'back', text: '‹ Back', onclick: function () { leaveEditor(); } }), ed.title, h('span', { class: 'step' })),
      ed.stage, ed.body),
    ed.foot);
  ed.body.appendChild(h('div', { class: 'empty' }, h('div', { class: 'spin', style: 'margin:0 auto 10px' }), 'Reading its plan from the PC…'));
  document.body.appendChild(ed.root);
  document.body.style.overflow = 'hidden';
  if (tg && tg.BackButton) { try { tg.BackButton.show(); } catch (e) {} }
  api('GET', 'api/videos/' + encodeURIComponent(id) + '/edit').then(function (doc) {
    if (!ed || ed.id !== id) return;
    ed.doc = doc; ed.d = draftOf(doc);
    edStage(); paintEditor();
  }, function (e) {
    if (!ed || ed.id !== id) return;
    closeEditor();
    if (e.status === 401) locked(e); else toast(e.message || 'The editor couldn’t open.', true);
  });
}
function closeEditor(quiet) {
  if (!ed) return;
  if (ed.video) { try { ed.video.pause(); ed.video.removeAttribute('src'); ed.video.load(); } catch (e) {} }
  document.body.removeChild(ed.root);
  ed = null;
  if (!sheet) {
    document.body.style.overflow = '';
    if (tg && tg.BackButton) { try { tg.BackButton.hide(); } catch (e) {} }
  }
  if (!quiet) paint();
}
function leaveEditor() {
  if (ed && edDirty() && !ed.busy) ask('Leave the editor? Your changes haven’t been made into a version.', function () { closeEditor(); });
  else closeEditor();
}
// The game's own footage, to play a cut before it's made.
function edStage() {
  var doc = ed.doc;
  clear(ed.stage);
  if (doc.game && doc.game.source && doc.game.id) {
    ed.video = h('video', { src: 'api/sports/source/' + encodeURIComponent(doc.game.id) + '?m=' + encodeURIComponent(S.media), controls: true,
                            playsinline: true, 'webkit-playsinline': true, preload: 'metadata' });
    ed.video.addEventListener('timeupdate', function () {
      if (ed && ed.stop != null && ed.video.currentTime >= ed.stop) { ed.video.pause(); ed.stop = null; }
    });
    ed.video.addEventListener('error', function () {
      if (!ed) return;
      ed.video = null;
      ed.stage.classList.remove('pin');
      clear(ed.stage).appendChild(h('div', { class: 'gone', text: 'This phone can’t play the game’s footage, so cuts can’t be previewed here. The times still work.' }));
      paintEditor();
    });
    ed.stage.appendChild(ed.video);
    ed.stage.classList.add('pin');  // the footage stays in view under the bar while the cuts below are worked on
    var bar = ed.root.querySelector('.bar');
    if (bar && bar.offsetHeight) ed.stage.style.top = bar.offsetHeight + 'px';
  } else {
    ed.stage.appendChild(h('div', { class: 'gone', text: 'The game’s footage isn’t on the PC right now, so cuts can’t be previewed here. The times still work: the PC downloads it again when the edit is made.' }));
  }
}
function playCut(a, b) {
  if (!ed || !ed.video) return;
  ed.stop = b;
  try { ed.video.currentTime = a; var p = ed.video.play(); if (p && p.catch) p.catch(function () {}); } catch (e) {}
}
function epicks(label, options, value, set) {
  return h('div', { class: 'field' }, h('label', { text: label }), h('div', { class: 'picks', role: 'group', 'aria-label': label }, options.map(function (o) {
    var on = String(value) === String(o[0]);
    return h('button', { class: 'pick' + (on ? ' on' : ''), 'aria-pressed': on ? 'true' : 'false', disabled: !!ed.doc.why, onclick: function () { set(o[0]); paintEditor(); } }, o[1]);
  })));
}
function etext(label, value, max, set, area, placeholder) {
  var el = h(area ? 'textarea' : 'input', { type: area ? null : 'text', maxlength: max, 'aria-label': label, placeholder: placeholder || null, autocomplete: 'off',
                                         disabled: !!ed.doc.why, oninput: function () { set(el.value); paintFoot(); } });
  el.value = value || '';
  return h('div', { class: 'field' }, h('label', { text: label }), el);
}
function opts(list) { return (list || []).map(function (o) { return [o.id, o.label]; }); }
// Start and end of one stretch of the footage: nudges, sliders, "where the video is now".
function trimmer(span, orig, min, max, onLength) {
  var doc = ed.doc, dur = doc.game.duration || 0, reach = doc.limits.reach || 20;
  var lo = Math.max(0, round1(Math.min(orig.start, span.start) - reach));
  var hi = round1(Math.max(orig.end, span.end) + reach);
  if (dur) hi = Math.min(hi, Math.floor(dur * 10) / 10);
  var tA = h('span', { class: 't', text: stamp(span.start) }), tB = h('span', { class: 't', text: stamp(span.end) });
  var rA, rB;
  function put(which, value, repaint) {
    value = round1(value);
    if (which === 'start') {
      value = Math.max(0, Math.min(value, span.end - min));
      if (span.end - value > max) value = round1(span.end - max);
      span.start = value;
    } else {
      value = Math.max(value, span.start + min);
      if (dur) value = Math.min(value, Math.floor(dur * 10) / 10);
      if (value - span.start > max) value = round1(span.start + max);
      span.end = value;
    }
    tA.textContent = stamp(span.start); tB.textContent = stamp(span.end);
    if (onLength) onLength();
    paintFoot();
    if (repaint) paintEditor(); else { rA.value = span.start; rB.value = span.end; }
  }
  function line(which, label, t) {
    return h('div', { class: 'line' }, h('span', { class: 'lbl', text: label }),
      [-1, -0.2].map(function (n) { return h('button', { class: 'nudge', text: (n < 0 ? '−' : '+') + Math.abs(n), 'aria-label': label + ' ' + n + ' seconds', onclick: function () { put(which, span[which] + n, true); } }); }),
      t,
      [0.2, 1].map(function (n) { return h('button', { class: 'nudge', text: '+' + n, 'aria-label': label + ' +' + n + ' seconds', onclick: function () { put(which, span[which] + n, true); } }); }));
  }
  rA = h('input', { type: 'range', min: lo, max: hi, step: 0.1, 'aria-label': 'Where it starts', oninput: function () { put('start', Number(rA.value)); }, onchange: function () { paintEditor(); } });
  rB = h('input', { type: 'range', min: lo, max: hi, step: 0.1, 'aria-label': 'Where it ends', oninput: function () { put('end', Number(rB.value)); }, onchange: function () { paintEditor(); } });
  rA.value = span.start; rB.value = span.end;
  var here = ed.video ? h('div', { class: 'line' },
    h('button', { class: 'btn slim primary', text: '▶ Play this cut', onclick: function () { playCut(span.start, span.end); } }),
    h('button', { class: 'btn slim', text: 'Start here', title: 'Start where the video above is now', onclick: function () { put('start', ed.video.currentTime, true); } }),
    h('button', { class: 'btn slim', text: 'End here', title: 'End where the video above is now', onclick: function () { put('end', ed.video.currentTime, true); } })) : null;
  return h('div', { class: 'trim' }, here, line('start', 'Start', tA), rA, line('end', 'End', tB), rB,
    Math.abs(span.start - orig.start) > 0.04 || Math.abs(span.end - orig.end) > 0.04
      ? h('button', { class: 'textbtn', text: 'Back to ' + stamp(orig.start) + ' – ' + stamp(orig.end), onclick: function () { span.start = orig.start; span.end = orig.end; paintEditor(); } }) : null);
}
function playRow(p, i) {
  var d = ed.d, doc = ed.doc, lim = doc.limits, open = ed.open === i;
  var orig = doc.plays.filter(function (x) { return x.id === p.id; })[0] || (doc.more || []).filter(function (x) { return x.id === p.id; })[0] || p;
  var length = h('span', { text: secs(p.end - p.start) + ' · ' + stamp(p.start) + ' – ' + stamp(p.end) });
  var row = h('div', { class: 'play' + (open ? ' on' : '') },
    h('div', { class: 'head' },
      h('span', { class: 'num', text: i + 1 }),
      h('button', { class: 'name', 'aria-expanded': open ? 'true' : 'false', onclick: function () { ed.open = open ? -1 : i; paintEditor(); } },
        h('b', { text: [p.label, p.title || p.badge].filter(Boolean).join(' · ') || 'Play ' + (i + 1) }), length),
      h('button', { class: 'icon', text: '▲', 'aria-label': 'Move play ' + (i + 1) + ' earlier', disabled: i === 0, onclick: function () { move(i, -1); } }),
      h('button', { class: 'icon', text: '▼', 'aria-label': 'Move play ' + (i + 1) + ' later', disabled: i === d.plays.length - 1, onclick: function () { move(i, 1); } }),
      h('button', { class: 'icon danger', text: '✕', 'aria-label': 'Take play ' + (i + 1) + ' out', disabled: d.plays.length < 2, onclick: function () {
        d.plays.splice(i, 1); ed.open = -1; paintEditor();
      } })));
  if (open) {
    add(row, [trimmer(p, { start: orig.start, end: orig.end }, lim.play_min, lim.play_max, function () {
        length.textContent = secs(p.end - p.start) + ' · ' + stamp(p.start) + ' – ' + stamp(p.end);
      }),
      h('div', { class: 'field pair', style: 'margin-top:10px' },
        h('div', {}, h('label', { text: 'Big label' }), tin(p.label, lim.label, function (v) { p.label = v; }, 'PLAY 3')),
        h('div', {}, h('label', { text: 'Lower third' }), tin(p.badge, lim.badge, function (v) { p.badge = v; }, 'WHO · WHAT')))]);
  }
  return row;
}
function tin(value, max, set, placeholder) {
  var el = h('input', { type: 'text', maxlength: max, placeholder: placeholder, autocomplete: 'off', oninput: function () { set(el.value); paintFoot(); } });
  el.value = value || '';
  return el;
}
function move(i, by) {
  var p = ed.d.plays, j = i + by;
  if (j < 0 || j >= p.length) return;
  var x = p[i]; p[i] = p[j]; p[j] = x;
  if (ed.open === i) ed.open = j; else if (ed.open === j) ed.open = i;
  paintEditor();
}
function paintEditor() {
  if (!ed || !ed.doc) return;
  var doc = ed.doc, d = ed.d, lim = doc.limits, y = ed.root.scrollTop, out = [];
  ed.title.textContent = (doc.kind === 'reel' ? 'Edit reel' : 'Edit clip') + (doc.version ? ' · ' + doc.version.split(' ')[0] : '');
  if (doc.why) out.push(h('div', { class: 'block' }, h('p', { class: 'warns', style: 'margin:0', text: '⚠ ' + doc.why })));
  else if (doc.editing) out.push(h('div', { class: 'block' }, h('p', { class: 'sub', style: 'margin:0', text: 'An edit of this one is being made right now. Wait for that version, then edit again.' })));
  out.push(h('div', { class: 'block' }, h('h2', { text: doc.title }),
    h('p', { class: 'sub', text: [doc.game.matchup, doc.kind === 'reel' ? 'Recap reel' : 'Single play', len(doc.length)].filter(Boolean).join(' · ') }),
    ed.video ? h('p', { class: 'small', text: 'Above is the game’s own footage, uncropped. “Play this cut” shows a stretch of it; the new version is framed, mixed and titled when the PC makes it.' }) : null));
  if (doc.kind === 'reel') {
    var used = d.plays.map(function (p) { return p.id; });
    var spare = (doc.more || []).filter(function (m) { return used.indexOf(m.id) < 0; });
    out.push(h('div', { class: 'block' }, h('h3', { text: 'Plays, in the order they run' }),
      d.plays.map(playRow),
      h('div', { class: 'acts', style: 'margin:12px 0 0' },
        h('button', { class: 'btn slim', style: 'background:var(--bg2);flex:1', text: ed.adding ? 'Done adding' : '＋ Add a play' + (spare.length ? ' (' + spare.length + ')' : ''), disabled: !spare.length || d.plays.length >= lim.plays,
                      onclick: function () { ed.adding = !ed.adding; paintEditor(); } }),
        h('button', { class: 'btn slim', style: 'background:var(--bg2);flex:1', text: 'Number them again', title: 'PLAY 5 … NUMBER 1, in this order', onclick: function () {
          var n = d.plays.length;
          d.plays.forEach(function (p, i) { p.label = n - i === 1 ? 'NUMBER 1' : 'PLAY ' + (n - i); });
          paintEditor();
        } })),
      ed.adding && spare.length ? h('div', { class: 'games', style: 'margin-top:10px' }, spare.map(function (m) {
        var end = Math.min(m.end, m.start + 24);
        return h('button', { class: 'game', disabled: d.plays.length >= lim.plays, onclick: function () {
          d.plays.push({ id: m.id, start: m.start, end: end, label: '', badge: '', title: m.title });
          ed.open = d.plays.length - 1;
          if (!(doc.more || []).some(function (x) { return x.id !== m.id && used.indexOf(x.id) < 0; })) ed.adding = false;
          paintEditor();
        } },
          h('span', { class: 'em', text: m.found ? '🎯' : '＋' }),
          h('span', { class: 'col' }, h('span', { class: 'line', text: m.title }), h('span', { class: 'lg', text: (m.found ? 'Found moment' : m.rank ? 'Ranked #' + m.rank : 'A play') + ' · ' + stamp(m.start) + ' into the video' })),
          h('span', { class: 'when', text: secs(end - m.start) }));
      })) : null,
      h('p', { class: 'small', text: 'Tap a play to trim it. Each runs ' + lim.play_min + ' to ' + lim.play_max + ' seconds; the reel, ' + len(lim.longest) + ' at most.' })));
    var o = d.opening, voice = h('select', { 'aria-label': 'Narrator', onchange: function () { o.voice = voice.value; paintEditor(); } },
      (doc.choices.voices || []).map(function (x) { return h('option', { value: x.id, selected: o.voice === x.id, text: x.label }); }));
    out.push(h('div', { class: 'block' }, h('h3', { text: 'Opening' }),
      epicks('Teaser: the last play, cut off just before its moment', [['1', 'On'], ['', 'Off']], o.on ? '1' : '', function (v) { o.on = !!v; }),
      etext('Headline over the opening', o.hook, lim.hook, function (v) { o.hook = v; }, false, 'A NIGHT OF BUZZER BEATERS'),
      h('div', { class: 'field' }, h('label', { text: 'Narrator' }), voice),
      o.voice !== 'off' ? etext('What the narrator says', o.words, lim.words, function (v) { o.words = v; }, true, 'Two sentences that set the game up.') : null,
      o.voice !== 'off' ? h('p', { class: 'small', text: 'A narrator’s voice is synthetic, so the video is labelled AI-generated if it’s ever posted.' }) : null));
    var k = d.look;
    out.push(h('div', { class: 'block' }, h('h3', { text: 'Sound and picture' }),
      epicks('Music', opts(doc.choices.music), k.music, function (v) { k.music = v; }),
      epicks('The game’s own sound', opts(doc.choices.game), k.game, function (v) { k.game = v; }),
      epicks('Picture', opts(doc.choices.framing), k.framing, function (v) { k.framing = v; }),
      epicks('Between plays', opts(doc.choices.styles), k.style, function (v) { k.style = v; }),
      epicks('Score bug', [['1', 'On'], ['', 'Off']], k.bug ? '1' : '', function (v) { k.bug = !!v; })));
    var w = d.words;
    out.push(h('div', { class: 'block' }, h('h3', { text: 'Words that go with it' }),
      etext('Final score line, over the last play', w.final, lim.final, function (v) { w.final = v; }, false, 'Lakers 108-106 Celtics'),
      etext('Caption', w.caption, lim.caption, function (v) { w.caption = v; }, true),
      etext('Hashtags', w.hashtags, 200, function (v) { w.hashtags = v; }, false, '#nba #lakers')));
  } else {
    var c = d.cut, cw = d.words, ck = d.look;
    var clen = h('p', { class: 'sub', text: secs(c.end - c.start) + ' · ' + stamp(c.start) + ' – ' + stamp(c.end) + ' of the footage' });
    out.push(h('div', { class: 'block' }, h('h3', { text: 'The cut' }), clen,
      trimmer(c, doc.cut, lim.clip_min, lim.clip_max, function () { clen.textContent = secs(c.end - c.start) + ' · ' + stamp(c.start) + ' – ' + stamp(c.end) + ' of the footage'; }),
      h('p', { class: 'small', text: 'A clip runs ' + lim.clip_min + ' to ' + lim.clip_max + ' seconds.' })));
    out.push(h('div', { class: 'block' }, h('h3', { text: 'Picture' }),
      epicks('Shape', opts(doc.choices.aspects), ck.aspect, function (v) { ck.aspect = v; }),
      ck.aspect === 'vertical_crop' ? h('div', { class: 'field' }, h('label', { text: 'Which part of the picture fills the screen' }),
        (function () {
          var r = h('input', { type: 'range', min: 0, max: 1, step: 0.05, 'aria-label': 'Crop position, left to right', oninput: function () { ck.crop_x = Number(r.value); paintFoot(); } });
          r.value = ck.crop_x; return r;
        })(), h('div', { class: 'ends' }, h('span', { text: 'Left' }), h('span', { text: 'Middle' }), h('span', { text: 'Right' }))) : null,
      epicks('Captions of what’s said', [['1', 'On'], ['', 'Off']], ck.captions ? '1' : '', function (v) { ck.captions = !!v; })));
    out.push(h('div', { class: 'block' }, h('h3', { text: 'Words' }),
      etext('Headline over the clip (empty for none)', cw.headline, lim.headline, function (v) { cw.headline = v; }, false, 'HE CALLED GAME'),
      etext('Caption', cw.caption, lim.caption, function (v) { cw.caption = v; }, true),
      etext('Hashtags', cw.hashtags, 200, function (v) { cw.hashtags = v; }, false, '#nba #lakers')));
  }
  out.push(h('div', { class: 'gap' }));
  clear(ed.body); add(ed.body, out);
  ed.root.scrollTop = y;
  paintFoot();
}
function paintFoot() {
  if (!ed || !ed.doc) return;
  var doc = ed.doc, d = ed.d, why = edProblem(), dirty = edDirty();
  var total = doc.kind === 'reel' ? reelLength() : d.cut.end - d.cut.start;
  var head = doc.kind === 'reel' ? len(total) + ' · ' + plural(d.plays.length, 'play') : secs(total);
  var note = why && !doc.why ? why : doc.why ? 'Can’t be edited' : doc.editing ? 'An edit is being made' : dirty ? 'The PC cuts a new version; this one stays' : 'Nothing changed yet';
  clear(ed.foot);
  add(ed.foot, [h('div', { class: 'sum' + (why && !doc.why ? ' bad' : '') }, head, h('small', { text: note })),
    dirty ? h('button', { class: 'textbtn', text: 'Reset', disabled: ed.busy, onclick: function () { ed.d = draftOf(doc); ed.open = -1; ed.adding = false; paintEditor(); } }) : null,
    h('button', { class: 'btn primary slim', text: ed.busy ? 'Sending…' : 'Make this version', disabled: ed.busy || !dirty || !!why || !!doc.editing, onclick: function () {
      var id = ed.id;
      ed.busy = true; paintFoot();
      api('POST', 'api/videos/' + encodeURIComponent(id) + '/edit', { changes: changesOf() }).then(function (r) {
        if (ed && ed.id === id) closeEditor(true);
        toast(r.message || 'The PC is cutting the new version.');
        S.last.sheet = S.last.sheetfix = null;
        paint(true);
        return refresh();
      }, function (e) {
        if (ed && ed.id === id) { ed.busy = false; paintFoot(); }
        if (e.status === 401) { closeEditor(true); return locked(e); }
        toast(e.message || 'The edit couldn’t be sent.', true);
      });
    } })]);
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
    f.want !== 'reel' && need && f.deliver === 'post' ? h('p', { class: 'small warnline', text: 'Single plays run 15 to 40 seconds, and a request’s videos have to be over ' + Math.round(need - 1) + ' s: they’ll wait for an OK instead of posting.' }) : null];
}
// Where it goes: yours to download (no account, no queue), or the posting queue of a category's account.
function deliverPick(d, f, sportId) {
  return [picks('When it’s made', [['download', 'I’ll download it'], ['post', 'Queue it to post']], f.deliver, function (v) { f.deliver = v; }),
    f.deliver === 'post' ? catPick(d, f, sportId)
      : h('p', { class: 'small', text: 'It lands under Videos → Downloads: save it to your device, edit it, or queue it to post later. It isn’t tied to a TikTok account and nothing posts by itself.' })];
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
  var how = h('div', { class: 'picks two', role: 'tablist' }, [['pick', 'Pick a game'], ['say', 'Describe it'], ['find', 'Find a moment']].map(function (k) {
    return h('button', { class: 'pick' + (f.how === k[0] ? ' on' : ''), role: 'tab', 'aria-selected': f.how === k[0], onclick: function () { f.how = k[0]; paint(true); } }, k[1]);
  }));
  var form = f.how === 'pick' ? pickForm(d, f, full) : f.how === 'find' ? findForm(d, f, full) : sayForm(d, f, full);
  var orders = d.orders || [];
  return [onPC(), h('div', { class: 'block' }, how, form,
      full ? h('p', { class: 'small', text: plural(open.length, 'request') + ' are waiting or under way: wait for one to finish, or cancel one.' }) : null,
      h('p', { class: 'small', text: 'Automation finds the game’s highlights on YouTube, picks its best plays and makes them on the PC, with your name on the request. What’s queued to post is checked first: over a minute, upright, with sound. League footage often gets copyright claims on TikTok.' })),
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
    var body = { game: f.game, want: f.want, plays: f.plays, deliver: f.deliver };
    if (f.cat && f.deliver === 'post') body.category = f.cat;
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
    deliverPick(d, f, chosen ? chosen.sport : f.psport),
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
    var body = { sport: f.sport, league: f.league.trim(), teams: f.teams.trim(), since: f.since, want: f.want, games: f.games, plays: f.plays, deliver: f.deliver };
    if (f.cat && f.deliver === 'post') body.category = f.cat;
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
    deliverPick(d, f, sport ? sport.id : ''),
    h('div', { class: 'gap' }), go);
}
// Find a moment: say it, and (when the words don't name the game) pick the game it's in.
function findForm(d, f, full) {
  var played = ((S.played || {}).games || []).filter(function (g) { return g.ready; });
  var query = h('input', { type: 'text', maxlength: 200, placeholder: 'Mahomes 4th quarter touchdown', 'aria-label': 'The moment to look for', autocomplete: 'off',
                           oninput: function () { f.query = query.value; go.disabled = !can(); } });
  query.value = f.query;
  var desk = d.find_games || [];
  var known = desk.map(function (g) { return 'desk:' + g.id; }).concat(played.map(function (g) { return 'slate:' + g.id; }));
  if (f.fgame && known.indexOf(f.fgame) < 0) f.fgame = '';
  var pickGame = h('select', { 'aria-label': 'Game', onchange: function () { f.fgame = pickGame.value; } },
    h('option', { value: '', text: 'The game the words name' }),
    desk.length ? h('optgroup', { label: 'On the Sports page' }, desk.map(function (g) {
      return h('option', { value: 'desk:' + g.id, selected: f.fgame === 'desk:' + g.id, text: g.label });
    })) : null,
    played.length ? h('optgroup', { label: 'Played in the last 24 hours' }, played.map(function (g) {
      return h('option', { value: 'slate:' + g.id, selected: f.fgame === 'slate:' + g.id, text: g.emoji + ' ' + g.league + ': ' + g.line });
    })) : null);
  function can() { return !S.busy && !full && f.query.trim().length >= 3; }
  var go = h('button', { class: 'btn primary wide', text: 'Search the game', disabled: !can(), onclick: function () {
    act(api('POST', 'api/sports/find', { query: f.query.trim(), game: f.fgame }), function () { f.query = ''; });
    query.blur();
  } });
  return h('div', {},
    h('div', { class: 'field' }, h('label', { text: 'The moment' }), query),
    h('div', { class: 'field' }, h('label', { text: 'In' }), pickGame),
    h('div', { class: 'gap' }), go,
    h('p', { class: 'small', text: 'The PC reads the game’s commentary and play-by-play for it. What it finds shows up under the request below, each with when it happened and what was said, to clip by itself or add to a reel. You can also name the game in the words: “red card in Arsenal Chelsea”.' }));
}
function momentRow(o, m, baskets) {
  var basket = baskets[m.game] || [], inside = basket.indexOf(m.id) >= 0;
  var secs = Math.max(0, Math.round((m.end || 0) - (m.start || 0)));
  return h('div', { class: 'moment' },
    h('div', { class: 'lab', text: '🎯 ' + (m.label || 'Moment') + (m.badge ? ' · ' + clip(m.badge, 50) : '') }),
    h('div', { class: 'at', text: len(m.t) + ' into the video · ' + secs + ' s · ' + (m.score || 0) + '% sure' + (m.certain === false ? ' · only talked about here' : '') }),
    m.quote ? h('div', { class: 'q', text: '“' + clip(m.quote, 200) + '”' }) : null,
    h('div', { class: 'acts' },
      h('button', { class: 'btn slim', text: '🎬 Clip play (' + secs + 's)', disabled: S.busy, onclick: function () {
        act(api('POST', 'api/sports/moments', { game: m.game, ids: [m.id], want: 'clips', deliver: S.sp.deliver }));
      } }),
      h('button', { class: 'btn slim' + (inside ? ' primary' : ''), text: inside ? '✓ In the reel' : '➕ Add to reel', disabled: S.busy, onclick: function () {
        act(api('POST', 'api/sports/basket', { game: m.game, id: m.id, add: !inside }).then(function (r) { return { message: inside ? 'Taken out of the reel' : plural(r.basket.length, 'moment') + ' set aside for the reel' }; }));
      } })));
}
function orderRow(o) {
  var open = o.status === 'queued' || o.status === 'searching' || o.status === 'working';
  var made = (o.steps || []).filter(function (s) { return s.status === 'done' && s.kind !== 'highlights'; });
  if (o.want === 'find' && !open && o.status === 'done') {
    var ms = o.moments || [], baskets = (S.sports || {}).baskets || {};
    var gid = ms.length ? ms[0].game : '', set = baskets[gid] || [];
    var newest = ((S.sports || {}).orders || []).filter(function (x) {
      return x.want === 'find' && x.status === 'done' && (x.moments || []).length && x.moments[0].game === gid;
    })[0];
    if (gid && newest && newest.id !== o.id) {  // the game was searched again since: these moments were replaced
      return h('div', { class: 'order' },
        h('div', { class: 'head' }, face({ id: o.by_id, first: o.by || '?' }, 'sm'), h('div', { class: 'col' },
          h('div', { class: 'title', text: '🔎 ' + o.title }),
          h('div', { class: 'who', text: nameOf(o.by_id, o.by || 'Someone') + ' · ' + ago(o.created) }))),
        h('div', { class: 'now', text: 'This game was searched again since, so these moments were replaced by the newer search above.' }));
    }
    return h('div', { class: 'order' },
      h('div', { class: 'head' }, face({ id: o.by_id, first: o.by || '?' }, 'sm'), h('div', { class: 'col' },
        h('div', { class: 'title', text: '🔎 ' + o.title }),
        h('div', { class: 'who', text: nameOf(o.by_id, o.by || 'Someone') + ' · ' + ago(o.created) }))),
      ms.length ? h('div', { class: 'now good', text: plural(ms.length, 'moment') + ' found' })
                : h('div', { class: 'now', text: 'Nothing found. Try the words a commentator would use, or a player’s name.' }),
      ms.map(function (m) { return momentRow(o, m, baskets); }),
      set.length ? h('button', { class: 'btn primary wide', style: 'margin-top:10px', text: '🎞 Make the reel (' + plural(set.length, 'moment') + ')', disabled: S.busy, onclick: function () {
        act(api('POST', 'api/sports/moments', { game: gid, ids: set, want: 'reel', deliver: S.sp.deliver }));
      } }) : null,
      ms.length ? picks('What’s made from these', [['download', 'I’ll download it'], ['post', 'Queue it to post']], S.sp.deliver, function (v) { S.sp.deliver = v; }) : null,
      ms.length && S.sp.deliver === 'post' ? h('p', { class: 'small', text: 'A single play is under a minute, so a clipped moment waits for an OK instead of posting by itself. Three or more moments make a reel that’s over a minute.' }) : null);
  }
  var now = open ? h('div', { class: 'now', text: o.doing || 'Waiting its turn on the PC' })
    : o.status === 'done' ? h('div', { class: 'now good' }, made.length ? [o.deliver === 'download' ? 'Ready to download' : 'Ready under Videos', h('ul', {}, made.map(function (s) { return h('li', { text: s.said || s.kind }); })),
        o.deliver === 'download' ? h('button', { class: 'textbtn', text: 'Open Downloads ›', onclick: function () { S.tab = 'videos'; S.filter = 'download'; paint(true); window.scrollTo(0, 0); } }) : null] : (o.message || 'Done'))
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
  var idea = h('textarea', { maxlength: 1000, placeholder: 'Make a 65s video about 3 dark psychology tricks. Or paste a link to an article.', 'aria-label': 'What the video is about, or a link to an article',
                             oninput: function () { S.idea = idea.value; go.disabled = S.busy || !idea.value.trim(); } });
  idea.value = S.idea;
  var cats = S.cats || [];
  var pick = h('select', { 'aria-label': 'Category', onchange: function () { S.cat = pick.value; } },
    cats.length ? h('option', { value: 'auto', selected: S.cat === 'auto', text: 'Pick the category for me' }) : null,
    cats.map(function (c) {
      return h('option', { value: c.id, selected: S.cat === c.id, text: c.name + (c.paused ? ' (paused)' : c.account ? ' · ' + c.account : ' (no account)') });
    }),
    h('option', { value: '', selected: S.cat === '', text: 'No category' }));
  var go = h('button', { class: 'btn primary wide', text: 'Make this video', disabled: S.busy || !S.idea.trim(), onclick: function () {
    var body = { idea: idea.value.trim(), category: pick.value };
    if (S.secs) body.seconds = S.secs;
    act(api('POST', 'api/make', body), function () { S.idea = ''; S.last.shell = null; });
    idea.blur();
  } });
  var next = (q.next || []).filter(function (j) { return j.making; });
  return [h('div', { class: 'block' }, h('h3', { text: 'A new video' }), idea, h('div', { class: 'gap' }),
      h('div', { class: 'field' }, h('label', { text: 'For' }), pick),
      picks('Length', [[0, 'Automatic'], [65, '65 s'], [75, '75 s'], [85, '85 s']], S.secs, function (v) { S.secs = v; }),
      h('div', { class: 'gap' }), go,
    h('p', { class: 'small', text: 'Say it the way you’d say it in the chat: the idea, or a link to an article to retell. “For History” or “to @account” in the words picks the category; otherwise it’s found from what the idea is about, and you’re asked when that isn’t clear. The category sets the voice, the pictures and the captions. Every video is upright and 62 to 85 seconds, which Creator Rewards needs.' })),
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
  request: ['asked for', ''], sports: ['asked for sports highlights:', ''], find: ['searched a game:', ''], approve: ['picked', ' to post'], keep: ['kept', ''],
  edit: ['edited', ''], download: ['took the file of', ''], queue: ['queued', ' to post'],
  cancel: ['cancelled the request for', ''], pause: ['paused automation', ''],
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
    var asked = new URLSearchParams(window.location.search);
    var want = asked.get('v') || (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';
    if (want && find(want)) { S.filter = find(want).state === 'download' ? 'download' : S.filter; openVideo(want); if (asked.get('e') === '1' && find(want).editable) openEditor(want); }
    else if (!videosIn('review').length && videosIn('download').length) { S.filter = 'download'; paint(); }
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
  if (tg.BackButton) { try { tg.BackButton.onClick(function () { if (ed) leaveEditor(); else closeSheet(); }); } catch (e) {} }
}
document.addEventListener('visibilitychange', function () { if (!document.hidden && S.me) refresh(); });
window.__clipmonster = { state: S, open: openVideo, edit: openEditor, editor: function () { return ed; } };
start();
})();
"""
