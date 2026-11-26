# Run it on an always-on Mac, use it anywhere

The app runs all the time on a Mac that stays on (a Mac mini is ideal), in a user account of its own. Your phone and your other computers open it over Tailscale, so they all see one library of songs, guitars and instructions, and one API key.

The pedal can be plugged into whichever device you're holding:

- **Into the Mac mini** (at home): the app talks to it directly, as it always has.
- **Into your Android phone or a laptop** (on the go): tap **Use pedal here** in the app. Chrome on that device opens the pedal over USB and passes the bytes back and forth to the Mac mini. Every push, import and slot listing works the same way. The page has to stay open, with the screen on, while it's talking to the pedal.

How it works: a LaunchDaemon runs the app as that account, it listens on 127.0.0.1 only, and Tailscale Serve gives it an HTTPS address that only your tailnet can reach. HTTPS isn't optional here: Chrome only allows USB access on a secure page.

Below, `assistant` is the account's short name. If yours is different (run `id -un` in that account), use it everywhere instead.

## 1. Install (in the assistant account)

HX Edit must be installed on the Mac mini (in `/Applications`, so every account sees it). The app reads its model table to talk to the pedal.

```bash
brew install uv libusb             # skip any already installed (Homebrew is shared)
git clone https://github.com/kippjohnsonai-a11y/signal-chain-generator.git ~/stompxl
cd ~/stompxl && uv sync            # installs Python and the libraries once
```

## 2. Settings file (assistant account)

```bash
mkdir -p ~/.config/stompxl
cp ~/stompxl/deploy/macos/stompxl.env.example ~/.config/stompxl/stompxl.env
chmod 600 ~/.config/stompxl/stompxl.env
open -e ~/.config/stompxl/stompxl.env
```

Set `STOMPXL_ALLOWED_LOGINS` to your Tailscale login (the email you sign in to Tailscale with; `tailscale status` shows it). Everyone else on the tailnet gets a "this app is private" page. That matters because the app spends your API key and writes to the pedal.

## 3. Move your library in (only if you already have one)

The library is one folder: `~/Library/Application Support/StompXL Tone Studio/` (songs, every version, guitars, instructions, settings with the API key, pedal backups, NAM captures). Quit the old app first (or reboot), so the database isn't mid-write.

**Same Mac mini, from your own account** (admin account):

```bash
SRC="/Users/<your account>/Library/Application Support/StompXL Tone Studio"
DST="/Users/assistant/Library/Application Support/StompXL Tone Studio"
sudo ditto "$SRC" "$DST"
sudo chown -R assistant:staff "$DST"
```

**From another Mac**: copy that folder to the Mac mini (AirDrop, or `scp -r` over Tailscale), then put it at the `DST` path above and run the `chown`.

After the move, use the Mac mini address everywhere and stop opening the old app icon, or you'll have two libraries again.

## 4. Start the service and publish it (admin account)

```bash
sudo bash /Users/assistant/stompxl/deploy/macos/install.sh assistant
```

This installs `/Library/LaunchDaemons/com.stompxl.server.plist`, starts it, and runs `tailscale serve --bg --https=8780 http://127.0.0.1:8780`. It prints the address: `https://<mac-name>.<tailnet>.ts.net:8780`. It uses its own port, so anything already on 443 is untouched. If `serve` asks, turn on HTTPS certificates in the Tailscale admin console.

The log is `/Users/assistant/Library/Logs/stompxl.log`.

## 5. Your phone and computers

1. Install Tailscale and sign in with the same login you put in `STOMPXL_ALLOWED_LOGINS`.
2. Open `https://<mac-name>.<tailnet>.ts.net:8780` in **Chrome** (or Edge).
3. On Android: Chrome menu, then **Add to Home screen** (or **Install app**). It opens like an app after that.

## 6. Using the pedal on the go

1. Power the pedal from its own supply.
2. Plug it into the phone with a USB-C OTG cable (USB-C on the phone end, the pedal's USB port on the other).
3. In the app, tap **Use pedal here** and pick the HX device. Android asks once whether Chrome may use it.
4. The pedal pill reads "HX Stomp XL · on this Android phone". Send, import and slot lists now go through the phone.
5. Keep the page open with the screen on until a send finishes. Tap **Stop using pedal here** when you're done, or just unplug.

Next time, the phone reconnects by itself when the page is open and the pedal is plugged in. While a phone or laptop is relaying the pedal, the app uses that one even if another pedal is plugged into the Mac mini. The newest **Use pedal here** wins.

**First time on a new phone?** `https://<mac-name>.<tailnet>.ts.net:8780/static/usb-probe.html` checks that the phone can reach the pedal at all. It says hello and goodbye and changes nothing.

Works in Chrome or Edge on Android, macOS, ChromeOS and Linux. It doesn't work on iPhone or iPad (Safari has no USB access) or in Firefox. On Windows, Line 6's driver probably holds the pedal's control connection, so Chrome can't open it.

## Friends without Tailscale

To let a few people sign in with Google, each with their own library, see [guests.md](guests.md).

## After a code update

The service pulls the latest code each time it starts, so restarting it is enough (admin account):

```bash
sudo launchctl kickstart -k system/com.stompxl.server
```

## Things that still need the Mac mini itself

- **TONE3000 sign-in** only redirects back to a loopback address. Do it once in a browser on the Mac mini, logged in as the assistant account, at `http://127.0.0.1:8780`. Direct loopback visits like that skip the Tailscale login check; anything that comes in through Tailscale Serve is still checked.
- **"Open folder"** for NAM captures opens Finder on the Mac mini, not on your phone.
- **NAM on the computer** runs NAM in a DAW on whichever computer the pedal's USB audio is plugged into. That's audio, not something the app relays.

## Troubleshooting

- **A send or slot read failed and you want Claude to look**: tap **Send diagnostics** on the error (or in Settings). It posts the version, error and USB trace as an issue here; see [diagnostics.md](diagnostics.md) for the one-time token.
- **"This app is private"**: the login in `STOMPXL_ALLOWED_LOGINS` doesn't match the one the device is signed in to Tailscale with. Fix the env file, then kickstart the service.
- **The address doesn't load**: Tailscale is off on one side, or `tailscale serve status` no longer lists port 8780. Re-run step 4.
- **"Couldn't take over the pedal's control connection"**: HX Edit (or another app) has the pedal on that device. Quit it.
- **Send stopped partway on the phone**: the screen locked or Chrome went to the background, which closes the connection. The slot's previous preset is in the backups. Keep the screen on and send again.
