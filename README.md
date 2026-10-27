# ClipMonster

ClipMonster turns long videos into ready-to-post clips. Paste YouTube links (or drop in your own video files) and it finds the best moments. You fine-tune them on a timeline, then export with animated captions in landscape, vertical or square format.

It comes as a Windows desktop app, **`ClipMonster.exe`**, plus a command-line tool, **`ytclip.exe`**, for batch jobs and scripts.

---

## Running it

The built app is in `dist/ClipMonster/`. Double-click **`ClipMonster.exe`**.

To give it to someone else, send them `dist/ClipMonster-windows.zip`. They unzip it and run `ClipMonster.exe`; Python isn't needed. Everything ships inside the folder, including ffmpeg, Qt, Whisper and the YouTube downloader.

> YouTube downloads need a JavaScript runtime on the PC: [Node.js](https://nodejs.org) or [Deno](https://deno.com). Local video files work without one.

## The studio

| Area | What it does |
|---|---|
| **Link bar** | Paste one or many YouTube links, playlists or channels. Drop video files or whole folders anywhere on the window. `Ctrl+V` adds links straight from the clipboard. |
| **Videos queue** | Every video is downloaded and analysed in the background, one at a time. Right-click a video to re-analyse, export, open its folder or remove it. Your queue and clip edits are saved and restored the next time you open the app. |
| **Preview** | **Export view** (on by default, `V` to switch) plays the selected clip exactly as it will export: the format, the blurred fill, banners, the stacked second video in sync, and captions that follow the speech word by word. It's drawn from the same numbers as the export, so what you see is what you get. Drag the picture to reframe it. **Safe zone** shades where TikTok's buttons and caption will sit. Switch Export view off to see the whole source video, with a frame showing what a crop keeps. |
| **Interest timeline** | The purple curve is how interesting each second is. Toggle the individual signals on and off. Clips are blocks you can trim by dragging their edges or move by dragging their body. Scroll to zoom, right-click to add or delete. |
| **Clips tab** | Rename, include or exclude, reorder, nudge start and end, and read the transcript of each clip. **Render preview** encodes just one clip so you can check it. |
| **Find clips tab** | Weight sliders and length rules. Clips update live as you move them. Clips you've edited by hand are kept. |
| **Layout tab** | Text banners above and below the video, and a second video stacked under (or over) your clip, such as gameplay or a satisfying loop. A to-scale sketch updates as you change things, and **Live frame** draws a real frame of the selected clip exactly as it will export. Each clip can have its own banner text. The **View** picker sets up a top/bottom view in one click (Title on top, Gameplay below, Gameplay above, Meme…), and **Save…** keeps your own. |
| **Export tab** | Format, resolution, GPU encoding, captions, loudness and more. Then **Export N clips**. |
| **Library** | Every exported clip with thumbnails. Double-click to play; right-click to show in Explorer. |
| **Research** | What's trending on YouTube in the countries you pick, scored for how well each video will cut into clips. Keeps checking in the background. Send the best ones to the studio with a double-click. See [Research](#research). |

### How clips are found

Every second gets a score from up to five signals. You decide how much each one counts:

| Signal | What it captures |
|---|---|
| **Most replayed** | YouTube's own data on where viewers rewind and rewatch |
| **Audio excitement** | Moments much louder than their surroundings: laughter, shouting, crowds, drops |
| **Laughter / applause** | `[Laughter]` and `[Applause]` tags in the captions |
| **Scene cuts** | Bursts of fast editing, which usually mean action |
| **Keywords** | Mentions of words or phrases you choose |

Clips grow outward from the peaks. When there's a transcript, their edges are aligned to whole sentences, so a clip never starts or ends mid-sentence; without one, they snap to natural pauses. Intros, credits and end screens count for less.

**Other clip finders** (pick one under *Find clips → Clip finder*):
- **AI picks (Claude):** Claude reads the transcript, one sentence per line with exact times, and picks self-contained moments with a strong hook and payoff, plus a title and the reason for each. Add your Anthropic API key under *Settings → AI*, then click **Test key** to confirm it works (this uses no tokens). When you click **Re-detect clips**, a failure shows a message and leaves your clips untouched. In batch runs, a failure falls back to the signals above, and the video is flagged with the reason. Clips you've edited by hand are kept when you re-detect.
- **YouTube chapters:** one clip per chapter.

**No captions?** For local files, or videos without captions, ClipMonster transcribes speech on your PC with Whisper. The model is downloaded once on first use to `%LOCALAPPDATA%\ClipMonster\models`.

### Export options

- **Formats:** original; 9:16 vertical with a blurred fill; 9:16 vertical cropped (reframe per clip); 1:1 square
- **Animated captions:** word groups pop on screen with the spoken word highlighted. Font, size, colours, outline, position, words per screen and UPPERCASE are all adjustable. Sizes are letter heights in pixels on a 1080-wide video, so a size looks the same in every font. Optionally, the clip title appears as a banner at the top.
- **Banners:** a solid bar of text at the top, the bottom or both (the video sits between them), or the same text laid over the video. `{title}` fills in the clip title, long text wraps and shrinks to fit, and each clip can override the text or show none.
- **Stacked layout:** your clip on one part of a 9:16 frame and a looping second video on the other. You choose which goes on top, how much room the clip gets, where the second video starts (a random spot per clip, or a fixed time) and how loud it is (silent by default). Captions set to *Middle* sit on the seam.
- **Fonts:** 13 free display fonts ship with the app and look the same on every PC: TikTok Sans Black, Montserrat Black/ExtraBold, Poppins Black/ExtraBold, Archivo Black, Anton, Bebas Neue, Bangers, Luckiest Guy, Lilita One, Titan One and Permanent Marker. Pick one for captions and one for banners. Any font installed on the PC works too. Licences are in `ytclip/fonts/licenses`.
- **TikTok-ready output** (on by default): every clip comes out 9:16 at 1080×1920 (between 540×960 and 1080×1920 at other resolution settings). *Original* and *1:1* are fitted in with a blurred fill; turn this off to keep them. In 9:16 the banner text and captions stay out of the areas TikTok covers with its tabs, buttons and caption (the layout sketch shades them). Output is H.264 High with AAC 44.1 kHz audio, at most 60 fps and 20 Mbps, and at least 540×960. The log warns about any clip over 10 minutes, or over 287 MB (the phone-app limit; computers can upload up to 4 GB).
- **Encoding:** GPU encoding on AMD (AMF), NVIDIA (NVENC) or Intel (Quick Sync), detected automatically, with x264 as the fallback
- **Polish:** loudness normalisation (−14 LUFS, the level social platforms target), fade in/out, resolution, quality and audio bitrate
- **Extras:** an `.srt` subtitle file and a thumbnail per clip, plus a `README.md` and `clips.json` index for each video
- **Naming:** templates for folders (`{channel}/{date} {title} [{id}]`) and files (`{rank:02d} - {time} - {label}`)

### Presets

One click sets everything for a style: **Shorts / TikTok / Reels**, **Vertical crop (talking head)**, **Podcast moments**, **Gaming / action**, **Square posts**, **Stacked (gameplay below)**, **Banner top + bottom**, **Split by chapters**, **AI picks**. To save your own, pick *Save current settings as preset…* from the preset menu.

**Top/bottom views** are separate presets for just the layout (banners and stacked video), in the Layout tab's **View** picker. They don't change clip finding, format or captions, so you can mix any style with any view.

### Keyboard shortcuts

| Key | Action |
|---|---|
| `Space` | Play / pause |
| `V` | Export view on / off |
| `←` `→` (`Shift` for 1 s) | Seek 5 s |
| `J` / `L` | Previous / next clip |
| `P` | Play selected clip |
| `I` / `O` | Set clip start / end at the playhead |
| `N` | New clip at the playhead |
| `Delete` | Delete selected clip |
| `Ctrl+E` | Export |
| `Ctrl+O` | Open video files |
| `Ctrl+,` | Settings |

## Research

The **Research** tab watches YouTube's most-popular chart for each region you choose (US, GB, CA and AU to start) and gives every video a **clip-ability** score out of 100.

| Part | What it measures |
|---|---|
| **Momentum** (40%) | Views per hour. After the second check this is measured between checks, so you see what's taking off right now, not lifetime averages. Marked with ↑ in the table. |
| **Most replayed peaks** (30%) | Reads the video's *Most replayed* graph, using the same signal the studio's clip finder uses, and looks for sharp standout moments. Their timestamps appear in the reasons. This runs for the best candidates each scan, or any video with **Check replays**. |
| **Engagement** (12%) | Likes and comments per view. |
| **Freshness** (12%) | Clips ride a video's wave for a day or two, then the score fades. |
| **Captions** (6%) | Captions make better titles, burned-in captions and AI picks. |
| **Material** (multiplier) | Length and category. A 48-minute gaming video has plenty to cut. A 3-minute music video has little, and reposts of it usually get copyright-claimed, so it can't score high however hot it is. |

Parts that can't be measured, such as hidden like counts or a brand-new video with no replay graph yet, are left out rather than counted as zero. Live streams, premieres and Shorts are capped low, and the reasons say why.

**Setup:** it needs a free YouTube Data API key. In the [Google Cloud console](https://console.cloud.google.com/apis/library/youtube.googleapis.com), enable *YouTube Data API v3* for a project and create an API key under *Credentials*. Paste it into the Research tab, or under **Settings → Research**, or set the `YOUTUBE_API_KEY` environment variable. Each region checked costs 1 unit of the 10,000-unit daily quota (per 50 videos), so four regions every 30 minutes uses about 2% of it. Replay checks go through yt-dlp and use no quota.

New arrivals scoring at least your alert level (75 by default) are counted on the tab, as **🔥 Research (3 new)**, when you're on another tab. Videos you've sent to the studio are ticked, and **Hide sent** hides them. History and the last results are kept in `%APPDATA%\ClipMonster\research.json`, so the tab opens with your last results.

### Picking rules

ClipMonster can also decide by itself which videos to clip, so they can be cut without you looking first. The rules are under **Settings → Picking** (or **Picking rules…** on the Research tab), and the **Pick** column shows what they would pick. Hover over a dash to see why a video was skipped. **Picks only** hides the rest, and **Send N picks to Studio** sends them all.

**Where videos come from.** Besides the trending charts, Research can watch two more sources:

- **Channel list:** new uploads from channels you choose, one per line, as `@handle`, a channel link or a channel id. Put a note after `#` saying why clips are allowed, e.g. `@somepodcast  # clipping programme, credit in caption`. The note is kept with every pick from that channel. Each channel costs about 1 quota unit an hour.
- **Creative Commons searches:** YouTube searches limited to videos under a Creative Commons licence, which allows reuse with credit. Each search costs 100 quota units, so it runs at most every 6 hours.

**Pick videos from** decides what may be picked:

| Setting | Picks |
|---|---|
| **My channel list and Creative Commons only** (default) | Videos from channels on your list (even when they're trending) and Creative Commons videos |
| **Creative Commons only** | Only Creative Commons videos |
| **Anything that passes the rules** | Any video, including trending videos nobody gave permission for. Reposts like these risk copyright strikes |

Then the rules go through the videos best first. A video is skipped if it was picked or sent before, is live or not out yet, is from a blocked channel or category (Music and Film by default), has a blocked word in its title (music video, trailer…), is shorter or longer than your limits (8 to 180 minutes), is older than your limit (3 days), or scores under your minimum (65). Finally, at most **3 videos a day** and **2 per channel a week** are picked. Each skipped video shows the rule that stopped it.

Only picks that are actually used count towards the limits: sent to the Studio, or clipped from the command line. The picks are kept in `%APPDATA%\ClipMonster\picks.json` with the reason each one was allowed, so nothing is picked twice.

From the command line:

```
ytclip research                          # your regions and settings
ytclip research -r US,GB -c gaming -n 15  # top 15 gaming videos in the US and UK
ytclip research --watch 30               # keep checking every 30 min, print new arrivals
ytclip research --clip 3 --min-minutes 8 # cut the top 3 straight away with your settings
ytclip research --picks                  # what your picking rules pick, and why the rest were skipped
ytclip research --picks --clip 5 --watch 30   # keep checking, and cut whatever the rules pick
ytclip research --json > trending.json
ytclip research --list-categories
```

## Automation

The **Automation** tab runs the whole job without you: it checks Research on your schedule, queues whatever your [picking rules](#picking-rules) pick, and clips one video at a time. Press **Start**. Automation runs as its own background process, so it keeps going after you close ClipMonster. Tick **Start with Windows** to have it start every time you sign in.

- **The queue** shows every video, what it's doing now, how many clips it made and why it was picked. Right-click a video to show its clips, try it again or remove it, or paste a link above the queue to add one yourself. Finished clips go to an `Automation` folder in your output folder, and each video's `clips.json` records why it was picked and the style it was cut with.
- **It survives anything.** The queue lives in `%APPDATA%\ClipMonster\jobs.db`. If the PC restarts or the process is killed halfway through a video, that video starts again next time. Downloads and analysis are cached, so the second try is quick. No video is ever queued twice.
- **It's gentle with YouTube.** It does one download at a time, 10 minutes apart. When YouTube asks the PC to "sign in to confirm you're not a bot", it pauses downloads for 6 hours instead of pushing harder. A video that fails is tried again after 15 minutes, then after 1 hour. Videos that can never work (private, removed, members-only) are marked failed straight away.
- **Pause** finishes nothing new until you resume. **Stop** ends the run within a few seconds, and a video in progress goes back in the queue. Updating ClipMonster stops automation and starts it again afterwards.

**Settings → Automation** chooses the style and the pace:

| Setting | Default | What it does |
|---|---|---|
| Style (preset) and Top/bottom view | Shorts / TikTok / Reels | How clips are found and exported |
| Recipes | none | A different style per category or channel, one per line: `Gaming = Gaming / action \| Gameplay below`, `@somepodcast = Podcast moments \| Title on top`, `* = \| Title on top`. The first matching line wins |
| Always burn in captions | on | Word-by-word captions on every clip |
| Work from / until | any time | Hours in which downloads and encodes may start, e.g. 1 to 7 overnight |
| Minutes between downloads | 10 | Spacing that keeps YouTube from flagging the PC |
| AI clip finder runs per day | 10 | Over this, videos use the smart signals, so Claude costs stay predictable |
| Tries per video | 3 | Before a video is marked failed |
| Delete full downloads after clipping | on | Keeps the disk from filling up |
| Clear cached analysis after | 14 days | Removes transcripts and measurements of finished videos |

The runner writes a log to `%APPDATA%\ClipMonster\automation.log`, which the tab shows too. From the command line:

```
ytclip auto                    # run in this console until Ctrl+C
ytclip auto --once             # do what's due now, then exit
ytclip auto --status           # the queue and what automation is doing
ytclip auto --add "https://youtu.be/VIDEO"
ytclip auto --pause | --resume | --stop
ytclip auto --retry ID         # try a failed video again
ytclip auto --install          # start with Windows (--uninstall to undo)
```

## Output layout

```
Videos/ClipMonster/
└── TED/
    └── 2007-01-07 Do schools kill creativity [iG9CE55wbtY]/
        ├── 01 - 14-47 - If a tree falls in a forest, and.mp4
        ├── 01 - 14-47 - If a tree falls in a forest, and.srt
        ├── 02 - 5-17 - They put these boxes down.mp4
        ├── thumbnails/01.jpg …
        ├── README.md     ← clip table with thumbnails and "watch on YouTube" links
        └── clips.json    ← the same, machine readable
```

Full downloads are cached in `<output>/.downloads`, so re-analysing or re-exporting never downloads a video twice. Your settings, presets and session live in `%APPDATA%\ClipMonster`.

## Command line (`ytclip.exe`)

`ytclip.exe` uses the same settings as the app, and the options below override them for that run.

```
ytclip "https://youtu.be/VIDEO" --preset shorts
ytclip "https://www.youtube.com/@channel/videos" --limit 5 -n 3
ytclip "C:\Videos\podcast.mp4" --mode ai --captions -f vertical_crop
ytclip "https://youtu.be/VIDEO" --keywords "goal, penalty" --dry-run
ytclip "C:\Videos\stream.mp4" --layout "Gameplay below + title" --captions
ytclip "C:\Videos\stream.mp4" --stack "C:\Videos\parkour.mp4" --top-text "{title}" --bottom-text "Follow for part 2" --captions
ytclip --list-presets
```

Main options: `-n` number of clips · `--min/--max` length in seconds · `--mode auto|ai|chapters` · `-f original|vertical_blur|vertical_crop|square` · `--captions` · `--keywords` · `-q` max download resolution · `--encoder` · `--limit` videos per playlist · `-o` output folder · `--dry-run` analyse without exporting · `--cookies-from-browser chrome` for age-restricted videos.

## Updates

ClipMonster updates itself from this project's GitHub Releases.

- About once a day it checks quietly for a newer release. When one exists, a banner offers **What's new**, **Update now** and **Skip this version**. You can also check any time from **? → Check for updates**, or with `ytclip --check-update`.
- **Update now** downloads the release and checks it against its SHA-256 checksum, then restarts ClipMonster. While the app is closed, a small helper swaps in the new version and keeps the old one until the new one is in place; if anything goes wrong, it restores the old version. Settings, presets and your session are untouched. The helper writes a log to `%LOCALAPPDATA%\ClipMonster\updates\update.log`.
- Turn automatic checks off, or opt into beta versions, under **Settings → Updates**.
- The repository must be **public** so the app can see its releases without a login.

## Publishing a release

One-time setup, after creating the GitHub repository:

```
git init
git add .
git commit -m "ClipMonster 1.1.0"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Then, for every release:

```
python release.py 1.2.0
git push && git push --tags
```

`release.py` bumps the version, commits and tags. Pushing the tag starts the **Release** workflow in GitHub Actions, which takes about 10 minutes. It builds the app on Windows and publishes `ClipMonster-windows.zip` and `SHA256SUMS.txt` as a GitHub Release, with notes generated from your commits. To lead with your own notes, add `release-notes/v1.2.0.md` (named after the tag) before tagging. Every installed copy then offers the update. A tag with a suffix, such as `v1.3.0-beta.1`, becomes a pre-release, which only reaches people who opted into betas.

Builds record which repository they came from, so they need no update configuration. Local builds read it from the git remote; releases built on GitHub get it from Actions.

## Building from source

Needs Python 3.10+.

```
pip install -e .[ai,whisper,build]
python build.py
```

This produces `dist/ClipMonster/` and `dist/ClipMonster-windows.zip`. During development, run the app with `python -m ytclip.gui.app` and the command line with `python -m ytclip`.

Run the tests with `pip install -e .[test]` and then `pytest`. They need no network or API key.

| Module | Role |
|---|---|
| `ytclip/engine.py` | Pipeline: open → measure (cached) → transcribe → find clips → export |
| `ytclip/analyze.py` | Signals, scoring and clip picking |
| `ytclip/ai.py` | Claude clip finder |
| `ytclip/trending.py` | YouTube trending charts, replay checks, research history |
| `ytclip/clipability.py` | Clip-ability score for trending videos |
| `ytclip/research.py` | One research scan, shared by the Research tab and `ytclip research` |
| `ytclip/sources.py` | Channel list and Creative Commons searches for Research |
| `ytclip/policy.py` | Picking rules and the log of picks |
| `ytclip/pipeline.py` | Automation: the unattended runner, recipes, pacing and retries |
| `ytclip/jobs.py` | `jobs.db`, the automation queue shared by the runner and the app |
| `ytclip/transcribe.py` | Whisper transcription |
| `ytclip/export.py`, `captions.py` | Reframing, captions, encoding |
| `ytclip/gui/` | Desktop app (PySide6) |
| `packaging/` | PyInstaller spec, launchers, icon |

Only download and repost videos you have the rights to use.
