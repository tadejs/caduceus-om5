# Conventions and hardware

Sources: `tools/bodies.py`, `docs/BODIES.md`, `docs/PORT_OM5.md`, `docs/SD_UPDATE.md`, `docs/CODE_SPACE_MAP.md`,
`docs/OM3_NOTES.md`. Tags: [V] verified, [I] inference, [C] camera, [R] reported.

## Bodies

| Body | Firmware file | Model | Runtime base (decoded offset 0) | Update name on card | Card mod folder | Compression | Status |
|---|---|---|---|---|---|---|---|
| E-P7 1.1 | `OLY_E_142_1100_0000_0000.BIN`, 111 017 984 B, SHA `9d161012...95eaeb` | 1420 | `0x60020020` | `DCOLYMP/E1429999.BIN` | `EP7MOD` | none | Full feature set, camera-tested [C] |
| OM-3 | (outside repo) | 1470 [I, from the update name] | `0x60021020` | `DCOLYMP/E1479999.BIN` | `OM3MOD` | see its repo | Working port by androidfanman ([caduceus-om3](https://github.com/androidfanman/caduceus-om3)) [R] |
| OM-5 1.2 | `OLY_E_144_1200_0000_0000.BIN`, 92 274 688 B, SHA `a0c91f3a...3d11` | 1440 | `0x60021020` | `DCOLYMP/E1449999.BIN` [I] | `OM5MOD` | LZ4 sections 5 and 6 | Analysed; camera ladder C1-C5 not yet run |
| OM-5 Mark II | `OLY_E_148_1100_0000_0000.BIN`, 90 832 896 B | 1480 | not worked out | `E1489999.BIN` [I] | | | Not covered |

Notes:
- The update name is `E` + model (4 digits) + `9999` + `.BIN`. The updater picks the largest hex value in chars 4-7
  of matching `E<model>????BIN` names, so `9999` always wins over older files on the card.
- The OM-5 base equals the OM-3's, not the E-P7's. OM-5 runtime address = decoded offset + `0x60021020`.
- Other bodies (E-M10 IV, E-M5 III, E-M1 III, PEN-F, OM-1) are in `docs/BODIES.md`; only the E-P7 and OM-3 have
  camera-tested ports. PEN-F is served by fishcu's own HSL mod.
- A body descriptor is `Body(key, label, fw, stock_name, stock_sha, stock_size, model, hard_code, model_id, base,
  card_name, mod_dir, compressed)` in `tools/bodies.py`. Add a body by adding a record there and mirroring the minimal
  copy in `caduceus.py`.

## Container (update file)

Components, each with a 32-byte header (`OE` signature, model, address, length, build/version, kind) and a 32-byte
trailer with a checksum word. Kinds: 0x100 main (USR), 0x101 OSD resource, 0x102 RESCOM, 0x106, 0x10e PIC (boot
sub-processor "LUKE"). The payload is scrambled per 16-byte group (`PERM = [1,3,0,2,6,4,7,5,11,10,9,8,13,14,12,15]`,
`decoded[i] = ~enc[PERM[i]]`; see `tools/om5_sd_update_probe.py`). Main-only packages (kind 0x100 alone) are valid
update files and are what `build_package.py` writes; the OSD can also be updated alone (used as the E-P7 probe).

| Component | E-P7 1.1 | OM-5 1.2 |
|---|---|---|
| Whole image | 111 017 984 B | 92 274 688 B |
| Main 0x100 | 26 214 336 B at `0x406a0020` | 24 903 616 B at `0x40780020` |
| OSD 0x101 | 26 214 336 B | 14 024 640 B |

## OM-5 compressed spans and section table (BCS)

LZ4 "olycompress": 16-byte magic, u32 packed size at +16, u32 raw size at +20, 8 zero bytes, one LZ4 block from +32.
Container offsets in main: `0x825fe0` (section 5, runtime `0x60847000`-`0x60cbe800`) and `0xc9d7e0` (section 6,
`0x60cbe800`-`0x61343800`). Expanded size equals the span size, so the run-time image is the same size as the file [V].
`tools/olycompress.py` is our own LZ4 decoder and encoder; `build_package.py` re-encodes any span with edits (and both
with `--recompress-all`). The bootloader accepts our encoding: shown in om-emu (C2 boots, 17 frames equal stock),
not yet on a camera.

BCS table at main file `0x6fd8` (`"BCS\0"`, entries from `0x6fdc`): `{u32 dest, u32 flash src, u32 flag}`, flag 1 =
compressed. 10 entries on the OM-5, 9 on the E-P7 (no flags). Free code space is the FF tail of each section **up to its loaded end** (only the copied part exists in RAM; loaded ends by index 0..9: `0x6023e800 0x605a7800 0x60620800 0x6082b000 0x60846000 0x60cb0000 0x6133b000 0x613f3000 0x615ea000 0x6169c000`; the span tails such as section 9 0x13d494 are NOT usable). Usable: section 1 `0x605a76c8` (0x138 B), section 8 `0x615e9848` (0x7b8), section 2 `0x60620580`, section 4 `0x608458d8`; `patches/om5_common` enforces it. Full table: `docs/PORT_OM5.md`. For the E-P7 see `docs/CODE_SPACE_MAP.md` (tails in sections 1, 2, 3, 6).

## Section load order (the OM-3 bootloop lesson)

Sections are copied into RAM by a loader at boot. On the E-P7 the order is 1, 7, 2, 3, 4, 5, 6, 8, 9 [V]; on the OM-5 `sec_copy` `0x6004635c` is called for ids 1, 8, 2, 3, 4, 5, 6, 7, 9, 10 = table indices 0, 7, 1, 2, 3, 4, 5, 6, 8, 9 [V dis, I for run order], setting bit n-1 of `0xa2693fa0` after each copy. An early OM-3 test bootlooped, likely because
a hook in section 2 called code in section 8 which was not loaded yet; code in sections 1-5 was safe on that
pipeline, and reflashing a working build recovered the camera. Rule: code that runs at power-on must only call into
sections loaded before it, and a hook placed in section N should keep its helper code in the same or an earlier one.
Re-read this before any hook that runs from power-on, such as the aperture ring (the main suspect in the E-P7 test 3
bootloop, `docs/BOOTLOOP_T3.md`).

## SD updater facts (`docs/SD_UPDATE.md`; OM-5: `tools/om5_sd_update_probe.py`)

- Task `FIRMUP_CLI` looks in the `DCOLYMP` folder for `E<model>????BIN` (and other letters for other component
  families). It posts the file to `FIRMUP_SRV`, whose validator is `0x60c964c1` on the E-P7 and `0x60ce6846` on the
  OM-5 (same code but for the model constant).
- It checks: the `OE` signature, the **model bytes** (E-P7 `1420`, OM-5 `1440`; a PEN-F or other model is rejected),
  the header/trailer match, and the checksum. **No version check** [V emulator]: same version, a downgrade, build
  `0x0000` and `0xffff` are all accepted. It has no LZ4 awareness.
- All components in the file are validated before any is written; a bad one writes nothing [V]. Write order: PIC
  (to the sub-processor), RESCOM, OSD, then main last. A failure part-way aborts the remaining writes, there is no
  rollback.
- Main (kind 0x100, 25 MiB) is erased and reprogrammed in full even when identical, so a stock-on-stock update is not
  a near no-op. Expect a long steady-light phase (1-2 minutes with a full battery).
- Status light: steady during the write, then **blinking about twice a second = success** [C, E-P7 2026-11-01]. An
  early inference that a fast blink meant success was wrong; a rejected file blinks fast with no steady phase and
  writes nothing.
- Start trigger: hold OK and switch the camera on, keep holding until the light comes on. Proven on the E-P7 and
  OM-3 [C]; NOT proven on the OM-5 [I]. The trigger lives in code we cannot see; if the camera just boots normally,
  nothing was written.
- Recovery: the updater is reachable even from a boot loop (E-P7 test 3: battery out, hold OK and switch on, flash a
  stock main restore). Proven on the E-P7 only, once; a fault early enough in boot might stop it on other bodies.
  OM Workspace over USB is the official fallback (the USB opcodes are the same as PEN-F's; whether OM Workspace
  accepts modified files is [I]).
- Offline checks on the OM-5: run `tools/om5_sd_update_probe.py` under WSL with `--patched` and
  `--restore`. Model 1440 accepted, E-P7 1420 and PEN-F 1220 rejected; stock file, recompressed package and restore
  package are accepted, with main written at the same address and length.

## Conventions

- Version label: the firmware screen shows `Caduceus: 0.29a` (E-P7) via `patches/fw_version`; on the OM-5
  `Caduceus : 0.1` via `patches/om5_fw_version`.
- Releases: `releases/<tag>/edits.json` and `SHA256SUMS`; `caduceus.py build --release <tag>` must reproduce it.
- Card files: looks in `<MOD>/BUNDLES.BIN`, `LOOKS.BIN`, `MOVIE.BIN`, loaded at the first half-press.
- Never ship OM bytes. Users supply their own stock file; the builder checks its SHA.
