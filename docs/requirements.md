# Requirements and roadmap

## Decisions
| Topic | Decision |
| --- | --- |
| Base | Fork/vendor [HelixGen](https://github.com/ORogers/HelixGen) (Apache-2.0, Python) for the catalog, `.hlx` generation, validation and USB upload |
| App | Local web app on the Mac (`uv run stompxl` → browser) |
| AI | Claude API (newest Opus, now `claude-opus-5-5`, adaptive thinking, structured JSON outputs) |
| Pedal | HX Stomp XL, latest firmware, Apple Silicon Mac |
| Guitars | Fender Stratocaster (SSS), Gibson Les Paul (HH), with more addable |
| Rig | Varies: pick headphones/FRFR, front of amp, or 4-cable method each time |
| Wah / EXP / looper | Per-preset toggles; external expression pedal; Stomp XL looper block |
| Slot | Chosen in the app on each push (defaults to the last one used) |
| Snapshots | 4, one per song section |
| IRs | None; stock cabs only |

## Must-haves (built in v0.1)
- [x] Song text → signal chain + parameters + 4 section snapshots
- [x] Guitar library; settings tailored to the chosen guitar
- [x] Output rig profiles (full / front of amp / 4CM) with rules enforced on the chain
- [x] Wah, expression and looper toggles, plus Claude's recommendation for each
- [x] Feedback tweaks ("too muddy") → new version, with the changed values listed
- [x] Standing instructions (all guitars or one guitar); any tweak can be saved as one
- [x] Song library with a version tree: go back to any version, favorite, fork to another guitar/rig/toggles
- [x] Best-practices knowledge library feeding every prompt
- [x] `.hlx` download for HX Edit import
- [x] USB push to a chosen slot, backing up the slot first

## Open items
- [ ] **Verify on hardware**: first USB push to the XL ([device-verification.md](device-verification.md))
- [ ] Confirm the expression controller numbering on a Stomp XL export
- [ ] Send expression-pedal assignments over USB (today they're only in the `.hlx`)
- [ ] Footswitch layout tuned for the XL's 8 switches (today: wah on FS 1, then up to 3 auto-assigned)
- [ ] Restore a backed-up slot from the app

## Ideas
- Pull an existing preset off the pedal and tweak it with feedback
- Setlists: order songs and push them into consecutive slots
- Side-by-side comparison of two versions
- Package as a double-clickable `.app`
