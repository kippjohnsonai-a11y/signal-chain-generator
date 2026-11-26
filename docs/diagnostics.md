# Send diagnostics

Whenever something goes wrong, tap **Report a problem** (in the top menu, or
**Report** in the bar at the bottom while editing or in Live). Every error
message also has a **Send diagnostics** button, and it's in Settings too. It
works for problems that never show an error, like "the pedal didn't change":
pick one of the quick notes (Pedal didn't change, Live not playing, Send
failed...) and tap Send. One tap posts an issue to this repository, labelled
`diagnostics`, which a Claude session can read with its GitHub access. No
terminal, no `sudo grep` on the Mac mini.

**Without a token** (the usual case for a fresh install), the button reads
**Copy and open GitHub**: it copies the report and opens a new issue on the
app's repository, titled from your note, for you to paste it into and submit.
**Save as a file** downloads it instead, to attach or send. Issues on a public
repository are public, so read it first under "See what gets sent".

## What gets sent

- The commit the server is running (what `/api/version` says).
- The error the app showed (the last one in the past half hour), and an
  optional note on what you were doing. The issue title is the note, or else
  the error.
- What this page showed and sent lately: every message it showed, every
  request that failed, and what each live edit update did ("sent 2",
  "refused: Mix", "waiting: snapshot 2").
- The browser and whether this page's USB relay was active.
- The last five pedal failures (sends, slot reads, live edit starts, drops and
  ends), newest first, each with the last frames on
  the wire and, for a relayed pedal, the last relayed USB calls. These are kept
  in `<data dir>/diagnostics/failures.json`, so a restart doesn't lose them.
- The names of the newest files in `backups/` (not their contents).
- The app's last 300 log lines since it started, including every error the
  app returned to a page.

Before it leaves the Mac, every email address, API key, GitHub token,
TONE3000 key and any value in an env variable named like a key, token, secret
or password is replaced with `[redacted]`. Preset data (frame headers and
parameter values) stays in: that is what makes it useful. The repository is
private. Guests don't see the button and can't use it.

"See what gets sent" in the dialog shows the exact text first.

## One-time setup: the token

1. On GitHub: Settings > Developer settings > Personal access tokens >
   Fine-grained tokens > Generate new token.
2. Repository access: **Only select repositories** > `stompxl`.
3. Permissions > Repository permissions > **Issues: Read and write**. Nothing
   else.
4. Generate, copy, then in the app open Settings, paste it into **GitHub token
   for diagnostics**, and Save.

Or put it in the env file as `STOMPXL_DIAGNOSTICS_TOKEN` and restart.

Without a token the dialog still builds the bundle: **Copy instead** puts it
on the clipboard to paste into a Claude thread.

`STOMPXL_DIAGNOSTICS_REPO` (`owner/name`) sends to a different repository.
