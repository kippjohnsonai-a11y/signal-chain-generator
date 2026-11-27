# Signal Chain Generator for HX Stomp XL

Type a song, pick one of your guitars, get a dialled-in **Line 6 HX Stomp XL** preset, and send it straight to the pedal.

The original **HX Stomp** works too (new, in beta): plug it in and the app makes three-snapshot presets for it. See [docs/hx-stomp.md](docs/hx-stomp.md).

It runs on your own Mac or Windows PC, which keeps your library and talks to the pedal. Your phone, tablet and other computers open it in a browser. Nothing goes through anyone else's server: your songs stay on your computer, and Claude is called with your own API key.

*Formerly "StompXL Tone Studio". Not affiliated with Line 6; see [Legal](#legal).*

- **Song → preset.** "Enter Sandman by Metallica" becomes a full signal chain with **4 snapshots, one per song section** (e.g. INTRO / VERSE / CHORUS / SOLO), tempo-synced to the song.
- **Tailored to your guitar.** Your guitars are saved (pickups, tuning, notes). Every setting is adjusted for the one you pick: a Strat's single coils get more gain and less treble than a Les Paul's humbuckers, and so on.
- **Your rig.** Pick an output profile each time: headphones/FRFR/PA (full amp+cab), front of a real amp (no amp/cab models), the 4-cable method, or one of two [NAM rigs](docs/nam.md) where a Neural Amp Modeler capture is the amp, either on the pedal (NAM A2 Lite firmware) or on the computer over USB.
- **Wah / expression / looper / Poly Capo toggles.** Turn each on or off per preset. A live **DSP room** box shows what your rig and toggles already use and which other pedals (and how many of each) still fit, so the preset passes to the pedal. Claude also says whether the song *benefits* from each one and where to use it (e.g. wah on the solo snapshot, EXP 1 on the sweep).
- **Plain-English tweaks.** "Too muddy", "the solo doesn't cut through". Claude works out what that means (low cut up, bass down, less gain...), makes a new version, and lists every value that changed.
- **Standing instructions.** Tell it things once ("I play at bedroom volume", "never use chorus") for all guitars or one guitar, and every future preset follows them. Any tweak can be saved as one with a checkbox.
- **Library with history.** Every song keeps a tree of versions. Go back to any version, star a favorite, or **fork** one to another guitar or rig and Claude re-dials it from there.
- **Same song, other guitar.** **Other guitar** on a version remakes it for another of your guitars (say, Les Paul to Strat) with the exact same signal chain: same blocks in the same order, NAM capture, footswitches and gate. Claude only re-dials gain, EQ, levels and the snapshots for the new pickups. (Fork has the same "Keep the same signal chain" box.)
- **Folders, tags and search.** Group songs into folders and tag versions (e.g. "live", "acoustic"). Versions that use a NAM capture or a Poly Capo are tagged **NAM** / **Poly Capo** automatically. Click tags in the sidebar to find songs with a version carrying all of them; the search box matches titles, artists and tags. **Select** in the version list picks several versions to tag or delete at once. All of it is stored in the library on the server, so every device sees the same folders and tags.
- **Live edit.** Tick **Live on pedal** while editing values and each change plays on the pedal straight away, like HX Edit. Save writes it to the slot; Discard puts the slot back. Off unless you turn it on, and it holds the USB connection while it's on.
- **Pedal close-up.** While editing, tap a block (or **Tune**) to open it big: sliders with − / + steps for its main controls, and one-tap **quick picks** like "Less fizz", "Dotted 1/8" or "420 Hz cut". **+ Save as pick** turns what you just dialled in into your own pick for that pedal. Picks live in the library (My tone → Quick picks), so you change them in the app, not by redeploying. In Live, **Hold for original** A/Bs against where you started. A bottom sheet on a phone, a side panel on a computer.
- **Built for rehearsal.** Star songs to pin them under **Favorites**, with your last few songs under **Recent**. A folder is a set list: **Order** puts its songs in play order, and a song from it shows **◀ Prev · 3 of 14 · Next ▶**. One button per guitar in the song header opens that guitar's version, no Claude call (or "+ Strat" remakes it). Tap any pedal to tune it; the **Live** switch, Discard and Save sit in a bar at the bottom. A values-only save is named "Rehearsal Nov 15" and starred, so the song opens on it next time. On a phone, the song list and a song are separate screens, and every song has its own address, so Back, refresh and bookmarks work.
- **Push to the pedal** over USB into the slot you choose; the slot's old preset is backed up first. Or download the `.hlx` and drag it into HX Edit.
- **Best-practices library** in [`stompxl/knowledge/`](stompxl/knowledge): platform limits, chain order, gain staging, pickups, rigs, snapshots, wah/expression/looper, and how to interpret feedback. Every prompt includes it; edit or add a `.md` file to change how presets are built.

## Install

- **Mac:** download `Signal-Chain-Generator-VERSION.dmg` from the [latest release](https://github.com/kippjohnsonai-a11y/signal-chain-generator/releases/latest), drag the app to Applications, and open it.
- **Windows:** download `Signal-Chain-Generator-VERSION-Setup.exe` from the [latest release](https://github.com/kippjohnsonai-a11y/signal-chain-generator/releases/latest) and run it. No administrator rights needed. The installer isn't code-signed yet, so Windows may say "Windows protected your PC": click **More info → Run anyway**. Then open **Signal Chain Generator** from the Start menu; it opens in your browser, with no window of its own. Your library is in `%APPDATA%\Signal Chain Generator` (with `app.log` if something goes wrong). The pedal works over USB with Line 6's normal driver; close HX Edit while sending.
- **Either, from a terminal:** with [uv](https://docs.astral.sh/uv/), `uvx signal-chain-generator`.

## Setup on a Mac (Apple Silicon)

**Want it on your phone at home?** Turn on **More → Phones on this Wi-Fi** and scan the pairing QR code with the phone. Only paired phones get in, it's off until you turn it on, and the pedal stays plugged into the Mac.

**Want it on your phone everywhere, and on other computers?** Run it on the Mac mini and plug the pedal into whichever device you're holding: [docs/mac-mini.md](docs/mac-mini.md). To let a few friends in with their Google account, each with their own library: [docs/guests.md](docs/guests.md).

**New Mac, no Homebrew yet?** Follow the step-by-step guide: [docs/mac-setup.md](docs/mac-setup.md). **Adding another user account on the same Mac?** See [its section](docs/mac-setup.md#adding-a-second-user-on-the-same-mac).

With [uv](https://docs.astral.sh/uv/) installed (`brew install uv`), `uvx signal-chain-generator` is all it takes. To run from a checkout instead:

```bash
brew install uv                    # uv manages Python and the app's libraries
git clone https://github.com/kippjohnsonai-a11y/signal-chain-generator.git stompxl
cd stompxl
uv run stompxl                     # installs everything the first time, then opens http://127.0.0.1:8765
```

Then in the app (new to it? **? Help** at the top, or **More → How to use this app**, walks through everything step by step, with fixes for common pedal problems and a glossary):

1. **More → AI** → pick **Claude**, **ChatGPT** or **Gemini** and paste that company's API key ([Anthropic](https://console.anthropic.com/settings/keys), [OpenAI](https://platform.openai.com/api-keys) or [Google AI Studio](https://aistudio.google.com/apikey)). Keys are saved only on your Mac (`~/Library/Application Support/Signal Chain Generator/settings.json`, owner-read-only; installs from before the rename keep using `StompXL Tone Studio/`). Without a saved key, the app uses `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `GEMINI_API_KEY` from your shell; a saved key wins over it, so other tools keep theirs.
2. **Guitars** → add your guitars and their pickups (say, a Strat with SSS single coils).
3. Type a song, pick guitar + rig + toggles, **Generate**. It takes about a minute: four AI calls (song research → blocks → parameters → snapshots).

**For pushing to the pedal:** HX Edit must be **installed** (the app reads its model table) but **closed** while sending, since only one program can use the USB connection at a time. Connect the Stomp XL (or HX Stomp) over USB.

> **Before your first push:** back up the pedal in HX Edit (File → Backup). Then read [docs/device-verification.md](docs/device-verification.md). The USB upload code is in daily use on the HX Stomp XL and was first built on the regular HX Stomp; a first push to a new pedal is still worth doing as a supervised test.

## Cost

Claude is the default and what the app is tuned and tested with: the newest Claude Opus (now `claude-opus-5-5`) with adaptive thinking at **medium** effort. Settings has a model dropdown filled live from the provider's model list, so new models appear on their own; "Newest Opus" moves to each new Opus automatically. ChatGPT defaults to `gpt-5.6-sol` and Gemini to `gemini-pro-latest`; the thinking-effort setting carries over to both. A new preset is 4 calls and a tweak is 3; expect a few cents to low tens of cents per preset depending on model and effort. The cost shown on each preset uses list prices; your provider's billing page has the real bill.

## How it's built

| Piece | What it does |
| --- | --- |
| [`stompxl/helixgen/`](stompxl/helixgen) | Vendored [HelixGen](https://github.com/ORogers/HelixGen) (Apache-2.0): model catalog, `.hlx` generator, validator, reverse-engineered USB protocol. One local change: the snapshot count is a parameter, so 4-snapshot XL presets work. |
| [`stompxl/engine.py`](stompxl/engine.py) | The 4-round Claude pipeline, rig/toggle rules, tweaks and forks. |
| [`stompxl/xl.py`](stompxl/xl.py) | Pedal specifics: one profile each for the HX Stomp XL and HX Stomp (device ID, snapshots, preset banks, footswitches), expression pedal assignment. |
| [`stompxl/knowledge/`](stompxl/knowledge) | The best-practices library. |
| [`stompxl/db.py`](stompxl/db.py) | SQLite library: guitars, rigs, instructions, songs, the version tree. |
| [`stompxl/pedal.py`](stompxl/pedal.py) | Slot listing, backup-then-push over USB. |
| [`stompxl/server.py`](stompxl/server.py) + [`static/`](stompxl/static) | Local web app (FastAPI + a no-build-step single page). |
| [`stompxl/helixgen/device/relay.py`](stompxl/helixgen/device/relay.py) + [`stompxl/usb_relay.py`](stompxl/usb_relay.py) + [`static/relay.js`](stompxl/static/relay.js) | A pedal plugged into a phone or laptop: the page opens it with WebUSB and relays the bytes; the protocol stays in Python. |
| [`deploy/macos/`](deploy/macos) | Runs the app as a service on the Mac mini, published to your tailnet ([guide](docs/mac-mini.md)). |

Development: `uv run --extra dev pytest` runs the tests (HelixGen's 305 plus the app's), and `uv run --extra dev ruff check stompxl` runs the linter.

The vendored `helixgen` CLI still works for pedal utilities, e.g. a full backup of every slot:
`uv run helixgen backup --output ~/stompxl-backup`.

See [docs/global-settings.md](docs/global-settings.md) for the recommended pedal Global Settings and footswitch layout, and [docs/requirements.md](docs/requirements.md) for the requirements and roadmap.

## Legal

This software is provided "as is", without warranty of any kind (see [LICENSE](LICENSE)). It writes to your pedal using a protocol worked out by observing USB traffic, not one documented by Line 6. It backs up every slot before overwriting it, but keep your own HX Edit backup too.

The NAM capture converter is [nocaseguy/hx-stomp-nam-lite](https://github.com/nocaseguy/hx-stomp-nam-lite)'s `nam2ir.py` (GPL-3.0). It is downloaded at a pinned commit when you first use it and is not included in this repository. NAM captures from TONE3000 are downloaded with your own TONE3000 key; only their names and ids are listed here.

Helix, HX Stomp XL, HX Edit and Line 6 are trademarks of Yamaha Guitar Group / Line 6. This is an independent project, not affiliated with or endorsed by Line 6. See [NOTICE](NOTICE) and [docs/helixgen/legal.md](docs/helixgen/legal.md).
