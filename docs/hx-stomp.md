# Using the original HX Stomp

The app was built for the HX Stomp XL, and the original HX Stomp (three switches) works too. Support for it is new: the USB code it uses was first written for an HX Stomp, but this app hasn't yet been run against one end to end. If anything looks wrong, tap **Report a problem** in the More tab.

## What's different

| | HX Stomp XL | HX Stomp |
| --- | --- | --- |
| Snapshots per preset | 4 | 3 |
| Presets | 128, banks of 4 (1A-32D) | 126, banks of 3 (1A-42C) |
| Changing sections | FS1/FS4 step up/down (Stomp Mode "4 Switch", Up/Down Switches "Snapshots") | FS1, FS2, FS3 pick snapshots 1, 2, 3 in **Snapshot mode** |
| Stomp switches | FS2, FS3, FS5, FS6 (looper on FS6) | FS1-FS3 in Stomp mode (looper on FS3) |
| Blocks, DSP, models | 8 blocks, one DSP | the same |

## Setup

1. Plug the HX Stomp in and start the app. It recognises the pedal and switches to HX Stomp presets on its own. You can also choose it in **More > Pedal setup > Your pedal**.
2. Put the pedal's footswitches in **Snapshot mode**, so FS1-FS3 pick the song's three sections. See "Footswitch Modes" in the HX Stomp owner's manual.
3. Presets the app makes for the Stomp put everything a song needs in its three snapshots. Blocks still get FS1-FS3 for when you flip to Stomp mode.

## Songs made for the other pedal

Your library works with both pedals. When you send a song made for the XL to an HX Stomp, the app rebuilds it for the Stomp first: it plans the footswitches again and drops the fourth snapshot (it tells you which). A song made for the Stomp sent to an XL gets the XL's switches and keeps three snapshots. Make a new version of the song if you want four.

## Checking the first send

Follow [device-verification.md](device-verification.md) on a slot you don't mind losing, with these checks instead of the XL's:

- [ ] the preset lands in the slot you picked (labels run 1A, 1B, 1C, 2A ...)
- [ ] all 3 snapshots switch in Snapshot mode, with the right blocks on or off
- [ ] in Stomp mode, FS1-FS3 switch the blocks the app shows on FS 1-3, and the looper (if any) is on FS3
- [ ] the expression pedal sweeps the wah or volume block
