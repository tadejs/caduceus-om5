# Porting Caduceus to the OM-5 (original, 2022): first-glance feasibility

> **Static read only (2026-11-07).** Nothing here has been built or run on a camera. A signature or settings match says
> the code looks alike; it does not say an edit is correct or safe on this body. See [BODIES.md](BODIES.md) for the
> test ladder every new body goes through.

Companions: [PORT_EM5III.md](PORT_EM5III.md), [PORT_EM10IV.md](PORT_EM10IV.md), [OM3_NOTES.md](OM3_NOTES.md).

## Sources

- OM-5 `OLY_E_144_1200_0000_0000.BIN` (1.2, latest in OM's catalog, model id 1440, 92 274 688 B,
  SHA-256 `a0c91f3a…3d11`, the same file fishcu's `oly_fw_analysis` pins). Kept in `stock_firmware/om5/`, never committed.
- E-P7 1.1 (`OLY_E_142_1100_0000_0000.BIN`).
- Decoded with `external/oly_fw_analysis/tools/oly_container.py` into `analysis/om5_1200/` (round trip byte-identical,
  every checksum valid).
- The OM-5 Mark II is a different file (`OLY_E_148_1100_0000_0000.BIN`, model 1480, 90 832 896 B) and is not covered here.

## Container

| Component | E-P7 1.1 | OM-5 1.2 |
|---|---|---|
| Whole image | 111 017 984 B | 92 274 688 B |
| Main 0x100 | 26 214 336 B at `0x406a0020` | 24 903 616 B at `0x40780020` |
| OSD resource 0x101 | 26 214 336 B | 14 024 640 B |
| 0x106 | 45 219 776 B | 45 744 064 B |
| 0x102 | 13 107 136 B | 7 339 968 B |

Same component kinds and checksum scheme, so the packager should work once the model bytes (`1440`) are set.
The SD update file is probably **`DCOLYMP/E1449999.BIN`** by analogy with `E1429999.BIN` and the OM-3's `E1479999.BIN` [I].
Confirm with a stock file on a body before telling anyone to flash.

## CPU address base: `0x60021020`, not `0x60020020`

With the E-P7's base the OM-5 validator's list pointers land inside other records. With **`0x60021020`** (the OM-3's
base) every list decodes cleanly (e.g. the saturation sectors read −5..5). So OM-5 runtime address = decoded offset +
`0x60021020`. `oly_container.py --code-base 0x60020020` was used for decoding; it only affects the analysis address
labels, not the decoded bytes. `port_sigmatch.py`'s "same address" column is meaningless for this body because of that
base difference (its byte searches are unaffected).

## Signature match (E-P7 v0.20 edits → OM-5 1.2 main)

`tools/port_sigmatch.py analysis/om5_1200/00_0100_40780020.decoded.bin`: **65 of 294 = 22 %** found once
(E-M5 III 36 %, E-M10 IV 47 %). By feature:

| Feature | Edits | Found once | Ambiguous | Absent |
|---|---:|---:|---:|---:|
| LCD tint | 4 | 4 | 0 | 0 |
| Aspect (XPan/21:9 tables, validator, ORF) | 92 | 38 | 1 | 53 |
| Tone hook | 9 | 2 | 5 | 2 |
| Grain / bloom / clarity / Art | 16 | 4 | 2 | 10 |
| SD loader | 4 | 1 | 2 | 1 |
| Recording limit (`1740`/`7200` operands) | 30* | 6 | 0 | 24 |
| fw label | 5 | 2 | 0 | 3 |
| Log video (+925 hook) | 3 | 1 | 0 | 2 |
| Film page / looks / editor / mono | 65 | 6 | 0 | 59 |

\* keyword grouping is loose; the two operand sites themselves (`0x60319157`, `0x6031915c`) are absent.

Key functions, by 10-byte chunk match (OM-5 runtime addresses, base `0x60021020`):

| Function | E-P7 | OM-5 | Chunks matched |
|---|---|---|---|
| COLOR (per-colour hue/sat/light) builder | `0x603b0068` | `0x603e2f1c` | 112 / 118 |
| LV development handler (glow in live view) | `0x60321d3b` | `0x603511f4` | 50 / 63 |
| Art Effect → job flag converter | `0x603eab39` | `0x6041f049` | 29 / 48 |
| Luminance LUT (tone ends) builder | `0x603b0a70` | `0x603e3768` | 11 / 123 |
| Static settings validator | `0x604cc98c` (1333 records) | `0x6050a2a0` (1439 records) | layout identical |

The image-pipeline code (COLOR builder, LV handler) is close to the E-P7. GUI code and the patched sites around
it are not: they need locating by hand, as on the OM-3.

## Main finding: the OM-5 has COLOR 1–3 and MONO 1–3 profile settings

Unlike the E-M10 IV and E-M5 III, the OM-5 is **not** missing the profile system in its settings:

- **Validator:** E-P7 settings 896–1060 (COLOR 1–3 per-colour saturation 927–962, profile tone, the MONO 1–3 block) appear
  on the OM-5 **renumbered by −5, with 142 of 146 records byte-identical** (same type, size and value list).
  E.g. E-P7 902 lists `903–906, 972`; OM-5 897 lists `898–901, 967`. COLOR 1–3 saturation = OM-5 922–957.
- **Missing:** the E-P7-only extension: COLOR4 (1655–1666, mode 64) and the profile list setting 1646
  (MONO 1–3 + COLOR 1–4). So the OM-5 has the PEN-F-style three-and-three set.
- **Menu text (0x101):** the same block as the E-P7: `Color Profile 1–3`, `@A0B2@Color Profile 1`…, `All colors`,
  `Single color`, `Monochrome Profile 1–3`, the MONO filter colours, `Film Grain Effect`. A short `Color/Mono Profile 1–3`
  list sits next to the `ART1…ART14` labels.
- **Not seen:** the `PROFILE`/`COLORTONE` debug strings (0 on the OM-5, 21/213 on the E-P7); this build looks stripped.
- **Not in the menus [C, owner 2026-11-07]:** the OM-5's Picture Mode list is i-Enhance, Vivid, Natural, Muted, Portrait,
  Monochrome, Custom, e-Portrait, Underwater, then Color Creator and the Art Filters.

### What is left of the profiles, and what is gone

| Part | E-P7 | OM-5 1.2 | Evidence |
|---|---|---|---|
| Profile settings (validator records) | COLOR 1–4, MONO 1–4 | COLOR 1–3, MONO 1–3, same layout | records above [V static] |
| Menu text | yes | yes, same block | 0x101 strings [V static] |
| Pipeline branch: mode 11 → COLOR builder | `0x603b02c1` `cmp 11,d1; bne` | **same bytes at `0x603e3175`**, builder at `0x603e2f1c` | byte compare [V static] |
| Picture Mode list (sid 111) rows for modes 11/12 | 8 rows (MONO 12.1–4, COLOR 11 ×4) among 33 | **none**: 26 rows `7 1 2 3 4 5 6 8 10 9` + Art 100–124 | validator list [V static] |
| Profile list sid 1646 (lever) | 8 rows | none | [V static] |
| Colour-wheel descriptor (cursor 1091 + 12 sat ids) | `0x60b40c4c` | **not found** (looked for 1086 + 922–933) | byte search [V static] |
| Parameter-builder branches that load each profile's settings (17 ids for COLOR1, 10 for MONO1) | `0x602b814e`… (jump table `0x60460458`, modes 54–59) | **not found** as immediates; best cluster `0x60967b87` has only the 12 sector ids. The OM-5 may read them another way (unverified without a disassembler) | immediate search [V static, negative] |

So the profiles look like **settings and pipeline leftovers without their menu entries or editor screen**, not a feature
switched off by one flag. Un-hiding them would mean adding Picture Mode rows (a relocated validator list, as the aspect
patch already does), labels, a wheel editor screen and possibly the per-profile parameter packing. That is a sizeable
GUI port, but the image side (COLOR builder and its mode-11 branch) is already there, which the E-M5 III and E-M10 IV lack.

## Assessment

Two routes for the looks, to be decided by a small on-camera experiment:

- **A. Revive COLOR 1–3.** Add mode-11 rows to the Picture Mode list, then check on the camera that picking one gives a
  normal photo and that its saturation settings take effect. If that works, the OM-3 port's editor approach applies, with
  3 COLOR slots instead of 4, but the wheel screen has to be built rather than hooked.
- **B. Host the looks on Color Creator** (stock and visible), as for the E-M5 III. Less GUI work up front, less like the
  other ports.

Either way the look format and Control Panel stay shared: one entry in `webapp/src/cameras.ts`, one export target in
`webapp/src/targets/` (three slots for route A).
- **Stand-alone patches** (LCD tint, aspect, fw label) relocate partly by signature. The recording limit, log hook and
  SD loader need locating by hand.
- **Which base:** the OM-3 port is the closer guide for addresses and GUI (base `0x60021020`, 3-digit setting shift,
  similar editor records); this repo for the format, tools and Control Panel. Code is ported by hand either way.

## Next steps

1. Set up the MN103 toolchain (`tools/setup_toolchain.sh`) and disassemble the OM-5 mode-11 path and the Picture Mode
   list builder (E-P7 `0x60de477b`), to see what feeds the COLOR builder its settings.
2. Test ladder steps 1–3 on the body: stock 1.2 through the SD updater, restore, a firmware-screen label change.
3. Route A experiment: COLOR 1 row added to the Picture Mode list, nothing else; shoot, compare against Vivid.
