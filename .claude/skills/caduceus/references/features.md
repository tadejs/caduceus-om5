# Added features

Source: README feature table, CHANGELOG, WORKLOG, `docs/`. Status columns: E-P7 from the README (camera-tested = [C]);
OM-3 is the outside port's own claim [R]; OM-5 is the state on branch `om5-port`. Key sites are E-P7 addresses
(base `0x60020020`) unless noted; do not reuse them on another body, relocate them (`references/methodology.md`).

"Patch" is the folder under `patches/`; the matching doc is in `docs/`.

| Feature | What it does | Patch / doc | Key E-P7 sites | E-P7 | OM-3 | OM-5 |
|---|---|---|---|---|---|---|
| Film simulations | 29 colour looks in `EP7MOD/LOOKS.BIN`, one per COLOR slot, name shown in Picture Mode | `film_menu`, `film_menu_v2`; `FILM_SIMS.md`, `FILM_LIBRARY.md` | menu entries kind 5 (runtime text pointer); marker cells in section 6 tail | [C] | has looks [R] | not started (needs COLOR front end) |
| On-camera look editor | COLOR wheel gains pages Film, Saturation, Hue, Light, Extras (INFO cycles) | `look_editor`; `LOOK_EDITOR.md`, `LOOK_PICKER.md` | editor descriptor table `0x61186ba4`, records `0x61185be8`, Up `0x60e20e0a` | [C] | [R] | missing: no editor records or descriptor; wheel view producer `0x60a34cb5` exists |
| Looks on SD | `<MOD>/BUNDLES.BIN` loaded at first half-press, edits save back with `.BAK` | `sd_bundles`, `_v2`, `_v3`; `SD_BUNDLES*.md` | hook `0x602c805f` (HLS_EVENT task), loader 659 B in section 3 tail | [C] | `GUIDES.BIN` card-code idea [R] | not started (plan C5 probe first) |
| Tone per look | Highlights/Midtones/Shadows plus Blacks/Whites (curve ends) | `tone_ends`; `TONE_ENDS.md` | LUT builder `0x603b0a70` | v0.20 | [R] | builder `0x603e3768`, 11/123 chunks only |
| LCD colour fix | Rear screen green/magenta trim, Monitor Adjust slider | `lcd_tint`; verifier `ep7_verify_lcd_tint.py` | panel routine `0x60184360`, 0xd9 command bytes | v0.20 | - | 4/4 sites found, not ported |
| Extras per profile | Bloom/clarity, WB shift, Art Effects (Star Light, Pin Hole) | `bloom`, `wb`, `lv_art`; `BLOOM_HALATION.md`, `CLARITY.md`, `WB_PATCH.md` | WB stub `0x6055e700`, bloom block section 2 | [C] | [R] | WB/bloom sites absent or ambiguous |
| Film grain | Grain Off/Low/Med/High per COLOR profile, added to luma | `grain`, `grain_boost`, `grain_c`, `grain_cpu`, `grain_fast`; `GRAIN_PORT.md` | wl_hook, grain_mod in section 1 | v0.27 (finer level [C]) | method comes from OM-3 port [R] | not started |
| Sharpening off | Extras sector 10 Sharp Camera/Off per profile | `sharp_off`; `SHARPENING_OFF.md` | - | v0.28, not controlled-tested | - | not started |
| NR per profile | Camera NR level per COLOR profile (Extras 9) | in `grain`/bundles; `PROFILE_INDEX_WB.md` | - | [C] | same design [R] | not started |
| Lens vignette correction off | Look turns off shading correction (Extras 8) | `VIGNETTE.md` (also `sharp_off`-style per-profile flag) | - | v0.25, awaiting reports | maps to Shading | not started |
| Mono film presets | Pan F, Acros, Tri-X, HP5 and more, from OM's mono settings | `mono_presets`, `mono_colour`; `MONO_PRESETS.md` | MONO colour hook `0x603b02bf`, code in section 7 | [C] | left out of panel | MONO 1-3 settings and labels survive |
| Live-view preview | Hue, lightness, bloom shown in live view | `lv_bundles`, `lv_art_refresh`; `LV_BUNDLES.md` | LV handler `0x60321d3b` | [C] | Art Effects in LV [R] | LV handler `0x603511f4` (50/63 chunks) |
| True log video | Hidden log curve with movie mode Muted | `video_log`; `VIDEO_LOG.md` | +925 hook | [C] | - | not found; OM-5 has official OM-Log400 (check need) |
| XPan 65:24 and 21:9 | Panorama/Wide in menu and SCP, live view and playback framing | `aspect`, `aspect_fix`, `aspect_icons`, `aspect_scp`; `ASPECT.md` | size tables, rects, validator list for setting 270 | [C], fixes in v0.29 | - | 86 of 150 edits found after expansion; not ported |
| Movie colour | Movie Vivid becomes its own profile in `MOVIE.BIN` | `v026_movie`; `MOVIE_COLOUR.md` | - | [C], long clips untested | - | not started |
| Vertical video | Rotation recorded in clips | `v026_rot`; `VIDEO_ROTATION.md` | - | [C] (ffmpeg flags) | - | not started |
| Button functions | More REC/Fn functions, settable right/down arrows | `buttons`, `btn_arrows`, `omc_buttons`; `BUTTONS.md` | Button Function validator data | v0.29 | - | not started |
| Recording limit | 29 min / 2 h caps raised to 9 h | `rec_limit` | `0x60319157` (mov 1740), `0x6031915c` (mov 7200) | [C] | - | no matching function; locate by hand |
| Firmware label | Setup > Firmware shows `Caduceus: <version>` | `fw_version` (E-P7), `om5_fw_version` (OM-5); `PORT_OM5.md` | setter calls `0x60d7f271`, `0x60d7f2cd`; OM-5 `0x60dc58f6`, `0x60dc5952`, descriptor `0x61241c7c` | [C] | - | built and om-emu verified (C3), not on a camera |
| Aperture ring | Panasonic ring on the lens drives aperture, runs from power-on | `aperture_ring`; `APERTURE_RING.md`, `BOOTLOOP_T3.md` | dispatcher `0x601f9df7`, parser `0x601f5ed7`, AE `0x60427ae7` | parked after bootloop suspicion; excluded from builds | - | planned last, alone in its flash |

## Pieces that are not features but matter

- **Control Panel / webapp** (`webapp/`): look editor and share codes; export targets per camera. OM-3 translation
  (hue/light/WB limited to +-5, contrast/sharpness +-2, vignette becomes Shading, halation becomes glow) in
  `docs/OM3_NOTES.md`. An OM-5 target is planned (`webapp/src/targets/om5.ts`, 3 COLOR slots).
- **Looks format**: shared across cameras; keep wider values until the user moves the control.
- **OM-5 look route** (`docs/PORT_OM5.md`): A revive COLOR 1-3 (settings 922-957, converter `0x6041a5e7`, selector
  `0x603e3175`, builder `0x603e2f1c`, capture fill `0x614cf122` all survive; mode-11 record template, Picture Mode
  rows, parameter handlers 54-59 and the editor are missing) or B host looks on Color Creator. Decided by a small
  camera experiment. A row added alone is not a safe test.
- **Known risks** to carry between bodies: hue direction differs between E-P7 and OM-3 results (open question in
  `OM3_NOTES.md`); removed Blur Art Effect could restart the camera; SD loader v2 hooks the first half-press (possible
  event-queue deadlock).
