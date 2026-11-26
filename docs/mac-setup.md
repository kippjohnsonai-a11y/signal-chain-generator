# Setting up a new Mac (Apple Silicon)

About 20-30 minutes, most of it waiting on downloads. Every command below goes into **Terminal** (press Cmd+Space, type "Terminal", press Return). Paste one block at a time and press Return.

## 1. Install Homebrew (the Mac package manager)

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

- It asks for your **Mac login password**. Nothing shows while you type; that's normal.
- It offers to install the **Xcode Command Line Tools** (which include `git`). Say yes and press Return when asked. This takes 5-15 minutes.

When it finishes it prints **"Next steps"**. Run these so Terminal can find `brew`:

```bash
echo >> ~/.zprofile
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

Check it worked: `brew --version` should print a version number.

## 2. Install the tools the app needs

```bash
brew install uv libusb
```

- **uv** installs Python and the app's libraries
- **libusb** lets the app talk to the pedal over USB

## 3. Download the app

```bash
cd ~
git clone https://github.com/kippjohnsonai-a11y/signal-chain-generator.git stompxl
cd stompxl
```

## 4. Install HX Edit and back up the pedal

1. Download **HX Edit** from [line6.com/software](https://line6.com/software/) and install it. The app needs it installed to send presets to the pedal.
2. Connect the Stomp XL by USB. If macOS asks **"Allow accessory to connect?"**, click Allow.
3. Open HX Edit, then **File → Backup** to make a full backup of the pedal.
4. **Quit HX Edit.** Only one program can talk to the pedal at a time.

## 5. Get an Anthropic API key

1. Go to [console.anthropic.com](https://console.anthropic.com), sign in, and add some credit under **Billing** ($5-10 goes a long way).
2. **Settings → API Keys → Create Key**. Copy the key (it starts with `sk-ant-`).

## 6. Start the app

```bash
uv run stompxl
```

The first run downloads Python 3.13 and the app's libraries (a minute or two), then opens **http://127.0.0.1:8765** in your browser.

In the app:
1. **Settings**: paste the API key and save.
2. **Guitars**: add your guitars and their pickups (for example a Stratocaster with SSS single coils).
3. Type a song and **Generate**.

Before sending anything to the pedal, read [device-verification.md](device-verification.md).

## Everyday use: one-click launcher

Run this once:

```bash
cd ~/stompxl && bash scripts/make-mac-app.sh
```

It creates **StompXL Tone Studio** in the Applications folder inside your home folder (in Finder: Go → Home → Applications). Drag it to the Dock. Every click:
- downloads the latest version of the app
- restarts the app if it's running an older version, or starts it if it isn't running (no Terminal window)
- opens it in your browser

**Which version am I running?** It's shown next to the app's name at the top of the page, e.g. `v1a2b3c4 · 2026-09-25`.

**Switching to HX Edit:** you don't need to stop anything. The app only talks to the pedal while it's sending a preset, reading slot names, or checking the connection from a visible browser tab. Close the tab (or just switch away from it) and open HX Edit. Quit HX Edit before sending a preset from the app.

The server keeps running quietly until you restart the Mac. To stop it by hand: `pkill -f "stompxl --no-browser"`. If it won't start, the details are in `~/Library/Logs/stompxl.log`.

The first click after setup can take a minute or two while it installs Python and the libraries.

Prefer Terminal? `cd ~/stompxl && uv run stompxl`, and press Ctrl+C to stop.

## Adding a second user on the same Mac

Each macOS user account gets its own copy of the app with its own guitars, library, settings, and API key (all kept in that user's `~/Library/Application Support/StompXL Tone Studio/`). The pedal, HX Edit, and Homebrew are shared. About 10 minutes.

**Skip these for the second user:** installing Homebrew (step 1), `brew install` (step 2), and installing HX Edit (step 4). They're already on the Mac, and only the first user can run `brew install` anyway.

Log in as the second user, open Terminal, and run:

**1. Let Terminal find the tools the first user installed:**

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
uv --version
```

It should print a version number.

**2. Download the app:**

```bash
cd ~
git clone https://github.com/kippjohnsonai-a11y/signal-chain-generator.git stompxl
cd stompxl
```

**3. Make the one-click launcher and open it:**

```bash
bash scripts/make-mac-app.sh
open ~/Applications/"StompXL Tone Studio.app"
```

The first click takes a minute or two while it installs Python for this user.

**4. In the app:** Settings, paste an API key (the same key as the first user works, or make a separate one to track spending), then add this user's guitars.

**Good to know:**
- Each user's launcher uses its own address: the first user is `http://127.0.0.1:8765`, the second is `:8766`, and so on. That way, if the first user's app is still running in the background, the second user still opens their own library, not the first user's. If you start it from Terminal instead, use `uv run stompxl --port 8766`.
- Only one program can talk to the pedal at a time, across all users. Before sending a preset, quit HX Edit in **both** accounts (fast user switching keeps the other account's apps running). The other user's StompXL app is fine to leave running; it only touches the pedal while it's sending, or while its tab is visible.
- To share a preset between users, download the `.hlx` from one account and import it in the other (put it in `/Users/Shared` to hand it over). Guitars are entered separately in each account.

## If something goes wrong

| Problem | Fix |
| --- | --- |
| `zsh: command not found: brew` | Run the three "Next steps" lines from step 1, or open a new Terminal window. |
| `command not found: uv` | Run `brew install uv libusb` again. |
| Top bar says **Pedal: USB unavailable** | `brew install libusb`, then stop the app (Ctrl+C) and start it again. |
| Top bar says **Pedal: not connected** | Quit HX Edit, check the USB cable, and try another port (not a hub). |
| "HX Edit's symbol table was not found" | Install HX Edit (step 4); keep it installed but closed. |
| "address already in use" | Another copy is already running, maybe in another user's account. Use the launcher (it picks a port per user), or start with `uv run stompxl --port 8767`. |
| "Claude rejected the API key" | Paste the key again in Settings, and check you have credit in the Anthropic console. |
