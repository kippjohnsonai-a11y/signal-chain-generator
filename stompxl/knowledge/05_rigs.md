# Output rig profiles

## Full rig: headphones / FRFR / PA / studio monitors (amp + cab modelling)
- Every preset needs an amp and a cab (Amp+Cab block). Use post-cab EQ to tame mud/fizz.
- Output block set to Line level. Keep overall level consistent across presets.

## Front of a real guitar amp (no amp or cab models)
- No amp, cab, or preamp models. The real amp supplies the core tone; the {{PEDAL}} supplies dynamics, drives, modulation, delay, reverb, EQ.
- Set drives as the gain stages: a clean amp needs drives with more Gain; a crunch amp needs drives as boosts (low Gain, high Level).
- Time-based effects in front of a distorted amp get messy - keep delay/reverb mixes lower, or suggest the 4-cable method.
- Output at Instrument level.

## 4-cable method (4CM) with a real amp's effects loop
- Place drives, wah, compressor BEFORE the FX Loop block; modulation, delay and reverb AFTER it. The FX Loop block represents the amp's preamp, so there are no amp/cab models.
- The FX Loop block uses one of the 8 blocks.
- Snapshots can switch amp-channel-like sounds only via drives; mention using the amp's own channel switch if needed.

## NAM on the pedal (NAM A2 Lite firmware)
- A Neural Amp Modeler capture is the amp. The app inserts the NAM block (it shows as Xenomorph Fuzz in HX Edit) before the cab or the first post-amp block; never add an amp or preamp model.
- Pick the capture whose amp and gain range is closest to the recording. A capture is a fixed snapshot of one amp at one setting: get more or less gain from the drives in front of it, not from the amp.
- An amp-only capture needs exactly one cab block after it; a full-rig capture (amp and cab) needs none.
- The NAM block costs about 23% of the DSP, less than a stock amp and cab together, so there is usually room for a Poly Capo too.

## NAM on the computer (USB)
- NAM and a cab IR run on the computer between the pedal's two halves: pre-amp blocks (gate, comp, wah, drives, Poly Capo) go out to the computer; modulation, delay, reverb, volume and looper come after NAM returns. List the pre-amp blocks first.
- No amp, preamp or cab blocks on the pedal. The Input block's gate sits after NAM, so use a Noise Gate block before the drives if the song needs gating.
- The pedal's DSP is almost all free for effects, which is the reason to choose this rig for heavy blocks like the Poly Capo.
