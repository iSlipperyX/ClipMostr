# Uploading clips to Google Drive

ClipMonster uploads finished clips to your Google Drive by itself. Google needs to know which app is asking, so you make a free "OAuth client" once, in your own Google Cloud account, and paste it into ClipMonster. It takes about 10 minutes. Nothing here costs money.

ClipMonster only gets the **drive.file** permission: it can see and change the files it uploads, and nothing else in your Drive.

## 1. Make a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in with the Google account whose Drive the clips should go to.
2. Click the project picker at the top left, then **New project**.
3. Name it `ClipMonster` and click **Create**. Make sure it's selected in the project picker afterwards.

## 2. Turn on the Drive API

1. Open [APIs & Services → Library](https://console.cloud.google.com/apis/library).
2. Search for **Google Drive API**, open it and click **Enable**.

## 3. Tell Google about the app

1. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview) and click **Get started**.
2. **App name**: `ClipMonster`. **User support email**: your email. Click **Next**.
3. **Audience**: pick **External**. Click **Next**.
4. **Contact information**: your email. Click **Next**, tick the agreement and click **Create**.
5. Open **Audience** in the left menu and click **Publish app**, then **Confirm**. The status should say **In production**.

   This matters: while the app is in *Testing*, Google signs ClipMonster out every 7 days. Google doesn't need to review it, because ClipMonster only asks for the basic drive.file permission.

## 4. Make the client ClipMonster signs in with

1. Open **Clients** in the left menu and click **Create client**.
2. **Application type**: **Desktop app**. **Name**: `ClipMonster`. Click **Create**.
3. A window shows the **Client ID** (it ends in `.apps.googleusercontent.com`) and the **Client secret** (it starts with `GOCSPX-`). Keep it open, or download the JSON.

## 5. Sign in from ClipMonster

1. In ClipMonster, open **Settings → Upload**.
2. Set **Upload finished clips to** to **Google Drive**.
3. Paste the **Google OAuth client ID** and **Google OAuth client secret**.
4. Click **Sign in to Google Drive…**. Your browser opens Google's sign-in page.
5. Pick your account. Google may say "Google hasn't verified this app": click **Continue** (it's your own app). Allow ClipMonster to see the files it uses in your Drive.
6. The browser says *ClipMonster is signed in*. Close the tab. Settings shows **✓ Signed in as** your email.
7. Click **OK**.

That's it. Clips go into a `ClipMonster` folder in your Drive (change the name under **Folder in your Drive**), in the same folders as on your PC: `ClipMonster/Automation/<niche>/<channel>/<video>/01 ….mp4`. Each file's description in Drive has the clip's title, score and the video it came from.

## How uploading works

- A clip uploads within a few minutes of being exported, as long as ClipMonster or automation is running. Clips the quality check rejected never upload.
- Big clips go up in pieces. If the internet drops, or the PC restarts, the upload carries on from where it stopped.
- A clip exported again replaces its copy in Drive.
- The Automation tab shows what's uploading, what's waiting and what failed. `ytclip upload` shows the same.
- If Google stops accepting the sign-in, or your Drive is full, uploads pause and say why. Fix it, then click **Retry failed uploads** in Settings → Upload.
- Each video folder's `clips.json` goes up too, just before its clips. It doesn't count as a clip in the
  status, and it's replaced when it changes.
- **TikTok Manager** (1.9+) can download the clips from Drive by itself, with their titles, scores, hooks,
  niche and part series from that `clips.json`: on this PC or another one. It signs in with this same OAuth
  client, so there's nothing new to set up: see [its guide](https://github.com/iSlipperyX/TTUpdates/blob/main/docs/google-drive.md).
- Clean up's **Clips safely uploaded** clears clips from the PC once the same file is in Drive and TikTok Manager doesn't need it any more. The copy in Drive stays.

## Command line

```
ytclip upload               # what's uploading, waiting and failed
ytclip upload --sign-in     # sign in (uses the client ID and secret from Settings)
ytclip upload --now         # upload everything waiting, then stop
ytclip upload --retry       # try failed uploads again
ytclip upload --sign-out
```

## If something goes wrong

| ClipMonster says | Fix |
|---|---|
| *Google doesn't accept the OAuth client ID and secret* | Copy them again from Google Auth Platform → Clients. The client must be a **Desktop app**. |
| *The Google Drive API isn't turned on in your Google Cloud project* | Step 2. It can take a few minutes to start working. |
| *Google Drive stopped accepting ClipMonster's sign-in* after about a week | The app is still in Testing. Do step 3.5, then sign in again. |
| *Error 403: access_denied* in the browser | The app is in Testing and your account isn't a test user. Publish it (step 3.5), or add yourself under Audience → Test users. |
| *Your Google Drive is full* | Free up space in Drive, or buy more storage, then Retry failed uploads. |

To sign ClipMonster out of your account, click **Sign out** in Settings → Upload. You can also remove it at [myaccount.google.com/connections](https://myaccount.google.com/connections).

## Uploading to a NAS or another folder instead

Set **Upload finished clips to** to **A folder**, and pick the folder: a NAS share (`\\nas\clips`), a mapped drive, or a folder another app syncs (Dropbox, OneDrive). Clips are copied there in the same folders, and Clean up treats them as uploaded in the same way.
