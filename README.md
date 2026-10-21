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
| **Preview** | Plays the video. When the format is 9:16 cropped or 1:1, a frame shows exactly what will be kept. Drag it to reframe each clip. |
| **Interest timeline** | The purple curve is how interesting each second is. Toggle the individual signals on and off. Clips are blocks you can trim by dragging their edges or move by dragging their body. Scroll to zoom, right-click to add or delete. |
| **Clips tab** | Rename, include or exclude, reorder, nudge start and end, and read the transcript of each clip. **Render preview** encodes just one clip so you can check it. |
| **Find clips tab** | Weight sliders and length rules. Clips update live as you move them. Clips you've edited by hand are kept. |
| **Layout tab** | Text banners above and below the video, and a second video stacked under (or over) your clip, such as gameplay or a satisfying loop. A to-scale sketch updates as you change things, and **Live frame** draws a real frame of the selected clip exactly as it will export. Each clip can have its own banner text. |
| **Export tab** | Format, resolution, GPU encoding, captions, loudness and more. Then **Export N clips**. |
| **Library** | Every exported clip with thumbnails. Double-click to play; right-click to show in Explorer. |

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
- **Animated captions:** word groups pop on screen with the spoken word highlighted. Font, size, colours, outline, position, words per screen and UPPERCASE are all adjustable. Optionally, the clip title appears as a banner at the top.
- **Banners:** a solid bar of text at the top, the bottom or both (the video sits between them), or the same text laid over the video. `{title}` fills in the clip title, long text shrinks to fit, and each clip can override the text.
- **Stacked layout:** your clip on one part of a 9:16 frame and a looping second video on the other. You choose which goes on top, how much room the clip gets, where the second video starts (a random spot per clip, or a fixed time) and how loud it is (silent by default). Captions set to *Middle* sit on the seam.
- **Encoding:** GPU encoding on AMD (AMF), NVIDIA (NVENC) or Intel (Quick Sync), detected automatically, with x264 as the fallback
- **Polish:** loudness normalisation (−14 LUFS, the level social platforms target), fade in/out, resolution, quality and audio bitrate
- **Extras:** an `.srt` subtitle file and a thumbnail per clip, plus a `README.md` and `clips.json` index for each video
- **Naming:** templates for folders (`{channel}/{date} {title} [{id}]`) and files (`{rank:02d} - {time} - {label}`)

### Presets

One click sets everything for a style: **Shorts / TikTok / Reels**, **Vertical crop (talking head)**, **Podcast moments**, **Gaming / action**, **Square posts**, **Stacked (gameplay below)**, **Banner top + bottom**, **Split by chapters**, **AI picks**. To save your own, pick *Save current settings as preset…* from the preset menu.

### Keyboard shortcuts

| Key | Action |
|---|---|
| `Space` | Play / pause |
| `←` `→` (`Shift` for 1 s) | Seek 5 s |
| `J` / `L` | Previous / next clip |
| `P` | Play selected clip |
| `I` / `O` | Set clip start / end at the playhead |
| `N` | New clip at the playhead |
| `Delete` | Delete selected clip |
| `Ctrl+E` | Export |
| `Ctrl+O` | Open video files |
| `Ctrl+,` | Settings |

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

`release.py` bumps the version, commits and tags. Pushing the tag starts the **Release** workflow in GitHub Actions, which takes about 10 minutes. It builds the app on Windows and publishes `ClipMonster-windows.zip` and `SHA256SUMS.txt` as a GitHub Release, with notes generated from your commits. Every installed copy then offers the update. A tag with a suffix, such as `v1.3.0-beta.1`, becomes a pre-release, which only reaches people who opted into betas.

Builds record which repository they came from, so they need no update configuration. Local builds read it from the git remote; releases built on GitHub get it from Actions.

## Building from source

Needs Python 3.10+.

```
pip install -e .[ai,whisper,build]
python build.py
```

This produces `dist/ClipMonster/` and `dist/ClipMonster-windows.zip`. During development, run the app with `python -m ytclip.gui.app` and the command line with `python -m ytclip`.

| Module | Role |
|---|---|
| `ytclip/engine.py` | Pipeline: open → measure (cached) → transcribe → find clips → export |
| `ytclip/analyze.py` | Signals, scoring and clip picking |
| `ytclip/ai.py` | Claude clip finder |
| `ytclip/transcribe.py` | Whisper transcription |
| `ytclip/export.py`, `captions.py` | Reframing, captions, encoding |
| `ytclip/gui/` | Desktop app (PySide6) |
| `packaging/` | PyInstaller spec, launchers, icon |

Only download and repost videos you have the rights to use.
