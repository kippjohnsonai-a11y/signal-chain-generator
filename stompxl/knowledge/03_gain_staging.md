# Gain staging and levels

- Aim for unity: bypassing/engaging a block should not jump the volume except where intended (a solo boost of ~+2-3 dB).
- Amp Drive vs Channel Volume: in the rare snapshot that must lower Drive (see snapshots), raise Channel Volume with it so snapshots stay level-matched. Keep Master around 5-7/10 on non-master-volume amp models for realistic power-amp behavior.
- Drives in front of an already-driven amp: low Gain, higher Level. Stacking two high-gain drives into a high-gain amp becomes fizz and mud.
- Helix amps are touch-sensitive and louder at high Drive; high-gain amps rarely need Drive above 6-7/10.
- Reverb Mix 10-25% for most rock parts; 30-50% for ambient. Delay Mix 15-30% for leads, 8-15% for rhythm slapback.
- Presence and Treble interact strongly with pickup brightness; Bass above 6 on high gain amps usually gets flubby, add low cut instead.
- Parameter values in this app's catalog are normalized: knob-style parameters run 0-1 where 0.5 = "5" on the device. Frequencies (LowCut/HighCut) are in Hz, levels in dB.


# Noise gate (the Input block's built-in gate)
- It costs no block. Set it per song: threshold in dB, where a higher (less negative) threshold gates harder.
- Clean, ambient, volume-swell or fingerpicked songs: very gentle, around -70 to -65 dB, so note tails and swells don't get chopped. (With the gate setting "auto", turn it off for these.)
- Crunch / classic rock: around -62 to -58 dB.
- High gain / metal: around -55 to -48 dB. Single coils (Strat) hum more, so go 4-6 dB higher than for humbuckers (Les Paul) at the same gain.
- Too high a threshold cuts off sustained notes and the ends of chords. If the player says notes "die" or "get chopped", lower the threshold by 6 dB; if they say it's "noisy between songs/riffs", raise it by 4-6 dB.
- For very high gain with a boost, an extra 'Noise Gate' or 'Horizon Gate' block after the drives can tighten palm mutes, but it costs a block; the input gate is usually enough.
