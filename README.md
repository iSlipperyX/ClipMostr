# ClipMonster

ClipMonster runs TikTok clip accounts by itself. It finds YouTube videos worth clipping, cuts and edits them into vertical clips with captions, posts each one to the right TikTok account on a schedule, reads how they do, keeps a record of every posted video and deletes its file once it's up. Set it up once and leave it running.

You can still do it by hand: paste YouTube links (or drop in your own video files), fine-tune the clips on a timeline and export them in landscape, vertical or square format. And from your phone, a [Telegram bot](#your-phone-telegram) of your own asks automation for videos, shows you what's waiting to post and deletes the bad ones from the PC.

It comes as a Windows desktop app, **`ClipMonster.exe`**, plus a command-line tool, **`ytclip.exe`**, for batch jobs and scripts. It also installs on a [Linux server](#linux-host-google-cloud), such as a Google Cloud VM, which you run from your Mac. Since 3.0.0 it includes what used to be the separate TikTok Manager app: one install, one set of updates.

---

## Running it

Download **`ClipMonster-Setup.exe`** from the [newest release](https://github.com/iSlipperyX/ClipMostr/releases/latest) and run it. It installs for you only, with no admin prompt, into `%LOCALAPPDATA%\Programs\ClipMonster`, and adds ClipMonster to the Start menu and to Windows' Installed apps (uninstall it from there). Python isn't needed: ffmpeg, Qt, Whisper and the YouTube downloader are all inside.

**The first start opens Set up ClipMonster**, about ten minutes, every step skippable:

1. **Tools.** It checks for what it can't bring along: [Node.js](https://nodejs.org) (YouTube downloads), Firefox (the YouTube sign-in) and Microsoft Edge or Chrome (posting to TikTok).
2. **Keys.** A Gemini key (free) or a Claude key for the AI, and a YouTube Data API key for finding videos. **Test keys** tries them.
3. **YouTube sign-in**, in a Firefox profile of ClipMonster's own.
4. **TikTok accounts.** Add each account and log in to it once.
5. **Your first category:** what it clips and which account it posts to.
6. **The posting schedule:** 2 posts a day, between 11:00 and 21:00, and a warm-up for new accounts.
7. **Start with Windows**, and start automation.

**Finish later** keeps what you filled in, and the setup opens again next time. **Settings → General → Set up again** opens it whenever you like.

Where things live:

| | Where |
|---|---|
| The program | `%LOCALAPPDATA%\Programs\ClipMonster` |
| Settings, categories, the automation queue and history, sign-ins | `%APPDATA%\ClipMonster` |
| TikTok accounts, the posting queue and what was posted | `%APPDATA%\ClipMonster\posting` |
| Each TikTok account's browser login | `%LOCALAPPDATA%\ClipMonster\posting\browser-profiles` |
| Updates, the previous version, models and temporary work | `%LOCALAPPDATA%\ClipMonster` |
| Your clips, until they're posted | the output folder in Settings → General |
| A record of every posted video | `Documents\ClipMonster\Posted videos` |

None of it is in OneDrive. A copy unzipped by hand (before 2.8.0) offers **Install now**, which moves it to the installer in one step and then offers to remove the old copy.

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
| **Library** | Every exported clip with thumbnails. Double-click to play; right-click to show in Explorer. Automation's videos show their category and TikTok account, and the filter shows one category at a time. **🧹 Clean up…** clears out what nothing needs any more (see [Clean up](#clean-up)). |
| **Research** | What's trending on YouTube in the countries you pick, scored for how well each video will cut into clips. Keeps checking in the background. Send the best ones to the studio with a double-click. See [Research](#research). |
| **Categories** | One category for each of your TikTok accounts: where its videos come from, how many it gets a day and how its clips look. See [Categories](#categories). |

### How clips are found

Every second gets a score from up to seven signals. You decide how much each one counts:

| Signal | What it captures |
|---|---|
| **Most replayed** | YouTube's own data on where viewers rewind and rewatch |
| **Audio excitement** | Moments much louder than their surroundings: laughter, shouting, crowds, drops |
| **Laughter / applause** | `[Laughter]` and `[Applause]` tags in the captions |
| **Scene cuts** | Bursts of fast editing, which usually mean action |
| **Keywords** | Mentions of words or phrases you choose |
| **Animated voice** | Pitch and loudness swinging about: someone getting worked up |
| **Emotional words** | Strong feeling in the transcript, and sharp swings from calm to outraged |

Clips grow outward from the peaks. When there's a transcript, their edges are aligned to whole sentences, so a clip never starts or ends mid-sentence; without one, they snap to natural pauses. Intros, credits and end screens count for less. **Trim dead air** (on by default) then pulls each clip in so it starts a quarter of a second before the first word and ends half a second after the last, instead of opening on silence. Clips you've edited by hand are never trimmed.

**Clean cut points.** As a last step, ClipMonster listens to the audio around each clip edge. An edge that would land in the middle of a word moves into the nearest pause, preferring the longer pause between sentences, without making the clip longer than your maximum or running into the next clip. This matters most for YouTube's auto-generated captions, which have no punctuation: the pauses the audio shows are used to find where sentences end. A clip is never longer than *Max clip length* either: when rounding out to a whole sentence would run over, it ends on the last sentence that fits. Nor is it ever shorter than *Min clip length*: a clip that comes out short grows to the end of a later sentence (or back to the start of an earlier one), and one with no room to grow is dropped. Clip length is always yours: presets and Automation's style don't change it, only a category's or a clip request's own length does.

**Part series.** When a great moment runs longer than one clip, it carries on in **Part 2** (and Part 3), each starting on the next sentence where the last part stopped and ending when the moment does. Parts count towards *Clips per video*, so a series takes the place of the weakest stand-alone clips, and a weak last part never pushes out a better clip. Every part except the last shows your "Follow for part 2" bottom text; if the quality check rejects a part, the part before it is made again without the promise. The clip list marks parts (P1, P2…), and `clips.json` records each clip's `series`, `part` and `parts`, counting only the parts that passed the quality check. Set the most parts in *Find clips → Part series* (3 to start with; 1 turns it off).

**Other clip finders** (pick one under *Find clips → Clip finder*):
- **AI picks (Claude or Gemini):** the AI reads the transcript, one sentence per line with exact times, and picks self-contained moments with a strong hook and payoff, plus a title and the reason for each. Under *Settings → AI*, choose **Claude** (Anthropic API key, from [console.anthropic.com](https://console.anthropic.com/settings/keys)) or **Gemini** (Google's Gemini API key, free from [aistudio.google.com/apikey](https://aistudio.google.com/apikey)), add its key, then click **Test keys** to confirm it works (this uses no tokens). Each AI has its own models for finding and for polishing. With both keys and **If it fails, try the other AI** on (the default), a video the chosen AI can't do (a used-up quota, busy servers, a refusal) goes to the other one. The keys can also come from the `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) environment variables, and `--ai claude` or `--ai gemini` picks one for a single command-line run. Google may use what free Gemini keys send to improve its products; keys with billing turned on aren't used that way. When you click **Re-detect clips**, a failure shows a message and leaves your clips untouched. In batch runs, a failure falls back to the signals above, and the video is flagged with the reason. Clips you've edited by hand are kept when you re-detect.
- **Local AI (on this PC):** the same job done by a language model running on your own PC: free, private, offline, no key. Choose **Local AI on this PC** under *Settings → AI*. ClipMonster reads the graphics card's memory and picks the biggest model that fits on the card whole, on a typical day:
  - **Compact:** Llama 3.2 3B (2.0 GB download), for a card with up to 6 GB, or no card: it stays quick on the CPU.
  - **Standard:** Qwen2.5 7B (4.7 GB), for 8 to 10 GB.
  - **High-capacity:** Mistral Nemo 12B (7.5 GB), for 11 to 20 GB, such as a 16 GB Radeon RX 9060 XT, where all of it runs on the card with a 32,768-token memory.
  - **Ultra:** Qwen2.5 32B (19.9 GB), for 24 GB and more. **Command R 32B (08-2024)** is in the list too, but only when you choose it: its licence (CC BY-NC 4.0) doesn't allow commercial use, which clips that earn money may count as.

  You can choose any of them, or your own `.gguf` file. A model too big for the card still runs: the layers that don't fit stay in RAM and run on the CPU (slower; Ultra on a 16 GB card puts 38 of its 65 layers on the card). Each time the model starts, ClipMonster reads the model file's own header and how much graphics memory and RAM are free right then, and works out the split and how much of the transcript the model can hold at once (its memory, up to 32,768 tokens). It keeps the whole model on the card when it can, giving up at most half of that memory for it, and always leaves part of the card free (1 GB, or 7% of a big card) so Windows and your other programs never run it out. A memory over 8,192 tokens is stored at 8 bits: half the size, with no real loss. The model downloads once, to `%LOCALAPPDATA%\ClipMonster\models\llm`, and a stopped download carries on next time. *Pick for this PC* never starts a big download by itself in the middle of a run: it keeps using the model you have until you download the better one (`ytclip local-ai download`, or choose it in Settings).

  It runs through llama.cpp, on the graphics card with Vulkan (AMD, NVIDIA and Intel), or on the CPU. If the card runs out of memory while the model loads, it tries fewer layers, then the CPU. If the card fails part-way through an answer (a driver timeout, "device lost"), the same question is asked again on the CPU, so the video still gets its clips; the card is left alone for half an hour, and what happened goes into `automation.log`. The answers are held to a strict JSON grammar, so they're always readable: start and end, a headline, how strong the hook is (1.0 to 10.0), the category, the pacing (*Dramatic Push-in* gives the clip the slow push-in), what makes it work (Shock/Disbelief, Curiosity Gap, Storytelling…) and how long a clip it is. A moment it marks as a Creator Rewards one but that's shorter than a minute is lengthened to the next sentence end past 62 seconds. Long videos are read in overlapping parts. With a cloud AI chosen and *If it fails, try the other AI* on, the local AI also takes over when the cloud one fails, once its model is downloaded. `ytclip local-ai` shows what each model would run on (how many layers on the card, how long a memory); `ytclip local-ai test` loads it and asks it to pick a clip.
- **YouTube chapters:** one clip per chapter.

With no AI at all, two more signals help the smart signals: **Animated voice** (how much the pitch and loudness swing, measured from the audio) and **Emotional words** (how strong, and how sharply changing, the transcript's sentiment is). They're sliders like the others.

### Pro edit

*Settings → Editing* turns a clip into an edit, not a cut-out. The **Shorts / TikTok / Reels** preset turns it all on (except music and B-roll, which need your own files):

- **Follow the speaker:** faces are tracked (OpenCV's YuNet), the one whose mouth moves is the speaker, and a virtual camera follows them. It eases after them, ignores small movements, and cuts straight to the other person when the speaker changes, the way an editor would.
- **Punch-ins and push-ins:** the shot alternates between wide and 8% closer at each new sentence, and stories and curiosity clips slowly push in (to 115% over 10 s), starting again at each change of topic.
- **Cut dead air and filler words:** pauses over 0.4 s shrink to 0.12 s, and "um", "uh" and set-apart fillers ("it was, *like*, huge") go. Each join has a 5 ms fade, so there's no click. The captions follow the cut.
- **Kinetic captions:** 1 to 3 words at a time in TikTok's safe zone, popping in (80% → 110% → 100%) with the spoken word highlighted. Money words go green, danger words red, questions yellow (colours are yours to change). With *Bleep swearing in the sound*, swearing gets an 800 Hz bleep (or silence) as well as the masked caption.
- **Mastered sound:** an 80 Hz rumble filter, a gentle speech compressor and two-pass loudness to −14 LUFS with a −1 dB true-peak ceiling.
- **Music under the voice:** a track from your *Music folder* (licensed music only), picked by the clip's mood when the folder has subfolders like `Suspense` or `Upbeat`, and ducked about 16 dB whenever someone speaks.
- **Hook card:** the clip opens with its hook as a bold headline over the dimmed picture for 1.8 s, while the clip plays on under it.
- **Context lower-third:** the topic and "SOURCE · channel" slide in at the bottom for a few seconds, crediting where it's from.
- **B-roll:** every 15 to 20 seconds, a 2.5 s cutaway from your *B-roll folder*, chosen by matching its file and folder names to the words being said (`money/counting cash.mp4` shows when they talk about cash). Only your own footage is used.

When the pro edit can't make a clip, the plain version is made instead and the log says why.

### Creator Rewards

TikTok's Creator Rewards Program pays for views on videos longer than one minute. **Creator Rewards length** (*Settings → Editing*, or the **Creator Rewards (over 1 min)** preset, or `--creator-rewards`) finds 62 to 88 second clips, and a clip that the dead-air cuts would bring under 61 seconds runs on to the end of a later sentence. The **Performance** tab shows each account's progress to the program's entry goals: 10,000 followers and 100,000 views in the last 30 days, with how long each should take at the recent pace and how many recent posts are over a minute. New accounts already start with a two-week warm-up (no posts for 3 days, then one a day, then two).

**AI polish** (*Settings → AI → Polish clips with AI*, off by default) makes one more AI call per video (to Claude or Gemini, whichever Settings → AI uses), sending each clip's transcript and one small frame from it. It sends back:
- a **hook** for each clip: a few honest, specific words that go in the top banner when it shows `{title}`, and into `clips.json` for your upload caption;
- a **bottom line** for each clip with no part 2, when your bottom text promises one (see *Banners* under [Export options](#export-options));
- **caption fixes** for words the transcript misheard, mostly names and jargon ("Joe Rogen" becomes "Joe Rogan"), swapped word for word so the timing stays right;
- a 1-10 **rating** of how well each clip stands alone, used by the [quality check](#export-options);
- where the **speaker** is, so *9:16 vertical cropped* and *1:1* keep them in shot;
- whether the video **already has subtitles** in the picture. If it does, your captions are left off for that video (or kept, if you choose), so there aren't two sets of words on screen.

Anything you've edited by hand is left alone, and if the call fails the clips simply go out unpolished. It uses Claude Sonnet 5.5 or Gemini 3.8 Flash by default, which you can change.

**No captions?** For local files, or videos without captions, ClipMonster transcribes speech on your PC with Whisper. The model is downloaded once on first use to `%LOCALAPPDATA%\ClipMonster\models`. **Transcription → For the clips** gets Whisper's better captions without the wait: YouTube's captions find the clips, then Whisper types out only the clips (and three seconds either side). That's a few minutes of speech instead of the whole video. A whole video keeps a checkpoint as it goes, so a stopped run carries on where it got to. `ytclip whisper-test VIDEO` times each model on your PC.

**Speed** (Settings → Speed) sets how hard ClipMonster works:

- **Quiet** gives Whisper a quarter of the CPU threads, and automation only uses the PC when nothing else wants it.
- **Balanced** gives Whisper half the threads, and automation steps aside for what you're doing.
- **Full power** uses every thread and the graphics card. Whisper runs on the card through whisper.cpp with Vulkan, so AMD, NVIDIA and Intel cards all work. Its model is downloaded once. If the card can't be used, Whisper falls back to the CPU and the health check says why. If the card fails part-way (a driver timeout, "device lost", running out of memory, or whisper.cpp going quiet), the same video is typed out again on the CPU with every thread, so the run carries on; `automation.log` says what happened, and the card is left alone for half an hour (after three failures in a row, until you press *Try the graphics card again* in *Settings → Speed*). When it's the local AI's model filling the card, the local AI is stopped to make room instead (it starts again when it's next needed).

Unless the Speed is Quiet, scene detection decodes the video on the graphics card. Automation also downloads the next video while this one is clipped, though it still waits the minutes between downloads. With AI polish, **Find extra clips, keep the best the AI rates** finds half as many clips again and exports only the best rated. More of your clips pass the quality check, and none is encoded just to be rejected.

### Export options

- **Formats:** original; 9:16 vertical with a blurred fill; 9:16 vertical cropped (reframe per clip); 1:1 square
- **Animated captions:** word groups pop on screen with the spoken word highlighted. Font, size, colours, outline, position, words per screen and UPPERCASE are all adjustable. Sizes are letter heights in pixels on a 1080-wide video, so a size looks the same in every font. Optionally, the clip title appears as a banner at the top. **Bleep strong swearing** writes f\*ck, sh\*t and the like in captions and `.srt` files, which TikTok is less likely to hold back.
- **Banners:** a solid bar of text at the top, the bottom or both (the video sits between them), or the same text laid over the video. `{title}` fills in the clip title, long text wraps and shrinks to fit, and each clip can override the text or show none. Bottom text that promises more, like *Follow for part 2*, only shows on a clip that the next clip carries straight on from, and not when the quality check rejects that part 2. Other clips show **Bottom text when there's no part 2** (*Follow for more* to start with) or, with AI polish, a line the AI wrote for that clip. The AI's lines never promise a part 2, and they keep any @handle in your bottom text.
- **Stacked layout:** your clip on one part of a 9:16 frame and a looping second video on the other. You choose which goes on top, how much room the clip gets, where the second video starts (a random spot per clip, or a fixed time) and how loud it is (silent by default). Captions set to *Middle* sit on the seam. Choose **a folder of second videos** instead of one, and each clip gets a different one: every video in the folder is used once before any repeats, and never the same one twice in a row, even across videos and restarts. `clips.json` notes which one each clip got.
- **Fonts:** 13 free display fonts ship with the app and look the same on every PC: TikTok Sans Black, Montserrat Black/ExtraBold, Poppins Black/ExtraBold, Archivo Black, Anton, Bebas Neue, Bangers, Luckiest Guy, Lilita One, Titan One and Permanent Marker. Pick one for captions and one for banners. Any font installed on the PC works too. Licences are in `ytclip/fonts/licenses`.
- **TikTok-ready output** (on by default): every clip comes out 9:16 at 1080×1920 (between 540×960 and 1080×1920 at other resolution settings). *Original* and *1:1* are fitted in with a blurred fill; turn this off to keep them. In 9:16 the banner text and captions stay out of the areas TikTok covers with its tabs, buttons and caption (the layout sketch shades them). Output is H.264 High with AAC 44.1 kHz audio, at most 60 fps and 20 Mbps, and at least 540×960. The log warns about any clip over 10 minutes, or over 287 MB (the phone-app limit; computers can upload up to 4 GB).
- **Encoding:** GPU encoding on AMD (AMF), NVIDIA (NVENC) or Intel (Quick Sync), detected automatically, with x264 as the fallback
- **Polish:** loudness normalisation (−14 LUFS, the level social platforms target), fade in/out, resolution, quality and audio bitrate
- **Quality check** (*Check clips after export*): each clip is watched once, quickly, after it's made. A clip that's silent, mostly black or frozen, or the wrong length or size is **rejected**: it moves to a `_rejected` folder beside the others, so it never ends up in front of you as ready to upload. With AI polish on, the AI also rates how well each clip stands alone, from 1 to 10, and clips under *Reject AI ratings under* (5) are rejected too, with its reason. Clips that start or end mid-sentence, are very quiet or have hardly any talking are kept but flagged. The Library, the video's `README.md` and `clips.json` (a `qc` record per clip) all show the result. `ytclip qc FOLDER` checks existing folders again.
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

New arrivals scoring at least your alert level (75 by default) are counted on the tab, as **🔥 Research (3 new)**, when you're on another tab. History and the last results are kept in `%APPDATA%\ClipMonster\research.json`, so the tab opens with your last results.

**Fresh results, not the same ones.** Videos you've already used (picked, queued or clipped by automation, or sent to the Studio) are ticked, and **Hide used** (on to start with) hides them. The **Seen** column says when Research first saw each video: **New** for the last day, otherwise how long it's been showing up, or **Used**. **New only** shows just the new ones. With your categories set up, each region's chart for the categories your categories use (up to four) is checked too, at 1 unit each, so a Gaming category sees the Gaming chart and not just the all-categories one (Settings → Research → *Also check my categories' category charts*).

### Velocity, saturation and opportunity badges

Each scan also measures the market side of every video (`ytclip/research/velocity.py`):

- **Velocity delta**: its views per hour against the median of the same channel's other videos seen over the last 7 days. 1x is a normal upload for that channel; 3x is breaking out. A channel seen fewer than three times is measured against its category in the scan, then the whole scan, and the detail panel says which.
- **Saturation**: for the fastest long-form risers, one YouTube search counts the Shorts *other* channels have posted of it since it went up. Each check costs 100 quota units, so it's 2 a scan by default, at most 20 a day, and each video once per 12 hours (Settings → Research → *Saturation checks per scan*; 0 turns it off). YouTube Shorts is the one short-video platform with a search open to everyone, so it stands in for TikTok and Reels, which can't be checked.
- **Opportunity** (0-100) blends the delta, the clip-ability score and the replay peaks, cut down by saturation.
- **Badges**, on each video and rolled up on the category pills: 🔥 **Hot Arbitrage** (2x or more, at least 8 minutes long, and few Shorts of it found), 🔁 **High Retention** (sharp Most replayed peaks), 🧊 **Oversaturated** (many Shorts already), 📈 **Rising** (2x or more, saturation not checked). *Hot Arbitrage* is only shown when saturation was measured.

### Find videos for a request

Type what you want into **Find videos** at the top of the Research tab, in your own words, and press **Find**. For example: *Call of Duty MW4 with good comedy*.

- **Searches.** ClipMonster turns the request into two YouTube searches, such as "Call of Duty MW4 funny moments" and then "Call of Duty MW4". With a Claude or Gemini key (Settings → AI), the AI writes the searches instead. It knows that MW4 is Modern Warfare 4, and which words funny videos put in their titles.
- **Something new every time.** A request keeps up to eight searches ("funniest moments", "hilarious moments", "try not to laugh"…; the AI writes new ones once a week) and each run takes the next two, sorted the next way: best match, then newest, then most viewed. When a page of results is all videos you've seen or used, it reads the next page. Videos you've already used are left out before anything is rated, and videos the request showed before keep their Match (the AI isn't asked again) and go below the new ones, which are marked **New**. **➕ Find more** under the bar searches again and adds only videos not in the list yet. All of this is remembered per request in `%APPDATA%\ClipMonster\fresh.json`, and standing requests use it too.
- **Match.** Every video found gets a **Match** score out of 100: how well it is what you asked for. With a key, the AI rates each one from its title, channel, tags and description. Without a key, the request's words in the title count most, then the tags and description. Videos about something else, such as another game, are left out.
- **Order.** The list is ordered mostly by Match, then by clip-ability, so a funny MW4 video that will clip well comes first.
- **Shorts are left out.** So is anything shorter than the tab's length filter.
- **Time limit.** The menu next to the box limits results to this week, this month or this year.
- **What's left out.** The bar above the table says what was searched and how many videos were left out. **← Back to trending** returns to the charts. Your last request stays one click away, and the box suggests your earlier requests.
- **Using the results.** Send them to the Studio, or use **Queue in Automation** (also on any trending video). Automation then clips them next, in their category's style and with your clip length.
- **Cost.** A request uses about 200 of the YouTube API's 10,000 free daily units, because each search costs 100 (300 when it reads a second page past repeats). The AI's rating is one short call, for the videos it hasn't rated before.

```
ytclip find "Call of Duty MW4 with good comedy"            # the best 20, with why each matches
ytclip find "Minecraft hardcore funny" --since month -n 10
ytclip find "Kai Cenat rage" --queue 3 --category Streamers   # add the best 3 to Automation's queue
ytclip find "MW4 funny" --no-ai --json
```

### Picking rules

ClipMonster can also decide by itself which videos to clip, so they can be cut without you looking first. The rules are under **Settings → Picking** (or **Picking rules…** on the Research tab), and the **Pick** column shows what they would pick. Hover over a dash to see why a video was skipped. **Picks only** hides the rest, and **Send N picks to Studio** sends them all.

**Where videos come from.** Besides the trending charts, Research can watch two more sources:

- **Channel list:** new uploads from channels you choose, one per line, as `@handle`, a channel link or a channel id. Put a note after `#` saying why clips are allowed, e.g. `@somepodcast  # clipping programme, credit in caption`. The note is kept with every pick from that channel. Each channel costs about 1 quota unit an hour.
- **Creative Commons searches:** YouTube searches limited to videos under a Creative Commons licence, which allows reuse with credit. Each search costs 100 quota units, so it runs at most every 6 hours. Each run sorts the next way (most viewed, newest, best match), and what earlier runs found stays a candidate until it's two weeks old, so the list grows instead of repeating.

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

- **Status bars.** While a video is clipped, the panel above the queue shows its steps (Download, Analyse, Transcribe, Find clips, AI polish, Export, Quality check: done ones ticked, the current one with its own percent), a bar for the whole video and about how long is left. The estimate learns from how long each step took on the last video. Between videos it shows what automation is checking (the charts, a standing request's searches). The video's row in the queue has a bar too, and so does a video downloading early. The line under the queue says what posting is doing: the next post, clips waiting, and anything that needs you. The Studio's list and status line, Research and update downloads show the same kind of bar.
- **The queue** shows every video, what it's doing now, how many clips it made and why it was picked. Right-click a video to show its clips, try it again or remove it, or paste a link above the queue to add one yourself. Finished clips go to an `Automation` folder in your output folder, in a folder for each [category](#categories) when you have them. Each video's `clips.json` records why it was picked, the style it was cut with, and its category and TikTok account.
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
| Polish clips with AI | on | [AI polish](#how-clips-are-found) for every video, when there's a Claude or Gemini key |
| AI calls per day | 10 | The AI clip finder and AI polish each count as one. Over this, videos use the smart signals and skip polishing, so AI costs stay predictable |
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
ytclip auto --add "https://youtu.be/VIDEO" --category Gaming   # queue it for one category
ytclip auto --pause | --resume | --stop | --try-now
ytclip auto --retry ID         # try a failed video again
ytclip auto --install          # start with Windows (--uninstall to undo)
ytclip request 10 --length 30-45 --category Gaming --notes "only the funniest reactions"
ytclip request 3 --link "https://youtu.be/VIDEO" "https://youtu.be/OTHER"
ytclip request --list | --cancel ID
ytclip report                  # what automation did in the last 12 hours (--hours 24, --json)
ytclip doctor                  # the health check (--offline skips checking the keys online)
```

### Clip requests

Want a set number of clips? **Automation → Requests → New request** asks for:

- **How many** clips, and **how long** each should be, e.g. 10 clips of 30 to 45 seconds;
- **which category** they're for (their folder, style and TikTok account), or none;
- **where from:** let automation pick videos for it (with your picking rules and a YouTube Data API key), or paste the links to clip;
- **special requests** in plain words, e.g. "only the funniest reactions, end on the punchline". With a Claude or Gemini key, the AI clip finder and the AI polish follow them.

A request goes ahead of everything else in the queue, and its videos don't count against the daily limits. The Requests page shows each one's progress ("4 of 10 ready"); it's done when enough clips passed the quality check, or "finished short" when its links had no more good moments of that length. Its clips go to `Automation\Requests\<date> <N clips a-b s>\`. A video you already clipped is clipped again for a request.

### The overnight report and the health check

**Automation → Overnight report** tells you what happened while you were away: clips made per category and per request, how many the quality check rejected and the usual reasons, videos that failed and why, and any pauses (YouTube sign-in requests, a full disk, stuck work stopped), for the last 12 hours, 24 hours or 7 days. The tab tells you when there's something new to read. `ytclip report` prints the same.

**The weekly report.** At 9 am on Mondays (or the first time automation runs after that), a notification sums up the past week on each TikTok account: what was posted (clips, Creator videos and anything posted by hand), its views against the week before, how many of them were on videos over a minute (the views Creator Rewards pays for) and where it stands on the program's goals, followers gained, the week's best and weakest post, any older video that took off, and whether clips or Creator videos did better. Then what automation made, and what's waiting for you. Click the notification, or **Performance → Weekly report**, to read it; a copy goes to `%APPDATA%\ClipMonster\reports`, and to your phone when the [Telegram bot](#your-phone-telegram) is set up. A week's views come from a small daily record of every video's views (three weeks kept). Until that reaches back a week, an older video's views are worked out from its stats history, and the report says "about". *Settings → Automation → Weekly report on Monday mornings* turns it off; `ytclip report --week` prints it.

Below it, the **health check** looks at everything automation needs: ffmpeg, how old the downloader is, your YouTube and AI keys (**Check now** tries them online), the folders and free space, TikTok posting (an account for every category, logins, the last upload-page check, a browser to post with), categories with nothing to find videos with, and requests that can't be filled. Each problem comes with the fix. Automation runs the offline part every time it starts and puts what it finds in the log. `ytclip doctor` runs it from the command line.

### Categories

Run a TikTok account for each topic? Give each account a category in the **🎯 Categories** tab. A category has:

- **The account:** a name, the TikTok account it **posts to** (picked from your accounts, or **Add an account…**), how many videos a day it gets (2 to start with), and a switch to pause it. A category with no account yet keeps its clips waiting.
- **Where its videos come from:** its own channel list and Creative Commons searches. Title words and YouTube categories catch videos from the trending charts. **Standing requests** add the videos you describe (see below).
- **How its clips look:** its own style, top/bottom view and folder of second videos (blank ones use Settings → Automation), and a note telling the AI what makes a good clip for that account. AI picks and AI polish both read the note.

Every picked video goes into one category: the one that lists its channel, then the one whose search found it, then the first with a matching title word, then the first with its category. A video that fits no category isn't picked, and its Pick tooltip says why. Research's **Category** column shows where each video would go.

- **Fair shares.** Each category picks at most its own number of videos a day. **Videos picked per day** (Settings → Picking) is the limit for all of them together, handed out one pick at a time, and the category with the fewest picks in the last day goes first. One busy category can't take every pick.
- **A paused category** holds its own channels' and searches' videos back. They aren't handed to another account.
- **Run from the Automation tab.** **Automation → Categories** shows each category's picks today against its limit, its videos waiting and its clips today. Pause or resume it there, press **Find videos now** to check its sources straight away, or **Request clips for it**.
- **Its own clips.** A category can set its own **clips per video** and **clip length**, over Automation's (clip length is otherwise Settings → Clip finding's). Its **special requests** note tells the AI clip finder and the AI polish what makes a good clip for that account; with a Claude or Gemini key, a category with a note uses the AI clip finder (within *AI calls per day*).
- **Style.** A recipe for the video's channel or category comes first, then the category's own style and view, then a catch-all (`*`) recipe, then Settings → Automation.
- **Creator videos a day.** A category can have [Video Creator](#video-creator) videos made for it every day too (Categories tab → Video Creator: up to 6 a day). Automation plans them once a day, taking ideas in turn from the category's own **Topics** (one idea per line, each used at most once a month) and from what's trending in its part of YouTube right now (phrases several of the newest videos share, like "GTA 6", looked up online so the script is about the topic and today's news on it, never a retelling of someone's video), then its title words. They're made overnight from 1 am, within Automation's working hours, so they're ready to post in the morning, or as soon as they're planned (*Settings → Creator → Make the daily videos*). Nothing is planned for a category that's off, paused or without an account, and a video that fails isn't replaced that day.
- **Folders.** Each category's clips go to `Automation\<category>\`, and `clips.json` records the category and its TikTok account, so each clip is queued for that account's posts. Renaming a category sends new clips to its new folder. Clips already made stay where they are.

**Standing requests.**
- **What they are.** A category's standing requests are what you'd type into Research's **Find videos**, one per line, such as *Call of Duty MW4 with good comedy*. Automation runs each one every few hours (6 by default; set it under Settings → Picking → Run standing requests every). Each run looks at videos posted in the last month.
- **What gets picked.** It picks the videos that match at least 60 out of 100, under the same rules and limits as everything else: the category's videos a day, the channel limit, blocked words and the minimum clip-ability.
- **Permission.** A video found for your request counts as permission to clip, like a link you add yourself. Picks say "Found for your request (90 match)". *Creative Commons only* still means only Creative Commons.
- **The log.** It shows what each request found, and why good matches weren't picked.
- **Cost.**
  - **YouTube quota.** Each run of a request costs about 200 quota units, so four requests every 6 hours use about 3,200 of the 10,000 a day.
  - **The AI.** With a Claude or Gemini key, the AI writes the searches and rates the matches. That's two calls a run, counted in *AI calls per day*. When the day's calls run out, the requests carry on matching by the words.
- **Without categories.** The standing requests in Settings → Picking run instead.

Making your first category moves your channel list, Creative Commons searches and standing requests into it. If you never make a category, nothing changes. `ytclip categories` lists your categories with what each picked today, what's waiting and its clips in the last 7 days (`--json` for scripts).

### Sports categories

Give a category a **Sport** (Categories tab → *The category* → Sport: soccer, basketball, American football, baseball, ice hockey, tennis, boxing/MMA, golf, cricket, motorsport, or *Any sport*) and its videos are clipped as sports, from a full match or a highlights video:

- **What finds the plays.** The **crowd erupting**: louder than the last two minutes of the match, noise-like (a roar, not one voice) and lasting a few seconds, so a whistle or one fan shouting doesn't count. The **commentator's big calls** for that sport (*"what a goal"*, *"touchdown"*, *"down goes…"*, a stretched *"gooooal"*), credited to the seconds just before them, where the play is. The commentator **getting animated**. **Replays** (*"let's see that again"*, *"another angle"*), which mark the half minute before them, and the **burst of camera cuts** after a big moment (close-ups, the bench). YouTube's *Most replayed*, [Cheering]/[Applause] captions and your title words count too.
- **Each clip is one whole play.** A few seconds of build-up before the crowd starts to rise (12 s for soccer, 8 s for basketball…), the moment, and the reaction until the roar dies down, ending before the replay starts. Clip edges never get pulled in to the commentary (*Trim dead air* is skipped), and only move outwards to finish the commentator's sentence.
- **Lengths.** Each sport has its own clip length (soccer 15–40 s, basketball 10–30 s…) unless the category sets **Clip length**. With Creator Rewards on, clips are still made over a minute.
- **The AI.** In AI mode the AI is told to pick whole plays of that sport (goals, saves, buzzer-beaters, knockouts…), and the AI polish writes hooks that build suspense without giving the ending away.
- **Sent to your Macs.** Choosing a sport also ticks **Send new clips to my Macs** (and YouTube's *Sports* category, if the category had none): every new clip lands in the **Sports clips** inbox of ClipMonster for Mac on each linked Mac, with a notification (see [ClipMonster for Mac](#clipmonster-for-mac)). Any category can have it ticked.
- **Copyright.** League and broadcaster footage is usually claimed on TikTok and can be muted or taken down. Channels that allow reuse, or your own footage, are safest.

### The Sports tab: pick the games, get each one's best plays

The **Sports** tab finds games on YouTube, lets you pick the ones you want, and ranks each game's plays so you only clip the best. It's the same page on the PC and in [ClipMonster for Mac](#clipmonster-for-mac) (under *Your PC → Sports*): the PC does the work either way.

1. **Which games.** Set the **Sport**, the **League** (a list for each sport, or type your own), the **Teams** or players (separated by commas; empty for every team), **Played in the last** 24 hours, 3 days, a week, a month or any time, the **Footage** (full games, extended highlights, highlights), any **Channels** to keep to, the category the clips are for (**Clips for**), and how many plays to tick for each game (**Best N per game**). Press **Find games**.
2. **The games.** Each upload is read into a game (home, away, score, which footage it is), and uploads of the same game are put together, so a game shows once with its versions (switch between them on the right). Reactions, previews, video games and fan edits are left out, and the search note says how many and why. With a YouTube Data API key (Settings → Research) the search is by upload date and costs about 200 units; without one it uses YouTube's own results for free.
3. **Pick the games** (tick them) and press **Find highlights**. Each game is downloaded and listened to like a [sports category](#sports-categories) (the crowd, the commentator's calls, replays), up to 14 candidate plays are cut as whole plays, and then **the AI ranks them**: it reads each play's commentary and its crowd and calls readings, rates it from 0 to 100, names it (*Poster dunk*, *Game winner*), writes a title, names the players it heard, and spots replays of the same play so the same moment isn't clipped twice. The score shown is mostly the AI's rating, with the signals' share. The best N are ticked.
4. **Make clips** makes the ticked plays for the category picked (its look, its folder and its account's queue; a sports category sends them to your Macs too), or **Open in Studio** puts them on the Studio's timeline to fix first.

**The AI.** Ranking uses the AI chosen in Settings → AI: **Claude** or **Gemini** with your API key, or the **local AI** on your PC (it gets fewer candidates and shorter commentary, so a small model keeps up). Titles are read by the same AI when one is set up. With no AI, or if it fails, the plays are ranked by the crowd and the commentary alone, and the page says so (*No AI: crowd & commentary rank plays*).

**Who does the work.** While automation runs, it does the Sports work between videos (and says *Waiting: working on the games you picked in Sports*), so a Mac's asks are done with the window closed. While it doesn't run, the window does it. Picked games survive a restart; work stopped by closing is picked up again next time.

### Recap reels: a game's best plays as one video

**Make reel** (beside *Make clips* on a game with highlights, on the PC or the Mac) turns the game into one vertical recap of 65 to 85 seconds, long enough for Creator Rewards. It uses the ticked plays, topped up from the best of the rest to between 4 and 7. Choose its voice, framing and order under **Recap reel** in the filter. It goes to the category's folder under `Reels` (or `Automation\Sports\Reels`), its account's posting queue, and your Macs when the category sends clips there.

- **The cut.** A 3-second opening shows the best play up to just before its moment, then cuts away. Then come the plays: as a countdown (*PLAY 4, PLAY 3… NUMBER 1*, or *THE GAME WINNER* when that's what it was) or in the order they happened. Each play keeps about two-thirds of its seconds for the build-up and the rest for the reaction, stops a second before a replay starts, and snaps to the broadcast's own shot changes. A quick slide, wipe or flash, with a whoosh, joins the plays.
- **Transitions** are 0.3 s and yours to choose (Settings → Sports → *Reel transitions*): a mix of wipes, slides and zooms, cross-dissolves, whip-pans, or a zoom into the next play.
- **The narrator.** The AI (Claude, Gemini or the local AI) writes the story of the game in two sentences, and the voice you pick (the Video Creator's voices: Kokoro, else Piper, Windows or eSpeak) reads it over the opening while the game's sound is ducked. The AI also writes the opening banner, the caption, the hashtags and a lower-third for each play (*LEBRON JAMES · POSTER DUNK*). A badge naming a player who isn't in the play's commentary, the title or the teams isn't used. Without an AI, the words come from the plays' own titles. A narrated reel is labelled AI-generated when it's posted.
- **Following the action.** *Follow the action* fills the 9:16 picture with a window that moves with the play. It works out where things move between frames (taking out the camera's own pan), then eases the window there, holding still when little moves and moving faster when the play runs away. *Whole picture* shows the whole broadcast over a blurred fill.
- **The score bug.** With Claude or Gemini, two frames of each play are sent to the AI to read the broadcast's scoreboard. The reel's own bug then shows the score at the start of each play and changes to the new score after the moment. Reads that show other teams, or a score going down, are dropped. The local AI can't see pictures, so with it the bug shows the two teams only. A **FINAL** card closes the reel when the final score is known.
- **The sound.** The game's sound is split into its middle, where the commentary sits, and its sides, where most of the crowd's width is. The sides are lifted around every play's moment, so the crowd erupts. The music (the Creator's *Epic* bed) ducks under the commentary and the narrator. The mix is set to -14 LUFS with peaks under -1 dBTP, measured on the finished AAC.

`ytclip sports-montage "GAME_URL_OR_FILE" --sport soccer --creator-rewards --output-dir "Videos/Sports"` makes a reel from the command line. It finds and ranks the plays as the tab does. Other options: `--category`, `--voice` (or `off`), `--framing follow|fit`, `--order countdown|timeline|climax` (climax: as it happened, with the best play held for the end), `--transitions mix|dissolve|whip|zoom`, `--events "QUESTION"` (a reel of the moments an [event search](#search-by-in-game-event) finds), `--feed` and `--anchor` (lower-thirds with the feed's own minute and scorer, such as *90+4' WINNER · E. HAALAND*), `--plays N`, `--home`/`--away`, `--no-ai`. With `--creator-rewards`, a game that comes to under a minute exits with code 3, and the reel is kept.

### Search by in-game event

The bar at the top of the **Sports** tab finds a moment by its description. Type it and press **Search**: the game you're looking at is searched (or, with none open, every picked game). The same bar is at the top of **Research**, where it sends the selected video to Sports and searches it there, and in ClipMonster for Mac under *Your PC → Sports*.

> *Red card or player ejection* · *Missed penalty or controversial offside call* · *Buzzer beater or game-tying shot in the final minute* · *Manager yellow card or sideline argument* · *Ref throws red card after VAR check in Arsenal vs Chelsea* · *Controversial pass interference in Chiefs 4th quarter* · *Technical foul on bench during game 7*

- **The question** is split into its alternatives (*X or Y*) and read for what it names (a red card, a VAR review, a technical foul, pass interference, a buzzer-beater… 24 ideas, each with the phrases commentators use for it), a period (*4th quarter*, *second half*, *overtime*), the end of the game (*final minute*), an order (*after a VAR check*) and the teams. When an AI is set up it reads what the rules don't (the local one first), and it only adds to what the rules found.
- **What the game said.** The commentary (YouTube's captions, else Whisper's transcript, timed to the word) is cut into 15-second windows that overlap by 10. Each window is compared with the question by meaning, using the sentence model **all-MiniLM-L6-v2** on this computer (a 90 MB download the first time; DirectML, CUDA or Core ML when that ONNX Runtime build is installed, else the CPU), by whether the idea's own words are said, and by whether the crowd reacted. *"That should have been a red"* counts as talk about a card, not a card: it scores lower and is marked.
- **What the game recorded.** The game's play-by-play is looked up (Settings → Sports → **Play-by-play feed**): **ESPN**'s public scoreboard (no key), **API-Football** for soccer when you add its key, or a **timeline file** you wrote (JSON or CSV rows with `kind` or `text`, and `video`, `minute`, or `period` + `clock`). Its events are placed in the video from **anchors**, moments where the video's second and the game's clock are both known: the commentary ("the second half is under way"), the scoreboard's clock read by Claude or Gemini, or yours (`2=58:40`). Soccer's clock runs with the world, so that's good to seconds; sports that stop the clock get a wider margin, narrowed by the commentary naming the event or the crowd's loudest second. In a highlights cut there's no clock, so events are placed only where the commentary names them, in the game's order. An event that can't be placed is left out, not guessed.
- **The cut.** The moment is where the idea is first named (or the feed's second). The clip runs from 12 s before to 25 s after (Settings → Sports), wider for ideas that take longer (a review, a fight), reaching back to the foul that drew a card, staying while the commentary is still on it, with each end moved onto a pause between sentences.
- **What you get.** The moments join the game's plays at the top, ticked and marked 🔎, each with why it matched and what was said. **Make clips** makes them; **Make reel of found** makes one reel of them.

SofaScore and FotMob aren't used: neither offers a public API, and both block automated reading. ESPN's feed is public but undocumented, so it can change; when it can't be read the search goes by the commentary and says so.

`ytclip sports-find "red card or player ejection" "GAME_URL_OR_FILE" --sport soccer` does the same from the command line and prints each moment with where it is and why. Options: `--feed auto|off|espn:LEAGUE:EVENT_ID|api-football:FIXTURE_ID|FILE`, `--anchor PERIOD=TIME` (repeatable), `--limit N`, `--words` (no sentence model), `--no-ai`, `--json`, `--reel` (with `--output-dir`, `--voice`).

### The Clip-Ability index of a game

After **Find highlights**, a game shows a 0-100 index under its title (hover it for the parts):

| Part | Weight | What it measures |
|---|---|---|
| Momentum | 30% | Views per hour since it was posted |
| Most replayed | 25% | How peaked YouTube's *Most replayed* graph is: its variance and its kurtosis (a few sharp spikes score high, a flat graph zero) |
| Emotion | 20% | How far the sound swings between its usual level and its peaks, how often the crowd roars, and how much the sound's brightness moves (which stands in for the pitch of an excited voice) |
| Controversy | 15% | Trigger terms in the commentary: *robbed*, *never a penalty*, *terrible ref*, *fight*, *unbelievable*… |
| Audio | 10% | The commentary's level over the noise under it, and how much of the game has a clear voice |

A part that can't be measured (no graph yet, no commentary) is left out and the rest re-weighted, and the index says which.

### Learning from results

Posting keeps track of how your posted clips do: every half hour it writes `%APPDATA%\ClipMonster\results.json`, with the views of every ClipMonster clip that has been up for two days, compared with the usual views on that TikTok account (so "2×" means twice your usual). With **Settings → Picking → Learn from how my clips do** on (the default), ClipMonster uses it in three ways:

- **Picking.** Channels whose clips beat your usual views get up to 12 extra clip-ability points when ranking and checking the minimum score, and channels whose clips flop lose up to 12. One lucky clip counts for little; the effect grows as a channel gets more posts. Research shows why in the Pick tooltip and the video's details, e.g. "Your clips from Some Pod get 2.4× your usual views (5 posts): +8."
- **AI polish.** Once six or more clips with hooks have results, the polish sees the hooks that worked and the ones that flopped, and writes new hooks more like the hits. Once a category has six of its own, its videos learn from just those.
- **Video Creator.** Once four of its videos have results, the script writer sees the titles and hooks of the ones that did best and worst ("worked: "Why you can't stop scrolling", hook "Your brain is being hacked" (3.2×)") and is told to learn from how they open, never to copy them. A category with four of its own learns from just those; until then it gets your clips' hooks. The Creator's videos never count toward a YouTube channel's ranking.
- **Tips.** The Automation tab shows **What's working**: the best and worst channels, which clip length does best, whether the AI rating predicts views (and so whether a higher quality-check minimum would help), and how Creator videos do against clips. Settings are never changed for you.

`ytclip results` prints the same, with every channel's numbers (`--json` for scripts). Results older than 90 days are ignored.

## Video Creator

The **Creator** tab (and `ytclip create`) makes an original, faceless video from an idea, all on your PC: the local AI writes the script, a neural voice reads it, a picture is made for every scene and slowly moved, music and sound effects are mixed in, and big word-by-word captions are burnt on. The video goes to its category's Automation folder and its TikTok account's posting queue like any clip, labelled **AI-generated**.

1. **Write it for me** with a prompt ("3 stoic habits that will change your life"), or **Use my script** to paste your own narration (the first sentence is the hook; a blank line starts a new scene).
2. Pick the **category** (its folder and account), the **voice** (▶ plays a sample), the **length** and the **look**. *Over a minute* keeps it 62–85 seconds, so it counts for Creator Rewards.
3. **Write storyboard** shows the script scene by scene: what's said, the picture, the camera move and the sound. Double-click anything to change it.
4. **Make video** renders it (a few minutes for a 65-second video), or **Add to automation queue** lets automation make it in turn with the clips. A category can also get Creator videos [every day by itself](#categories).

How each part works:

- **Script.** The AI writes a storyboard in a fixed JSON shape: a hook of at most 10 words (under 3.5 seconds), then 130–155 words a minute of narration, scene by scene. It's asked again when it misses the word count or the hook runs long. It never promises a part 2, never states made-up statistics as fact and never puts real people or text in the pictures. *Settings → Creator → Script writer* chooses the local AI (free) or the AI in *Settings → AI*. Each new script is compared with the last 400 you made, and one that repeats an earlier one's wording is written again.
- **Today's facts.** Before writing, the topic is looked up on Google News, Bing News and Wikipedia (free, no account). The newest stories, the text of the first few articles and today's date go to the AI, which is told to trust them over its memory. A local AI's training can be a year or more old, so without this it can repeat stale news (a release date that has since moved, say). The sources are listed in the video's `README.md`, `clips.json` and `storyboard.json`. *Settings → Creator → Look it up online first* turns it off; when you're offline, the script is still written, with today's date and a rule against guessing at recent events.
- **Fact check.** Once the script is written, the AI reads it again sentence by sentence against those sources. Each date, number, name, quote or event is *confirmed*, *contradicted* (a source says otherwise: the sentence is rewritten to what the source says) or *unconfirmed* (no source says it: a sentence with a date, number or quote nobody confirmed is softened, anything else is only marked). The storyboard's scene numbers get ✓ (fixed) or ⚠ (check it yourself), with what the source said on hover, and the video's README lists every change and mark with its source. Rewrites that are much longer than the sentence, or a hook over 10 words, aren't used. *Settings → Creator → Fact check*: fix (the default), mark only, or off. It doesn't run when nothing was found online: an AI checking itself from memory is how stale facts get in.
- **Voice.** [Kokoro-82M](https://github.com/thewh1teagle/kokoro-onnx) (natural, downloads about 340 MB once), else Piper, else Windows' own voices, else eSpeak NG. Eight narrators: Deep Male Narrator, Energetic Storyteller, Documentary, Warm Female, Bright Female, Hushed, British Male and Calm Male. When a voice runs long or short for the target, it's read again a little faster or slower, and the pace it actually reads at is learnt for the next script. Whisper then lines every caption word up with the voice.
- **Pictures.** ComfyUI if it's running on this PC (any checkpoint; SDXL-Turbo and FLUX.1-schnell get their own settings, or use your own workflow JSON with `{{prompt}}`, `{{negative}}`, `{{seed}}`, `{{width}}`, `{{height}}`), else **AI pictures** (SDXL-Turbo, or FLUX.1-schnell on a 16 GB+ NVIDIA card), else clips and pictures from your B-roll folder matched by name, else built-in moody textures in the look's colours.
- **AI pictures, one click.** The installed app has no PyTorch (it's several GB and depends on the graphics card), so the Creator tab's **Set up AI pictures** link (or `ytclip create --setup-pictures`) builds a separate picture engine in `%LOCALAPPDATA%\ClipMonster\picture-engine`: [uv](https://github.com/astral-sh/uv) fetches a private Python 3.11 and PyTorch with **DirectML** for AMD and Intel cards (CUDA for NVIDIA, the processor build otherwise), then diffusers, then the SDXL-Turbo model (about 7 GB, into the models folder) and draws a test picture. About 10 GB and 10–30 minutes in all, once. After that every scene's picture is drawn on your card, offline (576×1024, 4 steps). If half precision draws a black picture on your card, it switches to full precision by itself. SDXL-Turbo is Stability AI's model under its [Community License](https://stability.ai/license): free, also for videos that earn money, while you make under $1M a year. A source install with PyTorch and diffusers in its own Python (`pip install -e .[diffusion]`) uses that instead. Each scene falls back on its own, so one failed picture never stops the video. The local AI's model is unloaded first to give the picture model the graphics memory.
- **Rendering.** Several scenes are animated at once (none in parallel with *Settings → Speed → Quiet*, a few with Balanced or Full power) and encoded on the graphics card when ffmpeg can use it (AMF on a Radeon, NVENC, Quick Sync), near-lossless, before the one final encode. If the card's encoder fails on a scene, that scene is made again on the processor and the rest follow.
- **Motion.** Every picture gets a camera move: push in (zoom 1 → 1.15), pull out, pans in six directions or a push to one side, with a hint of roll, drawn at sub-pixel precision so nothing shimmers. Scenes change just before their first word, with 0.3-second dissolves, dips to black, slides or zooms.
- **Sound.** The voice is compressed and set to −14 LUFS; the music sits under it and ducks while it speaks; whooshes, risers, bass drops and hits land on the cuts and the cues the script asks for; a limiter keeps peaks under −1 dBTP. Music comes from your music folder (sorted by mood folders like `dark` or `epic`, if you have them), else a calm bed is composed for the mood. Sound effects are built in; put your own WAVs in a folder (*Settings → Creator → Sound effects folder*), named or foldered by cue (`whoosh`, `riser`, `impact`...) and they're used instead. Only use music and sounds you have the rights to.
- **Checked by you, at first.** *Settings → Creator → Check videos before they post*: an account's Creator videos wait in its queue (⏸ **Needs your OK** in the Queue tab) until you watch one and press **OK to post**, for its first two weeks of Creator videos (the default), always, or never. *Post now* counts as your OK. Clips never wait.
- **Checks.** 1080×1920, sound present, the loudness, the length (over 61 seconds for Creator Rewards), the size for the TikTok app, and the originality check. The folder gets `clips.json` (what posting reads, with the caption and hashtags), `storyboard.json` (to make it again: `ytclip create --storyboard`), a `README.md` with how it was made, the scene pictures and a thumbnail.

```
pip install -e .[creator]          # Kokoro voice (the release build includes it)
pip install -e .[piper]            # Piper voices
pip install -e .[diffusion]        # SDXL-Turbo / FLUX.1-schnell in this Python (install PyTorch for your card first)
ytclip create --setup-pictures     # or the picture engine: its own Python and PyTorch for your card
```

## Studio

The **Studio** makes a finished video from a prompt *or a public article's address*, upright for TikTok, Shorts and Reels or wide for YouTube, from a few seconds to 20 minutes, and lays the whole edit out as a **timeline you can change and render again**. It's the Video Creator's pipeline with a new middle, so the rest is what you know: categories and their accounts, the posting queue, the AI-generated label, the fact check, the originality check, learning from results, review before posting.

In the **Creator** tab, set *Made with* to **Studio** (or paste an article's address, which switches to it). On the command line it's `ytclip studio`:

```
ytclip studio "Why the Roman Empire really fell" --category History
ytclip studio --url https://example.org/a-public-article --seconds 60 --captions karaoke
ytclip studio "How lighthouses worked before electricity" --format long --seconds 480 --no-post
ytclip studio "3 money habits that compound" --visuals stock --tts kokoro --voice deep_male --ai local_first
ytclip studio "The angle to take" --url https://example.org/story --storyboard-only --out storyboard.json
ytclip studio --storyboard storyboard.json            # make an (edited) storyboard as it is, however long
ytclip studio --timeline "Videos\ClipMonster\Output\Created\2026-11-10 Lighthouses [c-1a2b3c4d]"
ytclip studio --check                                 # what's ready: script writer, voices, pictures, stock keys
ytclip studio --schema                                # the timeline's JSON Schema
```

`--format short` (the default) is vertical 9:16 at the Creator's length; `--format long` is wide 16:9, five minutes unless `--seconds` says otherwise; `--orientation vertical|landscape|square` picks the shape itself. An upright video of about a minute is kept at 62–85 s for Creator Rewards as before; ask for under a minute, a long one or a wide one and it's the length you asked for (a long or wide one even with the Rewards box ticked).

The run is eight steps, shown as it goes; if it stops, it says in which:

1. **Input.** The request is checked before anything is made.
2. **Script.** With `--url`, the page is read once (see the rules below) and retold in **new words**: the script is compared with the article, asked for again when it lifts a run of the article's wording, and refused when the AI keeps copying. Without one, the topic is looked up online as the Creator does. Either way the script is written to **130–155 words a minute** for the length, as scenes with a role each (hook, setup, build, twist, payoff, call to action), a picture description, a stock-footage search, a camera move and a sound cue. A long video is planned as chapters first, then written a chapter at a time so nothing is told twice. The fact check then reads the script against the article (or the sources found).
3. **Voice.** The Creator's local voices (Kokoro-82M, Piper, Windows' own, eSpeak NG), with Whisper lining every word up. **ElevenLabs** is used only when you pick it (`--tts elevenlabs` or *Settings → Creator → Voice engine*) with your own key: the script's text is sent to ElevenLabs, it costs money a character, and it's never an automatic fallback. Use voices you have the right to use; this app doesn't clone voices.
4. **Pictures.** Each scene gets, in order: a clip or picture from **your B-roll folder** matched by its file and folder names; **stock footage** from Pexels and Pixabay; a **picture drawn on this PC** (ComfyUI, SDXL-Turbo, FLUX.1-schnell) in the video's shape; a built-in texture. A scene written as an illustration is drawn first when a picture model is ready. `--visuals stock` puts stock footage first for every scene.
5. **Timeline.** Everything is laid out on tracks: pictures (each with its camera move as keyframes, a transition centred on every cut), the voice, the music, the sound effects, the captions and the hook's title, with markers on the hook, the twist, the payoff and each chapter.
6. **Render.** One ffmpeg run draws, mixes and encodes the whole timeline: no intermediate video files, so nothing is encoded twice. The voice is high-passed, gently compressed and set to −14 LUFS; the music is **ducked about 16 dB under speech** by a sidechain compressor and comes back up in the pauses; the mix is mastered to −14 LUFS with peaks under −1 dBTP.
7. **Check.** Size, length, sound, loudness, and wording too close to a video you made before.
8. **Deliver.** The video's folder, queued for its category's account like any Creator video.

**Captions** are kinetic: one to three words at a time inside the safe zone (clear of TikTok's buttons and caption), popping in (80% → 110% → 100% in 150 ms) with the spoken word lit. Money words turn green, warnings red, questions yellow, the script's emphasis words the style's second colour, and a small drawn icon pops in beside a word that has one. `--captions pop|bounce|karaoke|plain|off`.

**The timeline** is `timeline.json` in the video's folder, beside `scenes/` (each scene's picture or clip) and `assets/` (the voice with every word's time, the music, the effects). It's plain JSON: clips with their start, length, keyframes (position, scale, rotation, opacity, with easing) and transitions; audio clips with levels and fades; every caption word with its time, colour and where it's drawn; the master's loudness target and ducking depth. Change it, then `ytclip studio --timeline <folder>` (or **Render timeline again** in the Creator tab) renders it as `<title> (edit).mp4`. A key it doesn't know (a misspelling: `transiton_in`) is passed over and said so, with the nearest real name; a value it can't use is refused with where it is. For captions, a render draws each word's own `text`, `color` and times in the style's preset, font, size, colours, outline and position: the style's `uppercase`, `max_words` and keyword colours were used when the captions were built, so change the words themselves. The Creator tab draws the timeline under the storyboard.

The edit is rendered out of sight and put in place when it's whole, then **listed with its video**: in a folder posting watches it joins the queue with the same AI-generated label, caption and category, and **always waits for your OK** there (the original may be queued too: remove the one you don't want). Rendering again replaces the edit: if it's still in the queue, the new version is what posts after your OK. If you took it out of the queue it stays out, and one that has already posted isn't posted again under the same name, so to post the new version give it a new name: change `"title"` in `timeline.json` and render again, or use `--out "<the video's folder>/<a new name>.mp4"` (an edit beside its video is listed, labelled and held whatever it's called). Once an edit has posted and Clean up has cleared its file, the next render is taken in as a new edit by itself. An edit saved somewhere else with `--out` isn't listed: posting leaves it alone when it's one folder inside an AI-generated video's folder, and anywhere else a folder posting watches takes it for an ordinary video, with no label and no hold, as the render says. The scenes' stock clips and a video's other makings are never taken in as videos to post. While an edit is still to post, Clean up leaves the timeline and its files alone. *Settings → Creator → Keep each Studio video's timeline* turns the extra files off; Clean up removes them with the video once it's posted.

**Reading an article: the rules it keeps.** Only `http(s)` pages anyone can open. A page behind a sign-in or a paywall isn't read, and nothing is done to get around one. The site's `robots.txt` is asked first, as "ClipMonster", and a "no" is respected: at the address you gave and wherever it redirects. A page that marks itself as paid-for isn't read even when its text is in the page, and a part of a page marked as the subscribers' (a "paywall" or "subscriber-only" block) is cut out before any reader sees it. Addresses on your own network are refused, and the connection is made to the address that was checked. The page is fetched once, under ClipMonster's own name. The article's address, title, site, author and date are kept as the video's first source in `README.md`, `clips.json`, `storyboard.json` and `timeline.json`: **credit the source when you post**. Facts aren't anyone's property but a writer's words are, which is why the script must retell rather than copy; you're still responsible for what you publish.

**Stock footage: the rules it keeps.** Pexels and Pixabay are searched through their official APIs with **your own free keys** (*Settings → Creator*, or the `PEXELS_API_KEY` / `PIXABAY_API_KEY` environment variables): [pexels.com/api](https://www.pexels.com/api/), [pixabay.com/api/docs](https://pixabay.com/api/docs/). Only a scene's few search words are sent. Answers are kept for a day so the same search isn't sent twice; files are downloaded once, only from the sites' own addresses, with a size limit; a site that says "slow down", refuses the key, or turns down three searches in a row is left alone for the rest of the video. Each file's author, page and licence go into the README and the timeline. Both licences allow use in your own videos, including ones that earn money, but not selling the footage on its own or suggesting the people in it endorse something; Pexels asks for a link back where you can give one.

Long videos need memory to render: about 40 MB a scene at 1080p, so a ten-minute video with 150 scenes wants around 6 GB free. The Studio says so before it starts (and before an edit is rendered again) when there isn't that much. Windows also limits how long a command may be: stills never count towards it, a sound used many times counts once, and a timeline with more video clips than fit is refused in plain words rather than failing halfway.

```
pip install -e ".[creator,whisper]"     # the Kokoro voice and Whisper's word times (the release build has both)
pip install -e ".[studio]"              # better article reading: trafilatura and readability-lxml (optional)
pip install -e ".[piper]"               # Piper voices (optional)
pip install -e ".[diffusion]"           # SDXL-Turbo / FLUX.1-schnell in this Python (PyTorch for your card first)
ytclip create --setup-pictures          # or the picture engine: its own Python and PyTorch for your card
ytclip local-ai download                # the local AI that writes scripts (or add a Claude or Gemini key)
```

ffmpeg comes with the app (and with `pip install -e .`, through imageio-ffmpeg); version 6 or newer. From source on Linux or macOS, eSpeak NG (`apt install espeak-ng`, `brew install espeak-ng`) is only needed for the eSpeak voice.

## Posting to TikTok

Posting runs inside automation's background process, so it keeps going with the window closed and starts with Windows. For every clip that passed the quality check, it:

1. **Queues it for its category's account.** A clip from a category goes to the account the category posts to. Without categories, a single account takes everything. Parts of a series stay in order, back to back.
2. **Gets it ready.** The caption is the clip's hook, plus a credit line for Creative Commons clips (the licence requires it). The AI writes hashtags for it, fitted to the account's category and leaning on the tags that got your posts the most views; the account's own hashtags (`#fyp` to start with) are added to every post.
3. **Posts it when its time comes**, in TikTok Studio, in Microsoft Edge (or Chrome) with that account's own login. Leave **Show the browser window** on: TikTok is less likely to block a browser you can see, and you can solve a captcha if one appears. The wait for the upload follows TikTok's own progress, so a big clip on a slow connection gets as long as it keeps moving (15 minutes, plus time for anything over 100 MB at 2 Mbit/s), and an upload stuck at the same percentage for 4 minutes stops with that said. A clip over 100 MB is only posted once TikTok says the upload is done.
4. **Reads the profile** every 5 minutes, in the background: views, likes, comments, shares and followers, and which TikTok video each post became. A post that shows up on the profile is marked posted, even one you posted from your phone.
5. **Writes a record and deletes the clip.** Once a post is on the profile, a `.txt` to read and a `.json` beside it go to `Documents\ClipMonster\Posted videos\<account>`: when it posted and its link, the caption and hashtags, the clip (title, hook, length, part), where it came from (the YouTube video, its channel and licence, where in it the clip starts and ends), why it was made (category, how the video was picked, the clip finder's score, the AI's rating, the quality check) and how it did. The numbers are added 1 hour, 24 hours and 7 days after posting. Then the clip, its captions and thumbnail are deleted, and its row leaves the video's `clips.json`; a video folder with nothing left goes too, which for a Video Creator video means its scene pictures and storyboard (its record keeps the prompt, the script, the voice, where the pictures came from, the sources and the fact check). A post that hasn't shown on the profile after 2 days keeps its files, and the record says why.
6. **Keeps an eye on TikTok.** Once a day it opens each account's upload page without posting, so a change on TikTok's side is caught before a post fails. A post that fails isn't tried again by itself (it might be half up): it waits in the Queue tab with the reason, a notification says so, and **Try again** posts it.

**The cover.** Each post gets a cover of ClipMonster's choosing instead of TikTok's first frame (which, for a clip that opens on a hook card, is a dark frame with half a headline): the moment the clip's hook card is fully up, else the frame with the clearest, biggest face near the middle (sharp, well exposed), else the clearest frame. It goes in through TikTok Studio's cover editor just before Post, and the Queue tab shows it. If the editor isn't there or doesn't take it, the post goes up with TikTok's own pick: a cover never stops a post. *Settings → Posting → TikTok cover*: the hook card or the best face, the best face only, or TikTok's own pick.

**The schedule.** Each account posts 2 clips a day between 11:00 and 21:00, at least 3 hours apart, and each post time moves by up to 15 minutes from day to day so it doesn't look automated. More than 3 a day is past what posting studies support, and the Accounts tab says so. A **new account warms up** for two weeks: no posts for the first 3 days, then 1 a day, then 2 from day 7.

The tabs:

- **👤 Accounts.** Add, log in (a normal Edge window opens on TikTok's login page; log in there, with the QR code from the TikTok app if you like, and it closes by itself), log out or remove each account. Each has its own categories, schedule, who can watch, hashtags, an autopilot switch, and its profile picture, name and bio (**Apply to TikTok** fills in TikTok's Edit profile box and checks it took). **Test posting** walks the upload page with a 3-second black clip, up to just before Post, then throws it away.
- **📤 Queue.** What each account will post and when. Pick a clip to change its caption or hashtags, move it up or down, **Post now**, **Try again** after a failure, or **Don't post**. Below it, what was posted, with its views and likes, its record and its TikTok link.
- **📈 Performance.** Views, likes and followers today and over 30 days, your best videos, what's working, and a live feed. Notifications pop up when a clip posts, a post fails, a login runs out or a video takes off.

ClipMonster never sees your TikTok password: each account's login stays in its own browser profile on this PC. If the app closes in the middle of an upload, that clip is marked failed rather than posted again, because it may already be on TikTok; the next profile reading marks it posted if it is. Automating TikTok's website is against the spirit of its terms, so keep the pace human.

```
ytclip posting             # each account, its login, what's waiting and the next post; the last posts
ytclip posting --posted 30
```

## Your phone (Telegram)

A Telegram bot of your own runs ClipMonster from your phone while automation runs on the PC: ask for videos, watch what's waiting to post, OK it or delete the bad ones from the PC, and hear about what needs you.

**Setting it up** (five minutes, once):

1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot`, and pick a name and a username for your bot (the username ends in `bot`). BotFather replies with a token like `123456789:AAH…`.
2. In ClipMonster, **Settings → Telegram**: paste the token, tick **Use the Telegram bot** and press **Check the token**.
3. Press **Link a phone**. A six-letter code shows for 10 minutes: send it to your bot in Telegram, or press **Open Telegram** and then **Start**. The phone shows up under *Linked phones*.
4. **Save**, and keep automation running (Automation → Start, or Start with Windows). The bot runs inside automation, so it answers whenever automation runs, with the window closed too.

Only the phones you link can use it. Anyone else who finds the bot is told it's private, and a chat that sends five wrong codes is ignored for an hour. Only private chats can be linked, not groups; **Unlink** takes a phone off, and **Send a test** says hello to each linked phone. Use a bot just for ClipMonster: Telegram lets one program at a time read a bot's messages, so a bot another project already uses won't work here (the bot says so in automation's log). The token is kept with your other keys in `%APPDATA%\ClipMonster`.

**From the chat.** The buttons under the message box do the same as the commands:

| | |
|---|---|
| 📊 **Status** · `/status` | What automation is doing right now, what's waiting to be clipped, made and posted, the next post, the videos waiting for your OK, accounts that need signing in to TikTok, and the free disk space. **📌 Keep this up to date** pins it as a live dashboard that updates itself every 2 minutes (`/dashboard`, `/dashboard off`). |
| 🎬 **Videos** · `/videos` | Everything waiting to post: the ones held for your OK first, then in posting order. Tap one for the video itself with its card (account, category, when it posts, length and score, what it's from or the Creator's prompt, the caption) and **✅ OK to post**, **🚀 Post it now** and **🗑 Delete**. |
| ✨ **Make a video** · `/make the real story of the Bermuda Triangle` | A Video Creator video about an idea, for the category you pick, in automation's queue. The video comes back to the chat when it's made. |
| ✂️ **Clip a link** · `/clip`, or just send a YouTube link | Clips that video for the category you pick, and sends you the clips when they're done. |
| 📋 **Requests** · `/request 10 30-45s Gaming funny reactions only` | A [clip request](#clip-requests) in plain words: how many, how long, the category, links if you have them, and anything special for the AI. `/requests` lists them, with **Cancel**. |
| ⏯ **Pause or resume** · `/pause`, `/resume` | Pauses clipping and making videos. Posting carries on. |
| 📅 **This week** · `/week` | The [weekly report](#the-overnight-report-and-the-health-check) for the past 7 days. |

Anything else you type gets two buttons: make a video about it, or ask for clips about it.

**Deleting from the phone** asks first, then clears the video the way [Clean up](#clean-up) clears a posted clip: the clip, its captions, thumbnail and cover, and its row in the folder's `clips.json` and `README.md`, or the whole folder when it was the last clip there (with a Creator video's scene pictures and storyboard). It goes to the Recycle Bin when *Settings → Posting → Send deleted clips to the Recycle Bin* is on, and automation never brings it back. A video that's uploading or already posted can't be deleted from the phone.

**What it tells you by itself**, as *Settings → Telegram → Send me* says: each video that waits for your OK (with the video, so you can OK or delete it there and then), new clips and videos queued to post, each post as it goes up, milestones and videos taking off, problems that need you (a TikTok sign-in running out, a post failing, YouTube asking this PC to sign in, low disk space) and the weekly report. What you ask for in the chat is always answered. When you link a phone, the videos already waiting for your OK come as one message rather than one upload each.

**Videos on the phone.** A bot can send videos up to 50 MB. A bigger one goes as a smaller copy (720p or less, made on the PC and kept for 3 days), marked as such: what posts to TikTok is always the full-quality file. Videos go one at a time in the background, so the bot keeps answering while one uploads. When a video is OK'd, posted or deleted somewhere else (the Queue tab, or another phone), its message in the chat changes to match.

## ClipMonster for Mac

**ClipMonster for Mac** is ClipMonster on a Mac. It makes clips by itself, start to finish: the **Studio** (paste YouTube links, playlists or channels, or drop videos; see the clips it found on the timeline, fix them, and export them with captions) and the **Library**, as on the PC, with Mac menus, shortcuts and notifications. Link your PC whenever you like, and the Mac can also use the PC's local AI, have the PC make whole videos on its graphics card, and run the PC: everything under **Your PC** in the sidebar. It looks like ClipMonster: the same sidebar, colours and dark or light mode.

**Installing.** Download `ClipMonster-mac.dmg` (Apple silicon: M1 and newer) or `ClipMonster-mac-intel.dmg` (Intel) from the [latest release](https://github.com/iSlipperyX/ClipMostr/releases/latest), open it and drag **ClipMonster** to Applications. It needs macOS 14 (Sonoma) or newer. The app isn't signed with an Apple developer ID, so the first time macOS won't open it: open **System Settings → Privacy & Security** and press **Open Anyway** (on macOS 14, right-click the app → **Open** works too). Everything it runs is inside the app: ffmpeg with the Mac's media engine (VideoToolbox) and caption burning, Deno (YouTube downloads need it), Whisper and the local AI. On Apple silicon, Whisper and the local AI run on the Mac's graphics (Metal).

**The first start** asks which AI picks your clips, and where they're saved (`~/Movies/ClipMonster` to start with). Both are in Settings later.

| | |
|---|---|
| **Claude** | The best at picking clips and writing their titles. Paid per use (about a cent a video), with an API key from [console.anthropic.com](https://console.anthropic.com). |
| **Gemini** | Google's AI, with a free allowance. An API key from [aistudio.google.com](https://aistudio.google.com). |
| **Local AI on this Mac** | Free, private and offline. It picks a model for the Mac's memory: Llama 3.2 3B on an 8 GB Mac, Mistral Nemo 12B on 16 to 32 GB, Qwen2.5 32B on 36 GB or more (**Settings → AI → Local model** lists each one with the Mac it needs). The model downloads once, the first time it's needed. An Intel Mac runs it on its processor, which is slow: pick a small model there. |
| **Your PC's local AI** | The PC's own model, on its graphics card. Link your PC next (below). Only the PC's local AI answers the Mac, never its Claude or Gemini key. |
| **No AI for now** | Clips are picked from the video itself: its most-replayed moments, loud parts, laughter and scene changes. |

API keys are kept in the macOS Keychain, never in the settings file. Settings on the Mac speak of the Mac: the media engine (VideoToolbox) instead of a graphics card, Whisper on the Mac's graphics, and the local models that fit a Mac. Closing the window keeps ClipMonster running (an export finishes, the PC's news still comes); **Quit** (⌘Q) ends it, and asks first while something is being made. Drop a video on the Dock icon, or **Open With → ClipMonster** in Finder, to add it.

**Linking your PC** (once, whenever you like):

1. On the PC: **Settings → Mac app**, tick **Let my Mac run ClipMonster** and **Save**, and keep automation running (Automation → Start, or Start with Windows). The server runs inside automation, the way the Telegram bot does. The first time, Windows asks whether ClipMonster may use the network: allow it on **private** networks.
2. On the PC, press **Link a Mac**. It shows the PC's **IP address**, its **port** (47320 unless you change it in Settings → Mac app) and an eight-character **authorization code** that works for 10 minutes.
3. On the Mac: **Your PC → Link your PC** in the sidebar (or the **Your PC** menu → **Link Your PC…**). Type all three and press **Link**. PCs on the same network also show up in a list under the form; picking one fills in its address and port, so only the code is left to type. Allow ClipMonster to find devices on your local network when the Mac asks. The PC lists the Mac under *Linked Macs*, and from then on the Mac connects by itself.

**What the PC adds:**

- **Make on: This Mac | Your PC**, beside the paste box. With **Your PC** picked, each video goes to the PC (a link, or the video file itself, sent over first), which downloads it, finds the clips and exports them **with the Mac's settings** (the preset, captions, length, format), on its graphics card and with its local AI. The video shows in the Studio's list as it gets on there (*Waiting its turn on your PC*, *Being made on your PC*, *Made on your PC: its clips are coming over*), and its clips land in the Mac's Library and output folder, as if made on the Mac; the PC lets go of them once they're back. **Stop** or **Remove** works there too. If the PC can't be reached, the Mac offers to make the video itself instead. Videos sent before quitting carry on when the Mac opens again.
- **Your PC's local AI** as one of the AIs (Settings → AI, or the first start): the Mac transcribes on its own Whisper, and the PC's model picks the clips and writes their titles, so a big model works from a small Mac.
- **Your PC's pages**, below:

**Your PC's pages** in the sidebar:

| | |
|---|---|
| **Overview** | What automation is doing right now, with the same step bar as the Automation tab. Also: how many videos wait for your OK, the queue, posts today, what was made in the last day and the free disk space. It lists what's up next to clip or make, each TikTok account (signed in, queued, next post) and the news (posts going up, milestones, problems). |
| **Waiting to post** | Every video waiting to post, held ones first, with its picture. Pick one to **watch it**: it streams from the PC, so it starts at once and you can skip around. Then **✓ OK to post**, **🚀 Post now**, **↻ Try again** (a post that failed) or **🗑 Delete…**, which asks first and clears it from the PC the way [Clean up](#clean-up) does. You can change its caption and hashtags until it posts. |
| **Posted** | The latest posts with their views, likes, comments and shares. Double-click one to open it on TikTok. |
| **Sports** | The PC's [Sports tab](#the-sports-tab-pick-the-games-get-each-ones-best-plays): the same filter, games and ranked plays. Find games, pick them, find their highlights and make clips: the PC searches, downloads, listens and ranks with its own AI, so nothing heavy runs on the Mac. **Open in Studio** brings a game's ticked plays into this Mac's own Studio to edit and export here. A notification says when a game's highlights are ready. |
| **Sports clips** | Every new clip from a category with **Send new clips to my Macs** (every [sports category](#sports-categories)), newest first, with a notification as each arrives (and a count of what came while the Mac was off). Watch it, **Save to Mac**, **Save as…**, **Show in Finder**, **Open** it in QuickTime, copy the link to the moment in the source video, or **Remove from inbox**. With **Save new clips to this Mac by themselves** on (it is to start with), each one is downloaded as it arrives into `~/Movies/ClipMonster/<category>/` (pick another folder at the top). The PC keeps each clip for a week, or until every linked Mac has it, so it still reaches a Mac that was asleep, even after the clip posts and [Clean up](#clean-up) deletes it. |
| **New video** | Make a Creator video about an idea, clip YouTube links, or ask for clips (how many, how long, the category, links, special requests for the AI), each for the category you pick. |
| **Requests** | Your clip requests, how far along each one is, and **Cancel**. |
| **Categories** | Each category's state and what's waiting for it. **Pause** or **Resume** one, or **Find videos now**. |
| **This week**, **Log** | The [weekly report](#the-overnight-report-and-the-health-check), and automation's log as it's written. |

**Pause** in the bar over the PC's pages (or **Your PC → Pause Automation**, ⇧⌘P) pauses clipping and making videos on the PC (posting carries on). The Dock icon and the menu bar icon show how many videos wait for your OK. Mac notifications tell you when a video waits for your OK, when a sports clip arrives, when a post goes up and when something needs you. **Your PC → Switch to Another PC…** moves between linked PCs.

**Away from home.** The PC isn't opened to the internet. Install [Tailscale](https://tailscale.com/download) on the PC and the Mac, signed in to the same account, and type the PC's Tailscale address (like `100.101.7.42`, or its name `justyn-pc.tail1234.ts.net`) as the IP address in the Mac app. The Mac recognises it as the same PC and connects as usual.

**Security.** Every connection is encrypted with the PC's own certificate, which the Mac checks by fingerprint. Linking proves the Mac and the PC see the same certificate, so another computer on the network can't pose as either one. The code is checked through 200,000 rounds of PBKDF2 and stops working after five wrong tries. After linking, the Mac keeps its key in the macOS Keychain, and the PC keeps only a hash of it in `%APPDATA%\ClipMonster\remote.json`. **Unlink** on the PC, or **Your PC → Unlink This Mac…** on the Mac, removes a Mac. Settings → Mac app shows *This PC's ID*, and the Mac shows the same ID under the address as you type it, so you can check they match. If you reinstall ClipMonster on the PC from scratch, it gets a new ID and each Mac has to be linked again.

## Linux host (Google Cloud)

ClipMonster can also run on a Linux server, such as a Google Cloud VM, with nobody at it. It does everything the PC does: finds and clips videos, makes Creator videos and posts them. You run it from your Mac with [ClipMonster for Mac](#clipmonster-for-mac), and from your phone with the [Telegram bot](#your-phone-telegram).

**Making the VM.** In the Google Cloud console, **Compute Engine → VM instances → Create instance**:

- **Machine:** e2-standard-8 (8 vCPUs, 32 GB) or bigger. Clipping and rendering run on the CPU. A VM has no graphics card unless you add one (a G2 machine with an NVIDIA L4), so the local AI is slow on it: pick **Gemini** or **Claude** in Settings → AI instead.
- **Boot disk:** Ubuntu 24.04 LTS (x86/64), 100 GB or more. Downloads and clips take room.
- Leave the firewall boxes unticked. The Mac app has its own rule, below.

**Installing.** Open **SSH** on the VM and run:

```
curl -fsSL https://github.com/iSlipperyX/ClipMostr/releases/latest/download/install-linux.sh | sudo bash
```

It takes 15 to 30 minutes the first time. It installs ClipMonster in `/opt/clipmonster` with what it needs: Python, ffmpeg, Node.js, Google Chrome for posting, Firefox for the YouTube sign-in, and the local AI and whisper.cpp built for the VM's CPU (`--gpu vulkan` builds them for an NVIDIA card; `--no-ai-build` skips them). It runs as its own user, `clipmonster`, whose settings, sign-ins and clips live in `/var/lib/clipmonster`. Three services keep it going and start again after a reboot:

| Service | |
|---|---|
| `clipmonster-display` | A virtual screen. Posting runs Chrome on it like a window you can see. |
| `clipmonster` | Automation, with the Mac app's server and the Telegram bot. |
| `clipmonster-desktop` | Only while you ask for it: that screen, with ClipMonster's own window, in your web browser. |

Running the same command again updates ClipMonster and keeps everything. So does `sudo clipmonster update`. Options: `--version X.Y.Z`, `--dev` (the newest code), `--port N` (the Mac app's port), `--tailscale`, `--no-swap`. It adds a swap file when the VM has none.

**Setting it up: `sudo clipmonster desktop`.** This turns on a desktop with ClipMonster's window open, reachable only through an SSH tunnel. The command prints the exact line to run on your Mac, like:

```
gcloud compute ssh clipmonster-vm --zone us-east1-b --project my-project -- -N -L 6080:localhost:6080
```

Then open `http://localhost:6080/vnc.html?autoconnect=1&resize=scale` in your browser. There you do what the [first start](#running-it) does on Windows:

- Fill in your keys in Settings.
- Add your TikTok accounts and sign in to each one in the Chrome window that opens.
- Sign in to YouTube (Settings → Download).
- Make your categories.

`sudo clipmonster desktop off` closes the desktop when you're done. Automation keeps running either way.

**Linking your Mac: `sudo clipmonster link-mac`.** It shows the **IP address**, **port** and **authorization code** to type in ClipMonster for Mac (Your PC → Link your PC), and waits until the Mac is linked. The address is the VM's external IP, or its Tailscale address when Tailscale is installed.

Google Cloud blocks the Mac app's port until a firewall rule allows it. `sudo clipmonster firewall` prints the two `gcloud` commands for your VM. They allow only your home internet address, which [ifconfig.me](https://ifconfig.me) shows you. If your home address changes often, use Tailscale instead: `--tailscale` installs it, then run `sudo tailscale up` on the VM, install Tailscale on the Mac, and type the VM's Tailscale address in the Mac app. That way no port is opened at all.

**Every day:**

| `sudo clipmonster …` | |
|---|---|
| `status` | The services, the Mac app's server, the linked Macs and automation's queue |
| `link-mac`, `unlink NAME` | Link or unlink a Mac |
| `desktop [on/off]` | ClipMonster's window and the browsers, for sign-ins and Settings |
| `start`, `stop`, `restart`, `logs [-f]` | Automation |
| `update [--dev]`, `uninstall [--purge]` | `--purge` also deletes the settings, sign-ins and clips |
| `ytclip ARGS` | Any [command-line](#command-line-ytclipexe) command, as ClipMonster's user |

**Good to know:**

- **YouTube often asks cloud servers to sign in.** Its "confirm you're not a bot" check is much more common from a data centre than from home. Signing ClipMonster in to YouTube (a spare Google account) usually gets past it. When it doesn't, automation pauses downloads and tells you, the same as on the PC.
- **TikTok sees a data-centre address.** Posts from a cloud server can get less reach, or need a captcha more often, than posts from home. Captchas show up on the desktop, where you can solve them. Watch the first week's views before you move every account over.
- **Notifications** come through the Mac app and Telegram. Windows' own pop-ups don't exist on Linux.

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

Automation's clips go to `<output>/Automation` in the same layout, inside a folder for each category when you have categories: `Automation/Gaming/<channel>/<date> <title> [<id>]/`. A clip request's clips go to `Automation/Requests/<date> <N clips a-b s>/` (inside the category's folder when it's for one).

Full downloads are cached in `<output>/.downloads`, so re-analysing or re-exporting never downloads a video twice. Your settings, presets and session live in `%APPDATA%\ClipMonster`. Each step's temporary files go in `%LOCALAPPDATA%\ClipMonster\work` and are deleted when the step ends; whatever a crash leaves there is cleared at the next start.

## Clean up

Clips, downloads and previews pile up. **🧹 Clean up…**, in the Library and Automation tabs, clears out what nothing needs any more:

| Kind | What it is | Ticked to start with |
|---|---|---|
| Unfinished downloads | Downloads that stopped part way and haven't been touched for half an hour | yes |
| Preview renders | Clips made by Render preview and the Layout tab's preview, in the work folder | yes |
| Downloaded videos | Full videos kept after clipping. Clipping one again downloads it again | no |
| Saved transcripts and analysis | A video opened again is measured again without them | no |
| Clips the quality check rejected | The `_rejected` folders. These are never posted | yes |
| Posted clips whose files were kept | Posting normally deletes a clip once it's up; this catches any it kept (say, one TikTok never showed on the profile). Records and stats stay | yes |
| Clips you took out of the posting queue | **Don't post** in the Queue tab. They never come back | yes |

Nothing goes until you've seen it: the window lists every file and how much space it frees. You can untick whole kinds or single files, choose **Only things older than** (posted clips count from when you posted them), or send everything to the Recycle Bin instead. Each video's `clips.json` and `README.md` are updated to match. Some things always stay:

- clips still waiting to be posted;
- anything for a video that's queued, being clipped or in the Studio's list;
- anything changed in the last 10 minutes;
- anything that isn't ClipMonster's own.

**Do this by itself once a day while automation runs** is on to start with: automation clears the ticked kinds once a day, at the age you chose (never less than a day). When automation deletes a full download after clipping it (*Delete full downloads after clipping*), the transcripts and measurements saved beside it go too.

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
ytclip "https://youtu.be/MATCH" --sport soccer --captions    # whole plays, found by the crowd and the commentary
ytclip "C:\Videos\stream.mp4" --layout "Gameplay below + title" --captions
ytclip "C:\Videos\stream.mp4" --stack "C:\Videos\parkour.mp4" --top-text "{title}" --bottom-text "Follow for part 2" --captions
ytclip "https://youtu.be/VIDEO" --stack "C:\Videos\Gameplay" --polish --captions --bleep --qc
ytclip qc "C:\Users\me\Videos\ClipMonster"      # check exported clips again
ytclip results                                  # what's working, from your posted clips' stats
ytclip posting                                  # TikTok accounts, what's waiting, what was posted
ytclip categories                                   # your categories and how each is doing
ytclip clean                                    # what Clean up would clear (--delete clears it)
ytclip create "Dark psychology secrets" --category Psychology --voice deep_male --creator-rewards
ytclip create --file prompts.txt --category DailyFacts --schedule   # a video per line, made by automation
ytclip create --script story.txt --storyboard-only                  # check the storyboard first (JSON)
ytclip create --check                           # what's ready here: script writer, voices, pictures
ytclip create --setup-pictures                  # AI pictures on this PC's graphics card (about 10 GB, once)
ytclip studio "Why the Roman Empire really fell" --category History   # the Studio: a video and its timeline
ytclip studio --url https://example.org/article --format long --seconds 480   # retell a public article, wide
ytclip studio --timeline "path\to\video folder"   # render an edited timeline.json again
ytclip report --week                            # the weekly report: each TikTok account's past 7 days
ytclip sports-find "red card or player ejection" "https://youtu.be/MATCH" --sport soccer
ytclip sports-find "technical foul on bench" game7.mp4 --sport basketball --feed timeline.json --json
ytclip sports-montage "https://youtu.be/MATCH" --sport soccer --order climax --transitions dissolve
ytclip sports-montage game.mp4 --sport soccer --events "red card or controversial call"
ytclip ai ask "Three hooks for a last-minute winner"   # the local AI first, Claude or Gemini if it can't
ytclip ai usage                                 # tokens and cost by day and model; ai route, ai hardware
ytclip self-test                                # check this install's libraries all load
ytclip --list-presets
```

Pro edit: `--pro` turns on everything in *Settings → Editing* · `--creator-rewards` clips over a minute · `--music FOLDER` · `--broll FOLDER` · `--ai local` the local AI. `ytclip local-ai [status|download|test]` checks, downloads or tries the local AI.

Main options: `-n` number of clips · `--min/--max` length in seconds · `--mode auto|ai|chapters` · `-f original|vertical_blur|vertical_crop|square` · `--captions` · `--polish` AI hooks and caption fixes · `--bleep` mask swearing · `--qc` check clips after export · `--stack VIDEO|FOLDER` · `--keywords` · `--sport soccer|basketball|…` clip it as [sports](#sports-categories) · `-q` max download resolution · `--encoder` · `--limit` videos per playlist · `-o` output folder · `--dry-run` analyse without exporting · `--cookies-from-browser clipmonster` to download with ClipMonster's own sign-in (`ytclip sign-in`), or `chrome`, `firefox`... for a browser's.

### The AI router and what it costs

Everything in ClipMonster that asks an AI goes through one place, and new code can use `ytclip.intelligence.agent_router.query_intelligence(prompt, schema=None, priority="local_first")`:

- **Tier 1, local**: the GGUF model on this computer (or a linked PC's), with the answer held to your JSON schema by llama.cpp's grammar. Free.
- **Tier 2, cloud**: Claude or Gemini, whichever has a key (your pick in Settings → AI first). Used when the local model isn't set up, runs out of memory, takes longer than *Local AI time limit* (Settings → AI, 240 s), answers in the wrong shape, or when you ask for deep reasoning (`priority="cloud_deep"`). The other priorities are `cloud_first`, `local_only`, `cloud_only` and `settings` (the order in Settings → AI).
- **Counting.** Every call's tokens and cost are added to `ai_usage.json` in the app's folder (`%APPDATA%\ClipMonster` or `~/Library/Application Support/ClipMonster`), by day, AI and model. Prices are built in and can be overridden with an `ai_prices.json` beside it. **Settings → AI** has a daily and a monthly spending limit in dollars: when one is reached, paid AIs are skipped and the local one carries on.

`ytclip ai ask "…"`, `ytclip ai route` (who would be asked, and what's missing), `ytclip ai usage` and `ytclip ai hardware` (what each kind of model runs on here) show it from the command line.

## Updates

ClipMonster updates itself from this project's GitHub Releases, on the channel picked in **Settings → Updates**:

| Channel | Gets | Checks |
|---|---|---|
| **Stable** | finished versions (`python release.py 2.8.0`) | about once a day |
| **Beta** | those, and test versions of the next one (`2.9.0-beta.1`) | every 6 hours |
| **Dev** | those, and a new build every time `main` changes, versioned like `2.8.0+dev.5` (5 changes after 2.8.0) | every 2 hours |

- New versions download quietly in the background and are checked against their SHA-256. The bar then says the update is ready: **Restart now** installs it in a few seconds, and closing ClipMonster installs it too. With the window closed, automation installs updates itself between videos and carries on. Turn that off with *Get updates by themselves*; you're then offered each update instead.
- Switching to a steadier channel offers its newest version straight away (**Switch now**), even though its number is lower.
- **Go back to** (Settings → Updates) reinstalls the version you had before the last update, and skips the one you left.
- The update download is a zip packed with LZMA, the smallest zip there is. Windows' own *Extract all* can't open it: use 7-Zip, or run `ClipMonster-Setup.exe` to install by hand. ClipMonster's updater unpacks it itself.
- Downloads, the unpacked update and the previous version are kept in `%LOCALAPPDATA%\ClipMonster\updates`. The download is deleted once unpacked, and the unpacked copy once it's installed. The update helper's log is `updates\update.log` there.
- Installing replaces only the app's own files, with the old ones kept until the new ones are in; if anything fails, the old version is put back. Settings, history and clips are never touched.
- From the command line: `ytclip --check-update`, `ytclip --update` (add `--channel beta` to try another channel once), `ytclip --update --go-back`.
- Running a newer `ClipMonster-Setup.exe` also updates an installed copy.
- The repository must be **public** so the app can see its releases without a login.

## Publishing a release

Every push to `main` publishes a **dev build** for the Dev channel by itself: the pre-release tagged `dev`, replaced each time, with the changes since the last release as its notes.

For a release, write `release-notes/v2.9.0.md` (what the app and the release page show), merge it to `main`, then on `main`:

```
python release.py 2.9.0
```

For a beta, which only the Beta and Dev channels get (release notes optional):

```
python release.py 2.9.0-beta.1
```

`release.py` checks you're on an up-to-date, clean `main`, sets the version, commits, tags, and pushes `main` and the tag. The **Release** workflow in GitHub Actions then builds on Windows (about 15 minutes) and publishes `ClipMonster-Setup.exe`, `ClipMonster-windows.zip` (what updates install) and `SHA256SUMS.txt`. Never make releases by hand on GitHub's website: installed copies only trust what the workflow publishes with checksums.

**Without a git copy:** merge a pull request that sets `__version__` in `ytclip/__init__.py` and adds the release notes, then go to **Actions → Release → Run workflow** on GitHub, keep the branch on `main`, tick **Publish** and run it. The workflow builds main, tags it `v<version>` and publishes it, exactly as a tag from `release.py` would. It refuses a version that's already released.

Pull requests that change the build also run the workflow, without publishing, and keep the files as a workflow artifact. Builds record which repository they came from, so they need no update configuration.

## Building from source

Needs Python 3.10+.

```
pip install -e .[whisper,build]
python build.py
```

This produces `dist/ClipMonster/` and `dist/ClipMonster-windows.zip`, and `dist/ClipMonster-Setup.exe` when [Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed (`winget install JRSoftware.InnoSetup`; `python build.py --installer` fails without it). To include Whisper for the graphics card, first install the Vulkan SDK and run `python packaging/build_whisper_cpp.py build/whisper-cpp --vulkan`; `build.py` then copies it in. The local AI's `llama-server` is built the same way: `python packaging/build_llama_cpp.py build/llama-cpp --vulkan`. The release workflow does all of it. From source without that build, `pip install -e .[local]` installs llama-cpp-python, which the local AI uses instead. During development, run the app with `python -m ytclip.gui.app` and the command line with `python -m ytclip`.

To build ClipMonster for Mac on a Mac (with Xcode's command line tools and CMake): `pip install -e .[mac,build]`, then `python packaging/build_mac.py`. It gathers ffmpeg and Deno from their packages and builds whisper.cpp and llama.cpp (for Metal on Apple silicon) into `build/mac-tools`, then writes `dist/ClipMonster.app` and `dist/ClipMonster-mac.dmg` (`--name ClipMonster-mac-intel.dmg` on an Intel Mac). `dist/ClipMonster.app/Contents/MacOS/ClipMonster --self-check` starts everything the app brought. Run it from source with `python -m ytclip.mac`.

The [event search](#search-by-in-game-event)'s sentence model runs on ONNX Runtime. Install **one** build (they share a module name): `pip install -e .[search]` (the CPU; on a Mac this build includes Core ML), `pip install -e .[search-directml]` (Windows, any DirectX 12 card: AMD, Intel Arc, NVIDIA) or `pip install -e .[search-cuda]` (NVIDIA with CUDA 12 and cuDNN 9). The Windows app ships the standard build, with the Kokoro voice. `ytclip ai hardware` shows what's used.

Run the tests with `pip install -e .[test]` and then `pytest`. They need no network or API key.

| Module | Role |
|---|---|
| `ytclip/engine.py` | Pipeline: open → measure (cached) → transcribe → find clips → export |
| `ytclip/analyze.py` | Signals, scoring and clip picking |
| `ytclip/sports.py` | Sports categories: the crowd, commentary-call and replay signals, and cutting each clip as a whole play |
| `ytclip/sportsdesk/` | The Sports tab: the filter and finding games (`games.py`), ranking a game's plays with the AI or the signals (`rank.py`), the shared work queue and its worker (`desk.py`), and recap reels: the cut and the words (`montage.py`), following the action (`framing.py`), reading the scoreboard (`scoreboard.py`), the graphics (`overlays.py`), the mix (`sound.py`) and the render (`reel.py`). The page is `gui/sports_view.py`, on the Mac `mac/sports_page.py` |
| `ytclip/studio/` | The Studio: the timeline format and its validation (`timeline_schema.py`), reading a public article (`scraper.py`), writing the script to a word budget (`scriptwriter.py`), voices local and cloud (`voice_studio.py`), a picture for each scene from B-roll, stock sites or a model (`visual_sourcer.py`), laying it all out (`timeline_builder.py`) and the run as a state machine (`orchestrator.py`) |
| `ytclip/editor/` | The Studio's editing: keyframes and camera moves (`motion_engine.py`), kinetic captions as .ass (`kinetic_subtitles.py`), the three-bus mix with ducking (`audio_mixer.py`) and the timeline rendered in one ffmpeg run (`timeline_compiler.py`). `gui/timeline_widget.py` draws a timeline |
| `ytclip/ai.py` | AI clip finder, and the one place Claude and Gemini are called from (choosing the AI, falling back) |
| `ytclip/intelligence/` | Local AI: hardware and free memory (`hardware.py`), the model catalogue and download, the model file's header (`gguf.py`), the layer split and memory length (`planner.py`), llama.cpp (`llama_engine.py`), the transcript parser; offline voice and sentiment signals |
| `ytclip/diagnostics.py` | Graphics card failures that Whisper and the local AI worked around, written to `automation.log` |
| `ytclip/vision/` | Face tracking, the virtual camera (follow, punch-ins, push-ins), B-roll choice, rendering the moved picture |
| `ytclip/audio/`, `ytclip/rendering/` | Dead-air and filler cuts, ducking, mastering; the edit plan, the audio graph, kinetic caption tags, hook card and lower-third |
| `ytclip/compliance/duration_guard.py`, `ytclip/posting/tracker.py` | Creator Rewards: clips over a minute, and progress to the program's goals |
| `packaging/linux/` | The Linux host: `install.sh` (attached to releases as `install-linux.sh`), the `clipmonster` admin command, the virtual screen and desktop, and their systemd services. `ytclip/linux_host.py` sets a host's settings the first time |
| `ytclip/remote/` | The Mac app's server inside automation: linking (`protocol.py`, shared with the Mac), the PC's certificate, the linked Macs (`state.py`), what the Mac can see and do (`host.py`), clips sent to the Macs (`deliveries.py`), HTTPS and discovery (`server.py`) |
| `ytclip/mac/` | ClipMonster for Mac: its window (the Studio and Library with Mac menus, `window.py`), the first start (`welcome.py`), the PC's local AI (`pc_ai.py`) and videos made on the PC (`pc_jobs.py`), and the Your PC pages: the pinned connection and Keychain, finding PCs, the loopback video proxy (the Sports clips inbox: `inbox.py`) |
| `ytclip/gemini.py` | Google's Gemini API over plain HTTPS: streamed JSON answers, key checks |
| `ytclip/polish.py` | AI polish: hooks, caption fixes, speaker crops, burned-in subtitle check |
| `ytclip/qc.py` | Quality check: silence, black/frozen picture, length/size, mid-sentence edges, AI ratings |
| `ytclip/stackpool.py` | Deals a different second video to each clip from a folder |
| `ytclip/trending.py` | YouTube trending charts, replay checks, research history |
| `ytclip/clipability.py` | Clip-ability score for trending videos |
| `ytclip/research.py` | One research scan, shared by the Research tab and `ytclip research` |
| `ytclip/sources.py` | Channel list and Creative Commons searches for Research |
| `ytclip/policy.py` | Picking rules, each category's share, and the log of picks |
| `ytclip/categories.py` | Categories: each TikTok account's sources, look and folder, and which category a video goes in |
| `ytclip/cleanup.py` | Clean up: what nothing needs any more, and clearing it; the work folder's startup sweep |
| `ytclip/creator/` | Video Creator: the storyboard (`director.py`), looking it up (`web.py`) and checking it (`factcheck.py`), voices (`voice_engine.py`), pictures (`visual_generator.py`, and the one-click picture engine `picture_engine.py`), camera moves (`motion.py`), sound effects and music (`sfx_library.py`, `soundbed.py`), rendering and the mix (`compositor.py`), the whole run (`orchestrator.py`) |
| `ytclip/posting/service.py` | The posting worker, a thread of automation's background process: intake, posting, readings, records |
| `ytclip/posting/intake.py`, `store.py`, `jobs.py`, `schedule.py` | New clips into the posting queue for their account; the library; what posts when |
| `ytclip/posting/accounts.py`, `actions.py` | TikTok accounts; the window's requests to the worker (log in, post now, test posting) |
| `ytclip/posting/tiktok/` | TikTok Studio in Edge or Chrome (Playwright): login, posting, the upload-page check; how long an upload may take (`upload_wait.py`) |
| `ytclip/posting/live.py`, `results.py`, `records.py` | Profile readings and the feed; results for learning; each posted video's record, then its files deleted |
| `ytclip/updater.py`, `swap.py` | Update channels, background downloads, going back, the installer and old copies; the helper that swaps the files |
| `ytclip/pipeline.py` | Automation: the unattended runner, recipes, pacing and retries |
| `ytclip/jobs.py` | `jobs.db`, the automation queue shared by the runner and the app |
| `ytclip/transcribe.py` | Whisper transcription: whole videos (with checkpoints) or just the clips |
| `ytclip/whisper_cpp.py` | Whisper on the graphics card (whisper.cpp with Vulkan), and the CPU taking over when the card fails |
| `ytclip/speed.py` | Speed: Whisper threads, automation's priority, the graphics card |
| `ytclip/export.py`, `captions.py` | Reframing, captions, encoding |
| `ytclip/gui/` | Desktop app (PySide6) |
| `packaging/` | PyInstaller spec, the Inno Setup script (`ClipMonster.iss`), launchers, icon, the whisper.cpp build (`build_whisper_cpp.py`) |

Only download and repost videos you have the rights to use.
