# NAM amp and cab

Two output rigs hand the amp to [Neural Amp Modeler](https://github.com/sdatkinson/neural-amp-modeler) (NAM). The Stomp XL handles every other effect. Pick a NAM rig in the **Output rig** list when you create, fork or import a preset.

| | NAM on pedal (custom firmware) | NAM on computer (USB) |
| --- | --- | --- |
| Computer while playing | No | Yes, with a plugin host or DAW running |
| Captures | A2 Lite, 48 kHz, converted to a .wav in an IR slot | Any NAM capture, plus any cab IR |
| DSP left for effects | About 23% less (the NAM block) | Nearly all of it, so a Poly Capo fits easily |
| Latency | Same as a stock amp | About 8 to 10 ms at a 64-sample buffer |
| Getting it onto the pedal | Send to pedal works as usual | Download the .hlx and load it in HX Edit |

## Your captures

In **Settings > NAM captures**, list one capture per line:

```
12 | JCM800 Crunch | Marshall JCM800 2203, amp only
13 | Recto Full Rig | Mesa Rectifier with V30s | cab
- | Twin Clean | Fender Twin, on the computer only
```

The fields are: the IR slot that holds the converted capture (use `-` for a capture that's only on the computer), a name, what the amp is, and `cab` if the capture includes its cabinet. Claude picks a capture for each song from this list. NAM on pedal only offers captures that have a slot. NAM on computer can also suggest a capture that isn't on your list.

## Getting captures from TONE3000

The app can find, download and convert captures for you, from Settings > **Get captures from TONE3000...** (or `stompxl nam` on the command line).

1. Make a free API key at [tone3000.com](https://www.tone3000.com) > Settings > API Keys. Paste the **publishable** key (`t3k_pub_...`) into the app, never the `t3k_cs_` secret one.
2. Click **Sign in to TONE3000**. Your browser opens TONE3000, then comes back to the app.
3. Click **Install the starter library** for 20 captures in IR slots 101 to 120, **Install the full-rig pack** for 15 more in slots 121 to 128 and 44 to 50, and **Install the extended pack** for 50 more in slots 51 to 100 (the pedal has 128 IR slots; all three are described below), or search and add one at a time: paste a tone link, give it a slot and a short name, and optionally a few words from the name of the model in the pack you want.

For each capture, the app:

- downloads its A2 `.nam` into `~/Music/StompXL NAM/nam`, for the NAM plugin on the computer;
- if it has a slot, converts it with the firmware's own converter into `~/Music/StompXL NAM/HX IRs/<slot> <name>.wav`, for NAM A2 Lite on the pedal;
- lists it in Settings > NAM captures, with its slot, and whether it includes a cab.

**The one step left for you:** HX Edit has no way for another program to import IRs, so open its Impulses list and drag each `.wav` onto the slot its name starts with. HX Edit names an IR after its file and keeps 16 characters, which is why the names are short.

Captures belong to the people who made them; TONE3000 hosts them. Each person signs in with their own TONE3000 key, the files are downloaded to their own computer, and this repository lists only each capture's TONE3000 number and a short description, never the files. Don't commit captures or share the converted files; check a capture's license on TONE3000 before passing it on. Use of the TONE3000 API is covered by TONE3000's [API terms](https://www.tone3000.com/api) and [terms of service](https://www.tone3000.com/terms).

The converter is [hx-stomp-nam-lite's](https://github.com/nocaseguy/hx-stomp-nam-lite) `tools/nam2ir.py` (GPL-3.0). The app downloads it from GitHub on first use, at the commit that matches the firmware, and runs it as a separate program.

### Already on your pedal? (a new computer)

Each computer keeps its own list of captures, so a new computer, or a fresh install, starts with none even when the pedal already has them. Generating for NAM on the pedal then asks **Which NAMs are on your pedal?** (also in Settings > NAM captures): tick the packs that are on it (starter, full-rig, extended) and the app lists them, with nothing downloaded and nothing on the pedal changed. Not sure? **Check my pedal** reads your presets (read-only) and ticks the packs whose slots they use; a slot that isn't in a pack is named so you can add it by hand. The pedal can't yet be asked for its IR list itself, so this goes by what your presets use. Moving your library with More > Back up or move your library carries the list too.

The command line does the same: `stompxl nam starter`, `stompxl nam full-rig`, `stompxl nam find fender twin`, and `stompxl nam add LINK --slot 30 --name "Twin Edge" --model "edge"`. Sign in from the app first.

### The starter library

| Slot | Capture | Suits |
|---|---|---|
| 101 | Fender Deluxe Reverb, clean (full rig) | Soul, Stevie Wonder, country, Mayer |
| 102 | Fender '65 Twin Reverb (full rig) | Funk, Earth Wind & Fire, late Beatles, Jack White |
| 103 | Roland JC-120 (full rig) | Funk and new wave cleans |
| 104 | 1960 Fender Tweed Deluxe (full rig) | Blues, garage |
| 105 | Fender '65 Super Reverb (amp) | SRV, blues |
| 106 | Vox AC30 Top Boost (full rig) | Beatles, The Killers, Foo Fighters cleans |
| 107 | Vox AC30 pushed (amp) | Brian May, U2, Britpop |
| 108 | Marshall JTM45 (amp) | Early Clapton, Frusciante cleans |
| 109 | Marshall Plexi (amp) | Hendrix, Zeppelin, AC/DC, RHCP |
| 110 | 1980 Marshall JMP (full rig) | Punk, My Chemical Romance |
| 111 | Marshall JCM800 (full rig) | Slash, 80s rock |
| 112 | Hiwatt DR103 (amp) | The Who, Pink Floyd |
| 113 | Silvertone Twin Twelve (full rig) | Jack White |
| 114 | Mesa Dual Rectifier (full rig) | Foo Fighters, MCR |
| 115 | Mesa Mark IV lead (amp) | Metallica, Petrucci |
| 116 | Peavey 6505 lead (full rig) | Metal, metalcore |
| 117 | Friedman BE-100 (amp) | Modern hard rock |
| 118 | Soldano SLO-100 (amp) | High gain lead |
| 119 | Orange OR60 (amp) | Stoner, indie |
| 120 | Dumble ODS (amp) | John Mayer, Robben Ford |

The tones were picked on 2026-11-07 by downloads, favorites and comments. `stompxl/nam_starter.json` has their TONE3000 numbers.

### The full-rig pack

Every capture here includes its cab, so a NAM-on-pedal preset needs no cab block. That saves the DSP a cab costs, which is what lets NAM sit beside a Poly Capo. Tick **Full-rig captures only** when you generate a song and the app offers Claude only captures like these (the starter library's full rigs count too).

| Slot | Capture | Sound |
|---|---|---|
| 121 | Plexi 1959 | Marshall 1959 Super Lead 100W, full rig (G12, SM57 + condenser); classic rock crunch, Hendrix, Zeppelin, AC/DC |
| 122 | Plexi Crunch | Marshall 1987X hot-rodded crunch into a 1960 4x12 (Greenback + V30); hard rock, 80s rhythm |
| 123 | JTM45 Crunch | 1964 Marshall JTM-45 into a 1966 4x12 with G12 Alnicos; blues and early rock, Clapton, Cream |
| 124 | Hiwatt Rig | 1972 Hiwatt DR103 clean, Fane speakers, SM57; Gilmour, Townshend, big headroom cleans |
| 125 | Mark IIC+ | Mesa Boogie Mark IIC+ full rig; Metallica, Dream Theater, singing lead |
| 126 | Friedman Rig | Friedman BE-100 with a Tube Screamer, Greenback/V30 blend; modern hot-rod Marshall rock |
| 127 | Soldano Rig | Soldano SLO-100 crunch into a Mesa 4x12; 80s/90s rock rhythm and lead, Knopfler's Money for Nothing |
| 128 | Rockerverb | Orange Rockerverb 50 full rig, unboosted; stoner, doom, modern rock |
| 44 | Dumble Clean | 1974 Dumble Overdrive Special #124 clean into a 2x12 with G12Ms; Robben Ford, Mayer, jazz cleans |
| 45 | Dumble Lead | 1974 Dumble Overdrive Special #124 lead channel; SRV, Robben Ford, smooth singing overdrive |
| 46 | Super Lead | Fender '65 Super Reverb pushed to lead, EV speakers; SRV, blues lead |
| 47 | 62 Bassman | 1962 Fender Blonde Bassman crunch, blended 2x12; Chicago blues, roots rock |
| 48 | Matchless DC30 | Matchless DC-30 edge of breakup, Greenbacks; boutique chime, Brit-pop, country rock |
| 49 | Bogner XT | Bogner Ecstasy 101B channel 3; plexi-style crunch to high gain, Steve Stevens, modern rock lead |
| 50 | Princeton | Firebelly PR3512SE (modded Princeton) full rig; small-amp clean to edge, studio country and pop |

They fill the starter library's amp-only gaps (Plexi, JTM45, Hiwatt, Mark, Friedman, Soldano, Orange, Dumble, Super Reverb), picked on 2026-11-09 by downloads and favorites. `stompxl/nam_full_rig.json` has their TONE3000 numbers and the model picked from each pack.

### The extended pack

50 more captures in IR slots 51 to 100, for when the starter library doesn't have the sound: more cleans (Two-Rock, Dumble, Tone King, Magnatone, Lone Star), British chime and crunch (AC15, AC50, Bad Cat, Bluesbreaker, JMP-50, JCM900, JCM2000, JVM), hot-rods (Splawn, Dirty Shirley, Revv), doom (Matamp) and modern high gain (JP-2C, Mark III and V, Rectifiers, Uberschall, 5150s, Diezel, ENGL, Victory, Fortin). A few have a pedal in front, so the drive is part of the capture: a Klon (Two-Rock Klon, Deluxe Klon, JCM800 Klon), an OD808 or TS808 (Recto OD808, JCM900), and a TS808 plus Fuzz Face (EJ Plexi Fuzz). Click **Install the extended pack** in the TONE3000 window, or run `stompxl nam extended`.

| Slot | Capture | Sound |
|---|---|---|
| 51 | Two-Rock SSS | Two-Rock Signature (SSS) clean; John Mayer, glassy modern country and worship cleans |
| 52 | Dumble SSS | Dumble Steel String Singer clean; huge-headroom SRV and Mayer cleans, jazz |
| 53 | Two-Rock Klon (full rig) | Two-Rock CRS and Bad Cat Era 30 together, boosted by a Klon; lush edge-of-breakup lead, worship, blues |
| 54 | Vibrolux (full rig) | Fender Custom Vibrolux Reverb 2x10; sparkly blackface clean, surf, indie, country |
| 55 | Princeton SF (full rig) | 1976 Fender Princeton Reverb silverface; small-amp studio clean, Americana, Jason Isbell |
| 56 | Tone King | Tone King Imperial MKII rhythm channel; warm boutique Fender-style clean to breakup, country, soul |
| 57 | Magnatone (full rig) | 1966 Magnatone M10A crunch; vibrato-era grit, Buddy Holly, Lonnie Mack, roots rock |
| 58 | Lone Star (full rig) | Mesa Lone Star Classic drive channel; smooth boutique overdrive, country rock, blues lead |
| 59 | Deluxe Klon (full rig) | Fender '65 Deluxe Reverb boosted by a Klon; transparent overdrive, blues and alt-country lead |
| 60 | Blues Junior (full rig) | Fender Blues Junior III; the classic pub-gig combo, light crunch, blues, classic covers |
| 61 | Hot Rod Dlx | 2015 Fender Hot Rod Deluxe, vintage channel sweet spot; bar-band clean to crunch, 90s alt |
| 62 | Supro 1605 | Supro 1605R Reverb, gritty; Jimmy Page studio tone, garage and blues rock, The Black Keys |
| 63 | Benson | Benson Chimera edge of breakup; boutique blackface-style clean, indie, worship |
| 64 | Dr Z Ghia | Dr. Z Carmen Ghia 18W; Brad Paisley-style chime and crunch, country, roots rock |
| 65 | AC30 Edge (full rig) | 1964 Vox AC30 Top Boost, edge of breakup; jangle, The Beatles, Tom Petty, R.E.M. |
| 66 | AC15 | Vox AC15 top boost, edge of breakup; chimey Britpop, indie, Radiohead, Oasis cleans |
| 67 | AC50 (full rig) | 1964 Vox AC50 Mk I clean and bright; early British Invasion, The Beatles, The Shadows |
| 68 | Bad Cat Lynx | Bad Cat Lynx; class-A chime to crunch, modern indie, worship |
| 69 | Bluesbreaker (full rig) | Marshall 1962 Bluesbreaker combo; Clapton Beano tone, blues rock, Cream |
| 70 | JMP50 Plexi (full rig) | 1969 Marshall JMP-50 lead plexi crunch, full rig; Led Zeppelin, AC/DC, classic rock rhythm |
| 71 | Super Bass (full rig) | 1967 Marshall Super Bass crunch; Hendrix, Sabbath, fat 60s plexi grind |
| 72 | Brown Sound | JTM50-style amp dialled for Van Halen I; brown sound, 80s rock rhythm and lead |
| 73 | EJ Plexi Fuzz (full rig) | Plexi with a TS808 and Fuzz Face in front; Eric Johnson violin lead, fuzzy classic rock solos |
| 74 | JCM900 | Marshall JCM900 4100 with a TS808; 90s hard rock and grunge, Slash, Zakk Wylde, Pearl Jam |
| 75 | Twin JBL (full rig) | 1976 Fender Twin Reverb silverface with JBL D120Fs, full rig; huge glassy clean, funk, country, Steely Dan |
| 76 | JCM2000 DSL | Marshall JCM2000 DSL crunch; 2000s rock, Joe Bonamassa, Kirk Hammett rhythm, pop punk |
| 77 | JVM410 | Marshall JVM410H OD1 channel; modern Marshall rock, Satriani, Foo Fighters, Nickelback |
| 78 | JCM800 Klon | JCM800 2203X pushed by a Klon; hard rock and 80s metal rhythm, Guns N' Roses, AC/DC heavy |
| 79 | Quick Rod | Splawn Quick Rod; hot-rodded Marshall, Nickelback, modern hard rock and country rock |
| 80 | Dirty Shirley | Friedman Dirty Shirley Mini crunch; boosted plexi grind, classic and alt rock, Dave Friedman |
| 81 | Nu Metal Duo (full rig) | Mesa Dual Rectifier and Marshall JCM800 blended, full rig; Linkin Park Meteora, 2000s nu-metal and alt metal |
| 82 | Orange AD30 | Orange AD30HTC crunch; British indie and stoner rock, Arctic Monkeys, Oasis |
| 83 | TriAmp (full rig) | Hughes & Kettner TriAmp MK II full rig; 90s/2000s rock, Alex Lifeson, versatile crunch |
| 84 | Revv G120 | Revv Generator 120 MKIII EL34 red channel; modern rock and metal, tight and articulate |
| 85 | Matamp GT1 | Matamp GT1 mkIII; doom and stoner fuzz-like grind, Sleep, Electric Wizard |
| 86 | Bogner Shiva (full rig) | Bogner Shiva channel 2 boosted, full rig; boutique hard rock lead, Mark Tremonti |
| 87 | Recto OD808 (full rig) | Mesa Dual Rectifier Rev F red channel with an OD808, full rig; nu-metal and metalcore, Korn, Metallica Black Album |
| 88 | Triple Recto (full rig) | Mesa Triple Rectifier red channel, boosted, full rig; modern metal, Mastodon, Linkin Park |
| 89 | JP-2C | Mesa JP-2C channel 3; John Petrucci, prog metal rhythm and lead |
| 90 | Mark V (full rig) | Mesa Mark V lead, full rig; singing Boogie lead, Petrucci, Santana-style sustain |
| 91 | Mark III | Mesa Mark III high gain; 80s thrash, Metallica Master of Puppets, Dream Theater |
| 92 | Uberschall | Bogner Uberschall; crushing modern metal, Tremonti, djent and drop tunings |
| 93 | 5150 III | EVH 5150 III 6L6 red channel, boosted; modern Van Halen, metalcore, hard rock |
| 94 | 5150 Block (full rig) | 1992 Peavey 5150 block letter, full rig; 90s metal, Killswitch Engage, Trivium |
| 95 | Diezel VH4 | Diezel VH4 channel 3; Metallica St. Anger to Tool, tight German high gain |
| 96 | Herbert | Diezel Herbert channel 2 crunch; Tool, Adam Jones, heavy rock rhythm |
| 97 | ENGL Savage | ENGL Savage 120 lead; European metal, Children of Bodom, power and melodic metal |
| 98 | Victory MKX | Victory MKX; modern British high gain, Rabea Massaad, djent and progressive rock |
| 99 | Evil Pumpkin | Fortin Evil Pumpkin; Jason Richardson, tech metal and djent rhythm |
| 100 | Meshuggah (full rig) | Fortin Meshuggah plexi, full rig; extended-range djent, Meshuggah, Periphery |

They were picked on 2026-11-28 by downloads, favorites and comments on TONE3000, one or two per amp family, skipping amps the other packs already cover. Each has A2 models; where a tone holds several, `stompxl/nam_extended.json` names the one used.

## NAM on the pedal

1. Install the NAM A2 Lite firmware from [nocaseguy/hx-stomp-nam-lite](https://github.com/nocaseguy/hx-stomp-nam-lite). You can try it from RAM first, which doesn't install anything. It's unofficial, third-party firmware: not from or supported by Line 6, it may void your warranty, and you install it at your own risk. Read its Install and Recovery sections first. This app never ships or installs firmware; it only writes presets for it.
2. Get captures as above, or convert a `.nam` A2 Lite capture yourself on the firmware's setup page. Import each `.wav` into its User IR slot in HX Edit.
3. The app writes NAM as a **Xenomorph Fuzz** block, which is the identity the firmware gives it. Its Min Freq knob is the IR slot, and the tone stack starts flat. On stock firmware this block plays a real Xenomorph Fuzz, so only send NAM presets to a pedal running the NAM firmware.

## NAM on the computer

The preset sandwiches NAM between the pedal's two paths:

- **Guitar into the Return L jack.** A Return block at the head of path B brings it in.
- **Path B** holds the gate, comp, wah and drives, and goes out of the Send output to the computer over USB.
- **The computer** runs the NAM plugin (Mac: the **Gateway** installer at [neuralampmodeler.com/users](https://www.neuralampmodeler.com/users); GitHub only has source code) and a cab IR on one track at 48 kHz, with a 64-sample buffer (128 if it crackles). The track's input is the Stomp's send channel and its output is USB 5/6. Use the Line 6 ASIO driver on Windows; a Mac needs no driver.
- **Path A** starts at the Input block, which is set to USB 5/6, so NAM comes back in there. It adds modulation, delay, reverb and the looper, then goes to the main outputs.

**One-time setup.** No public source pins down the numbers a preset uses for the "USB 5/6" input and the "Send L/R" output, so the app learns them from you once:

1. In HX Edit, make any preset with the Input block on **USB 5/6** and path B's Output on **Send L/R**. Export it as an .hlx file.
2. Run `uv run stompxl learn-nam-usb path/to/that.hlx`.

Until you do this, NAM-on-computer presets come out with a note telling you to set those two dropdowns by hand in HX Edit.

The USB push only edits path A, so load these presets with **Download .hlx** and drag the file into HX Edit.

### Switching captures when you change presets

Yes, you can set this up so the computer follows the pedal. Turn on **Global Settings > MIDI/Tempo > MIDI Over USB** and **MIDI PC Send**. The Stomp XL then sends a MIDI program change over USB every time you pick a preset: slot 1A is PC 0, 1B is PC 1, and so on, up to 32D, which is PC 127.

The NAM plugin can't change captures from MIDI by itself, so let a host do it:

- **MainStage** (Mac, $30): make one patch per preset, each with NAM loading that preset's capture, and set the patch's program change to the preset's slot number.
- **Gig Performer**: works the same way, with a rackspace per program change.
- **Reaper**: use [ReaLearn](https://www.helgoboss.org/projects/realearn/) or SWS snapshots to load a NAM preset on each program change.

The version page names the capture for each preset, so the mapping is the slot you put it in and the capture listed there.
