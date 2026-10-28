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
| **Library** | Every exported clip with thumbnails. Double-click to play; right-click to show in Explorer. Automation's videos show their niche and TikTok account, and the filter shows one niche at a time. **🧹 Clean up…** clears out what nothing needs any more (see [Clean up](#clean-up)). |
| **Research** | What's trending on YouTube in the countries you pick, scored for how well each video will cut into clips. Keeps checking in the background. Send the best ones to the studio with a double-click. See [Research](#research). |
| **Niches** | One niche for each of your TikTok accounts: where its videos come from, how many it gets a day and how its clips look. See [Niches](#niches). |

### How clips are found

Every second gets a score from up to five signals. You decide how much each one counts:

| Signal | What it captures |
|---|---|
| **Most replayed** | YouTube's own data on where viewers rewind and rewatch |
| **Audio excitement** | Moments much louder than their surroundings: laughter, shouting, crowds, drops |
| **Laughter / applause** | `[Laughter]` and `[Applause]` tags in the captions |
| **Scene cuts** | Bursts of fast editing, which usually mean action |
| **Keywords** | Mentions of words or phrases you choose |

Clips grow outward from the peaks. When there's a transcript, their edges are aligned to whole sentences, so a clip never starts or ends mid-sentence; without one, they snap to natural pauses. Intros, credits and end screens count for less. **Trim dead air** (on by default) then pulls each clip in so it starts a quarter of a second before the first word and ends half a second after the last, instead of opening on silence. Clips you've edited by hand are never trimmed.

**Clean cut points.** As a last step, ClipMonster listens to the audio around each clip edge. An edge that would land in the middle of a word moves into the nearest pause, preferring the longer pause between sentences, without making the clip longer than your maximum or running into the next clip. This matters most for YouTube's auto-generated captions, which have no punctuation: the pauses the audio shows are used to find where sentences end. A clip is never longer than *Max clip length* either: when rounding out to a whole sentence would run over, it ends on the last sentence that fits. Nor is it ever shorter than *Min clip length*: a clip that comes out short grows to the end of a later sentence (or back to the start of an earlier one), and one with no room to grow is dropped. Clip length is always yours: presets and Automation's style don't change it, only a niche's or a clip request's own length does.

**Part series.** When a great moment runs longer than one clip, it carries on in **Part 2** (and Part 3), each starting on the next sentence where the last part stopped and ending when the moment does. Parts count towards *Clips per video*, so a series takes the place of the weakest stand-alone clips, and a weak last part never pushes out a better clip. Every part except the last shows your "Follow for part 2" bottom text; if the quality check rejects a part, the part before it is made again without the promise. The clip list marks parts (P1, P2…), and `clips.json` records each clip's `series`, `part` and `parts`, counting only the parts that passed the quality check. Set the most parts in *Find clips → Part series* (3 to start with; 1 turns it off).

**Other clip finders** (pick one under *Find clips → Clip finder*):
- **AI picks (Claude):** Claude reads the transcript, one sentence per line with exact times, and picks self-contained moments with a strong hook and payoff, plus a title and the reason for each. Add your Anthropic API key under *Settings → AI*, then click **Test key** to confirm it works (this uses no tokens). When you click **Re-detect clips**, a failure shows a message and leaves your clips untouched. In batch runs, a failure falls back to the signals above, and the video is flagged with the reason. Clips you've edited by hand are kept when you re-detect.
- **YouTube chapters:** one clip per chapter.

**AI polish** (*Settings → AI → Polish clips with AI*, off by default) makes one more Claude call per video, sending each clip's transcript and one small frame from it. It sends back:
- a **hook** for each clip: a few honest, specific words that go in the top banner when it shows `{title}`, and into `clips.json` for your upload caption;
- a **bottom line** for each clip with no part 2, when your bottom text promises one (see *Banners* under [Export options](#export-options));
- **caption fixes** for words the transcript misheard, mostly names and jargon ("Joe Rogen" becomes "Joe Rogan"), swapped word for word so the timing stays right;
- a 1-10 **rating** of how well each clip stands alone, used by the [quality check](#export-options);
- where the **speaker** is, so *9:16 vertical cropped* and *1:1* keep them in shot;
- whether the video **already has subtitles** in the picture. If it does, your captions are left off for that video (or kept, if you choose), so there aren't two sets of words on screen.

Anything you've edited by hand is left alone, and if the call fails the clips simply go out unpolished. It uses Claude Sonnet 5.5 by default, which you can change.

**No captions?** For local files, or videos without captions, ClipMonster transcribes speech on your PC with Whisper. The model is downloaded once on first use to `%LOCALAPPDATA%\ClipMonster\models`. **Transcription → For the clips** gets Whisper's better captions without the wait: YouTube's captions find the clips, then Whisper types out only the clips (and three seconds either side). That's a few minutes of speech instead of the whole video. A whole video keeps a checkpoint as it goes, so a stopped run carries on where it got to. `ytclip whisper-test VIDEO` times each model on your PC.

**Speed** (Settings → Speed) sets how hard ClipMonster works:

- **Quiet** gives Whisper a quarter of the CPU threads, and automation only uses the PC when nothing else wants it.
- **Balanced** gives Whisper half the threads, and automation steps aside for what you're doing.
- **Full power** uses every thread and the graphics card. Whisper runs on the card through whisper.cpp with Vulkan, so AMD, NVIDIA and Intel cards all work. Its model is downloaded once. If the card can't be used, Whisper falls back to the CPU and the health check says why.

Unless the Speed is Quiet, scene detection decodes the video on the graphics card. Automation also downloads the next video while this one is clipped, though it still waits the minutes between downloads. With AI polish, **Find extra clips, keep the best the AI rates** finds half as many clips again and exports only the best rated. More of your clips pass the quality check, and none is encoded just to be rejected.

### Export options

- **Formats:** original; 9:16 vertical with a blurred fill; 9:16 vertical cropped (reframe per clip); 1:1 square
- **Animated captions:** word groups pop on screen with the spoken word highlighted. Font, size, colours, outline, position, words per screen and UPPERCASE are all adjustable. Sizes are letter heights in pixels on a 1080-wide video, so a size looks the same in every font. Optionally, the clip title appears as a banner at the top. **Bleep strong swearing** writes f\*ck, sh\*t and the like in captions and `.srt` files, which TikTok is less likely to hold back.
- **Banners:** a solid bar of text at the top, the bottom or both (the video sits between them), or the same text laid over the video. `{title}` fills in the clip title, long text wraps and shrinks to fit, and each clip can override the text or show none. Bottom text that promises more, like *Follow for part 2*, only shows on a clip that the next clip carries straight on from, and not when the quality check rejects that part 2. Other clips show **Bottom text when there's no part 2** (*Follow for more* to start with) or, with AI polish, a line Claude wrote for that clip. Claude's lines never promise a part 2, and they keep any @handle in your bottom text.
- **Stacked layout:** your clip on one part of a 9:16 frame and a looping second video on the other. You choose which goes on top, how much room the clip gets, where the second video starts (a random spot per clip, or a fixed time) and how loud it is (silent by default). Captions set to *Middle* sit on the seam. Choose **a folder of second videos** instead of one, and each clip gets a different one: every video in the folder is used once before any repeats, and never the same one twice in a row, even across videos and restarts. `clips.json` notes which one each clip got.
- **Fonts:** 13 free display fonts ship with the app and look the same on every PC: TikTok Sans Black, Montserrat Black/ExtraBold, Poppins Black/ExtraBold, Archivo Black, Anton, Bebas Neue, Bangers, Luckiest Guy, Lilita One, Titan One and Permanent Marker. Pick one for captions and one for banners. Any font installed on the PC works too. Licences are in `ytclip/fonts/licenses`.
- **TikTok-ready output** (on by default): every clip comes out 9:16 at 1080×1920 (between 540×960 and 1080×1920 at other resolution settings). *Original* and *1:1* are fitted in with a blurred fill; turn this off to keep them. In 9:16 the banner text and captions stay out of the areas TikTok covers with its tabs, buttons and caption (the layout sketch shades them). Output is H.264 High with AAC 44.1 kHz audio, at most 60 fps and 20 Mbps, and at least 540×960. The log warns about any clip over 10 minutes, or over 287 MB (the phone-app limit; computers can upload up to 4 GB).
- **Encoding:** GPU encoding on AMD (AMF), NVIDIA (NVENC) or Intel (Quick Sync), detected automatically, with x264 as the fallback
- **Polish:** loudness normalisation (−14 LUFS, the level social platforms target), fade in/out, resolution, quality and audio bitrate
- **Quality check** (*Check clips after export*): each clip is watched once, quickly, after it's made. A clip that's silent, mostly black or frozen, or the wrong length or size is **rejected**: it moves to a `_rejected` folder beside the others, so it never ends up in front of you as ready to upload. With AI polish on, Claude also rates how well each clip stands alone, from 1 to 10, and clips under *Reject AI ratings under* (5) are rejected too, with its reason. Clips that start or end mid-sentence, are very quiet or have hardly any talking are kept but flagged. The Library, the video's `README.md` and `clips.json` (a `qc` record per clip) all show the result. `ytclip qc FOLDER` checks existing folders again.
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

### Find videos for a request

Type what you want into **Find videos** at the top of the Research tab, in your own words, and press **Find**. For example: *Call of Duty MW4 with good comedy*.

- **Searches.** ClipMonster turns the request into one or two YouTube searches, such as "Call of Duty MW4 funny moments" and then "Call of Duty MW4". With an Anthropic key (Settings → AI), Claude writes the searches instead. It knows that MW4 is Modern Warfare 4, and which words funny videos put in their titles.
- **Match.** Every video found gets a **Match** score out of 100: how well it is what you asked for. With a key, Claude rates each one from its title, channel, tags and description. Without a key, the request's words in the title count most, then the tags and description. Videos about something else, such as another game, are left out.
- **Order.** The list is ordered mostly by Match, then by clip-ability, so a funny MW4 video that will clip well comes first.
- **Shorts are left out.** So is anything shorter than the tab's length filter.
- **Time limit.** The menu next to the box limits results to this week, this month or this year.
- **What's left out.** The bar above the table says what was searched and how many videos were left out. **← Back to trending** returns to the charts. Your last request stays one click away, and the box suggests your earlier requests.
- **Using the results.** Send them to the Studio, or use **Queue in Automation** (also on any trending video). Automation then clips them next, in their niche's style and with your clip length.
- **Cost.** A request uses about 200 of the YouTube API's 10,000 free daily units, because each search costs 100. Claude's rating is one short call.

```
ytclip find "Call of Duty MW4 with good comedy"            # the best 20, with why each matches
ytclip find "Minecraft hardcore funny" --since month -n 10
ytclip find "Kai Cenat rage" --queue 3 --niche Streamers   # add the best 3 to Automation's queue
ytclip find "MW4 funny" --no-ai --json
```

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

Then the rules go through the videos best first. A video is skipped if it was picked or sent before, is live or not out yet, is from a blocked channel or category (Music and Film by default), has a blocked word in its title (music video, trailer…), is in a language you don't pick (**Only pick videos in**, English by default; videos with no language set are allowed), is shorter or longer than your limits (8 to 180 minutes), is older than your limit (3 days for trending videos, 14 for videos from your channel list or under Creative Commons), or scores under your minimum (65). Finally, at most **3 videos a day** and **2 per channel a week** are picked. Each skipped video shows the rule that stopped it.

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

The **Automation** tab runs the whole job without you: it checks Research on your schedule, queues whatever your [picking rules](#picking-rules) pick, and clips one video at a time. Press **Start**. Automation runs as its own background process, so it keeps going after you close ClipMonster. Tick **Start with Windows** to have it start every time you sign in (it shows up in Task Manager → Startup apps as "ClipMonster Automation"). Keep the ClipMonster folder outside OneDrive, for example in `C:\Users\<you>\ClipMonster`: the health check warns when it isn't.

- **The queue** shows every video, what it's doing now, how many clips it made and why it was picked. Right-click a video to show its clips, try it again or remove it, or paste a link above the queue to add one yourself. Finished clips go to an `Automation` folder in your output folder, in a folder for each [niche](#niches) when you have them. Each video's `clips.json` records why it was picked, the style it was cut with, and its niche and TikTok account.
- **It survives anything.** The queue lives in `%APPDATA%\ClipMonster\jobs.db`. If the PC restarts or the process is killed halfway through a video, that video starts again next time. Downloads and analysis are cached, so the second try is quick. No video is ever queued twice.
- **It's gentle with YouTube.** It does one download at a time, 10 minutes apart. When YouTube asks the PC to "sign in to confirm you're not a bot", it stops asking YouTube for anything for 2 hours (6 if it asks again within a day) instead of pushing harder. Press **Try now** in the Automation tab (or run `ytclip auto --try-now`) to end the pause early. Signing in ends it by itself (see below). A video that fails is tried again after 15 minutes, then after 1 hour. Videos that can never work (private, removed, members-only) are marked failed straight away.
- **Sign in to YouTube once, and it rarely asks again.** Press **Sign in to YouTube** in the Automation tab (or Settings → Download, or run `ytclip sign-in`). It opens Firefox with a profile kept just for ClipMonster. Sign in with a spare Google account, then close that Firefox window. Every download is signed in from then on, which is what keeps YouTube from asking the PC to "confirm you're not a bot". ClipMonster never sees the password: it reads the sign-in cookies from that profile (`%LOCALAPPDATA%\ClipMonster\youtube-login`). Don't browse with that Firefox, and sign in again from the same button if YouTube ever asks again. It needs Firefox installed.
- **It looks after itself overnight.** Before free space falls under **Keep this much disk free** (15 GB to start with), it clears unfinished and finished downloads, previews and rejected clips (at most once an hour), and pauses until there's room if that isn't enough. Work that makes no progress for **Stop stuck work after** (30 minutes), such as an export that hangs, is stopped and tried once more, then skipped with the reason. If automation crashes or the PC loses power, pressing Start again takes over straight away.
- **Pause** finishes nothing new until you resume. **Stop** ends the run within a few seconds, and a video in progress goes back in the queue. Updating ClipMonster stops automation and starts it again afterwards.

**Settings → Automation** chooses the style and the pace:

| Setting | Default | What it does |
|---|---|---|
| Style (preset) and Top/bottom view | Shorts / TikTok / Reels | How clips are found and exported |
| Recipes | none | A different style per category or channel, one per line: `Gaming = Gaming / action \| Gameplay below`, `@somepodcast = Podcast moments \| Title on top`, `* = \| Title on top`. The first matching line wins |
| Always burn in captions | on | Word-by-word captions on every clip |
| Bleep strong swearing | on | Writes f\*ck, sh\*t and the like in captions and `.srt` files |
| Work from / until | any time | Hours in which downloads and encodes may start, e.g. 1 to 7 overnight |
| Minutes between downloads | 10 | Spacing that keeps YouTube from flagging the PC |
| Check clips after export | on | The [quality check](#export-options); rejected clips go to `_rejected` and the queue shows them as "3 ✓ 1 ✗" |
| Polish clips with AI | on | [AI polish](#how-clips-are-found) for every video, when there's an Anthropic key |
| AI calls per day | 10 | The AI clip finder and AI polish each count as one. Over this, videos use the smart signals and skip polishing, so Claude costs stay predictable |
| Tries per video | 3 | Before a video is marked failed |
| Delete full downloads after clipping | on | Keeps the disk from filling up |
| Clear cached analysis after | 14 days | Removes transcripts and measurements of finished videos |
| Keep this much disk free | 15 GB | Below this, automation clears what's safe to clear, then pauses until there's room. 0 turns it off |
| Stop stuck work after | 30 min | An export, measurement or check with no progress for this long is stopped and tried once more. 0 turns it off |
| Clean up by itself once a day | off | Runs [Clean up](#clean-up) once a day for the kinds ticked under *What to clear*, on things older than *Only things older than* (3 days) |

The runner writes a log to `%APPDATA%\ClipMonster\automation.log`, which the tab shows too. From the command line:

```
ytclip auto                    # run in this console until Ctrl+C
ytclip auto --once             # do what's due now, then exit
ytclip auto --status           # the queue and what automation is doing
ytclip auto --add "https://youtu.be/VIDEO"
ytclip auto --add "https://youtu.be/VIDEO" --niche Gaming   # queue it for one niche
ytclip auto --pause | --resume | --stop | --try-now
ytclip auto --retry ID         # try a failed video again
ytclip auto --install          # start with Windows (--uninstall to undo)
ytclip request 10 --length 30-45 --niche Gaming --notes "only the funniest reactions"
ytclip request 3 --link "https://youtu.be/VIDEO" "https://youtu.be/OTHER"
ytclip request --list | --cancel ID
ytclip report                  # what automation did in the last 12 hours (--hours 24, --json)
ytclip doctor                  # the health check (--offline skips checking the keys online)
```

### Clip requests

Want a set number of clips? **Automation → Requests → New request** asks for:

- **How many** clips, and **how long** each should be, e.g. 10 clips of 30 to 45 seconds;
- **which niche** they're for (their folder, style and TikTok account), or none;
- **where from:** let automation pick videos for it (with your picking rules and a YouTube Data API key), or paste the links to clip;
- **special requests** in plain words, e.g. "only the funniest reactions, end on the punchline". With an Anthropic key, the AI clip finder and the AI polish follow them.

A request goes ahead of everything else in the queue, and its videos don't count against the daily limits. The Requests page shows each one's progress ("4 of 10 ready"); it's done when enough clips passed the quality check, or "finished short" when its links had no more good moments of that length. Its clips go to `Automation\Requests\<date> <N clips a-b s>\`. A video you already clipped is clipped again for a request.

### The overnight report and the health check

**Automation → Overnight report** tells you what happened while you were away: clips made per niche and per request, how many the quality check rejected and the usual reasons, videos that failed and why, and any pauses (YouTube sign-in requests, a full disk, stuck work stopped), for the last 12 hours, 24 hours or 7 days. The tab tells you when there's something new to read. `ytclip report` prints the same.

Below it, the **health check** looks at everything automation needs: ffmpeg, how old the downloader is, your YouTube and Anthropic keys (**Check now** tries them online), the folders and free space, whether TikTok Manager watches your Automation folder, niches with nothing to find videos with, and requests that can't be filled. Each problem comes with the fix. Automation runs the offline part every time it starts and puts what it finds in the log. `ytclip doctor` runs it from the command line.

### Niches

Run a TikTok account for each topic? Give each account a niche in the **🎯 Niches** tab. A niche has:

- **The account:** a name, the TikTok account it's for, how many videos a day it gets (2 to start with), and a switch to pause it.
- **Where its videos come from:** its own channel list and Creative Commons searches. Title words and YouTube categories catch videos from the trending charts.
- **How its clips look:** its own style, top/bottom view and folder of second videos (blank ones use Settings → Automation), and a note telling Claude what makes a good clip for that account. AI picks and AI polish both read the note.

Every picked video goes into one niche: the one that lists its channel, then the one whose search found it, then the first with a matching title word, then the first with its category. A video that fits no niche isn't picked, and its Pick tooltip says why. Research's **Niche** column shows where each video would go.

- **Fair shares.** Each niche picks at most its own number of videos a day. **Videos picked per day** (Settings → Picking) is the limit for all of them together, handed out one pick at a time, and the niche with the fewest picks in the last day goes first. One busy niche can't take every pick.
- **A paused niche** holds its own channels' and searches' videos back. They aren't handed to another account.
- **Run from the Automation tab.** **Automation → Niches** shows each niche's picks today against its limit, its videos waiting and its clips today. Pause or resume it there, press **Find videos now** to check its sources straight away, or **Request clips for it**.
- **Its own clips.** A niche can set its own **clips per video** and **clip length**, over Automation's (clip length is otherwise Settings → Clip finding's). Its **special requests** note tells the AI clip finder and the AI polish what makes a good clip for that account; with an Anthropic key, a niche with a note uses the AI clip finder (within *AI calls per day*).
- **Style.** A recipe for the video's channel or category comes first, then the niche's own style and view, then a catch-all (`*`) recipe, then Settings → Automation.
- **Folders.** Each niche's clips go to `Automation\<niche>\`, and `clips.json` records the niche and its TikTok account, so TikTok Manager can stage each folder for its own account. Renaming a niche sends new clips to its new folder. Clips already made stay where they are.

Making your first niche moves your channel list and Creative Commons searches into it. If you never make a niche, nothing changes. `ytclip niches` lists your niches with what each picked today, what's waiting and its clips in the last 7 days (`--json` for scripts).

### Learning from results

TikTok Manager shares how your posted clips did: after each stats refresh it writes `%APPDATA%\ClipMonster\results.json`, with the views of every ClipMonster clip that has been up for two days, compared with the usual views on that TikTok account (so "2×" means twice your usual). With **Settings → Picking → Learn from how my clips do** on (the default), ClipMonster uses it in three ways:

- **Picking.** Channels whose clips beat your usual views get up to 12 extra clip-ability points when ranking and checking the minimum score, and channels whose clips flop lose up to 12. One lucky clip counts for little; the effect grows as a channel gets more posts. Research shows why in the Pick tooltip and the video's details, e.g. "Your clips from Some Pod get 2.4× your usual views (5 posts): +8."
- **AI polish.** Once six or more clips with hooks have results, the polish sees the hooks that worked and the ones that flopped, and writes new hooks more like the hits. Once a niche has six of its own, its videos learn from just those.
- **Tips.** The Automation tab shows **What's working**: the best and worst channels, which clip length does best, and whether the AI rating predicts views (and so whether a higher quality-check minimum would help). Settings are never changed for you.

`ytclip results` prints the same, with every channel's numbers (`--json` for scripts). Results older than 90 days are ignored.

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

Automation's clips go to `<output>/Automation` in the same layout, inside a folder for each niche when you have niches: `Automation/Gaming/<channel>/<date> <title> [<id>]/`. A clip request's clips go to `Automation/Requests/<date> <N clips a-b s>/` (inside the niche's folder when it's for one).

Full downloads are cached in `<output>/.downloads`, so re-analysing or re-exporting never downloads a video twice. Your settings, presets and session live in `%APPDATA%\ClipMonster`.

## Clean up

Clips, downloads and previews pile up. **🧹 Clean up…**, in the Library and Automation tabs, clears out what nothing needs any more:

| Kind | What it is | Ticked to start with |
|---|---|---|
| Unfinished downloads | Downloads that stopped part way and haven't been touched for half an hour | yes |
| Preview renders | Clips made by Render preview and the Layout tab's preview, in the temp folder | yes |
| Downloaded videos | Full videos kept after clipping. Clipping one again downloads it again | no |
| Saved transcripts and analysis | A video opened again is measured again without them | no |
| Clips the quality check rejected | The `_rejected` folders. TikTok Manager never takes these | yes |
| Clips you've posted | Clips TikTok Manager says you've posted. Its post history and stats stay | yes |
| Clips you removed in TikTok Manager | It never brings them back | yes |

Nothing goes until you've seen it: the window lists every file and how much space it frees. You can untick whole kinds or single files, choose **Only things older than** (posted clips count from when you posted them), or send everything to the Recycle Bin instead. Each video's `clips.json` and `README.md` are updated to match. Some things always stay:

- clips TikTok Manager still has waiting;
- anything for a video that's queued, being clipped or in the Studio's list;
- anything changed in the last 10 minutes;
- anything that isn't ClipMonster's own.

Tick **Do this by itself once a day while automation runs** and automation clears the ticked kinds once a day, at the age you chose (never less than a day).

```
ytclip clean                     # what would go, and how much space it frees
ytclip clean --list              # every file, too
ytclip clean --only posted,rejected --older-than 7 --delete
```

Nothing is removed without `--delete`. Without `--only`, it leaves downloaded videos and analysis alone.

## Command line (`ytclip.exe`)

`ytclip.exe` uses the same settings as the app, and the options below override them for that run.

```
ytclip "https://youtu.be/VIDEO" --preset shorts
ytclip "https://www.youtube.com/@channel/videos" --limit 5 -n 3
ytclip "C:\Videos\podcast.mp4" --mode ai --captions -f vertical_crop
ytclip "https://youtu.be/VIDEO" --keywords "goal, penalty" --dry-run
ytclip "C:\Videos\stream.mp4" --layout "Gameplay below + title" --captions
ytclip "C:\Videos\stream.mp4" --stack "C:\Videos\parkour.mp4" --top-text "{title}" --bottom-text "Follow for part 2" --captions
ytclip "https://youtu.be/VIDEO" --stack "C:\Videos\Gameplay" --polish --captions --bleep --qc
ytclip qc "C:\Users\me\Videos\ClipMonster"      # check exported clips again
ytclip results                                  # what's working, from TikTok Manager's stats
ytclip niches                                   # your niches and how each is doing
ytclip clean                                    # what Clean up would clear (--delete clears it)
ytclip --list-presets
```

Main options: `-n` number of clips · `--min/--max` length in seconds · `--mode auto|ai|chapters` · `-f original|vertical_blur|vertical_crop|square` · `--captions` · `--polish` AI hooks and caption fixes · `--bleep` mask swearing · `--qc` check clips after export · `--stack VIDEO|FOLDER` · `--keywords` · `-q` max download resolution · `--encoder` · `--limit` videos per playlist · `-o` output folder · `--dry-run` analyse without exporting · `--cookies-from-browser clipmonster` to download with ClipMonster's own sign-in (`ytclip sign-in`), or `chrome`, `firefox`... for a browser's.

## Updates

ClipMonster updates itself from this project's GitHub Releases.

- About once a day it checks quietly for a newer release. When one exists, a banner offers **What's new**, **Update now** and **Skip this version**. You can also check any time from **? → Check for updates**, or with `ytclip --check-update`.
- **Update now** downloads the release and checks it against its SHA-256 checksum, then restarts ClipMonster. While the app is closed, the new version's own `ytclip.exe` replaces the app's own files (`ClipMonster.exe`, `ytclip.exe` and `_internal`) in place, keeping the old ones in a hidden backup folder until the new ones are in. If anything goes wrong, it puts the old version back. File Explorer windows or terminals open on the ClipMonster folder don't get in the way, and anything else you keep in the folder is left alone. Background automation is paused for the update and starts again afterwards. Settings, presets and your session are untouched. If an update doesn't complete, the app says why when it next opens. The helper writes a log to `%LOCALAPPDATA%\ClipMonster\updates\update.log`. Once the new version opens, it removes the downloaded zip and the unpacked copy.
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

This produces `dist/ClipMonster/` and `dist/ClipMonster-windows.zip`. To include Whisper for the graphics card, first install the Vulkan SDK and run `python packaging/build_whisper_cpp.py build/whisper-cpp --vulkan`; `build.py` then copies it in. The release workflow does both. During development, run the app with `python -m ytclip.gui.app` and the command line with `python -m ytclip`.

Run the tests with `pip install -e .[test]` and then `pytest`. They need no network or API key.

| Module | Role |
|---|---|
| `ytclip/engine.py` | Pipeline: open → measure (cached) → transcribe → find clips → export |
| `ytclip/analyze.py` | Signals, scoring and clip picking |
| `ytclip/ai.py` | Claude clip finder |
| `ytclip/polish.py` | AI polish: hooks, caption fixes, speaker crops, burned-in subtitle check |
| `ytclip/qc.py` | Quality check: silence, black/frozen picture, length/size, mid-sentence edges, AI ratings |
| `ytclip/stackpool.py` | Deals a different second video to each clip from a folder |
| `ytclip/trending.py` | YouTube trending charts, replay checks, research history |
| `ytclip/clipability.py` | Clip-ability score for trending videos |
| `ytclip/research.py` | One research scan, shared by the Research tab and `ytclip research` |
| `ytclip/sources.py` | Channel list and Creative Commons searches for Research |
| `ytclip/policy.py` | Picking rules, each niche's share, and the log of picks |
| `ytclip/niches.py` | Niches: each TikTok account's sources, look and folder, and which niche a video goes in |
| `ytclip/cleanup.py` | Clean up: what nothing needs any more, and clearing it |
| `ytclip/pipeline.py` | Automation: the unattended runner, recipes, pacing and retries |
| `ytclip/jobs.py` | `jobs.db`, the automation queue shared by the runner and the app |
| `ytclip/transcribe.py` | Whisper transcription: whole videos (with checkpoints) or just the clips |
| `ytclip/whisper_cpp.py` | Whisper on the graphics card (whisper.cpp with Vulkan) |
| `ytclip/speed.py` | Speed: Whisper threads, automation's priority, the graphics card |
| `ytclip/export.py`, `captions.py` | Reframing, captions, encoding |
| `ytclip/gui/` | Desktop app (PySide6) |
| `packaging/` | PyInstaller spec, launchers, icon, the whisper.cpp build (`build_whisper_cpp.py`) |

Only download and repost videos you have the rights to use.
