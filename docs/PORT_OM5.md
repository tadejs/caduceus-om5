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

## Compressed spans (found 2026-11-07)

Two spans of the OM-5 main block are stored as **LZ4 "olycompress" containers**, not as plain code:

| Container (file offset in main) | Runtime span | Packed | Expanded | Section |
|---|---|---:|---:|---|
| `0x825fe0` | `0x60847000`–`0x60cbe800` | 2 254 189 B | 4 683 776 B | 5 |
| `0xc9d7e0` | `0x60cbe800`–`0x61343800` | 2 697 821 B | 6 836 224 B | 6 |

- Format: 16-byte magic `olycompress…`, u32 packed size at +16, u32 raw size at +20, 8 zero bytes, one LZ4 block from +32.
  The container sits at the start of its flash span and the rest of the span is `FF`. The expanded size equals the span
  size exactly, so the run-time image is the same size as the file [V].
- `tools/olycompress.py` (`expand`, `info`, `roundtrip`; `find`, `lz4_decompress`, `lz4_compress`, `repack` as a library)
  is our own implementation, written from the format notes in om-emu's `emu/src/fw/expand.mjs`.
  `python -I tools/olycompress.py expand analysis/om5_1200/00_0100_40780020.decoded.bin analysis/om5_1200/main.expanded.bin`
  gives the **run-time image** (SHA-256 `692f1b9e…`). Runtime address = file offset + `0x60021020` throughout, because the
  flash layout is contiguous [V].
- **Read the expanded image, not `00_0100_40780020.decoded.bin`, for anything in `0x60847000`–`0x61343800`.** In the raw
  file those bytes are LZ4 tokens with literal runs, which look like code in places. Every finding made from the raw
  bytes in that range before 2026-11-07 is void and is redone below. That covers the GUI code (about `0x608…`–`0x60f…`),
  the menu, label and descriptor tables (`0x611…`–`0x6133…`) and the first `port_sigmatch.py` run.
- The E-P7 has no compressed spans (its table's third word is a copy of the destination, not a flag).

### Boot section table (BCS)

At main file `0x6fd8` (`"BCS\0"`, entries from `0x6fdc`): 10 entries `{u32 dest, u32 flash src, u32 flag}`, flag 1 = compressed.
Flash src = `0x40780020` + file offset. Span = dest to the next dest; the last runs to the end of the image (`0x617e0fe0`).
The E-P7 table has 9 entries in the same layout, without flags. FF runs are in the expanded image, 64 B or longer [V].

| Sec | Runtime range | Size | Flag | FF runs ≥ 64 B | Tail FF (code space) | Other large runs |
|---|---|---:|---|---:|---|---|
| 0 | `0x600eb000`–`0x6029f800` | 0x1b4800 | plain | 5 | 0x12d4 at `0x6029e52c` | `0x6023e19c` (0x19664), `0x602883f4` (0x740c), `0x60297804` (0x3ffc), `0x602940c4` (0x373c) |
| 1 | `0x6029f800`–`0x605ce800` | 0x32f000 | plain | 2 | 0x27138 at `0x605a76c8` | `0x604da6e4` (0x8c) |
| 2 | `0x605ce800`–`0x60627000` | 0x58800 | plain | 1 | 0x6a80 at `0x60620580` | none |
| 3 | `0x60627000`–`0x60843000` | 0x21c000 | plain | 20 | 0x1804c at `0x6082afb4` | 19 small (0x74–0x88), e.g. `0x607bf364`, `0x607c1de0` |
| 4 | `0x60843000`–`0x60847000` | 0x4000 | plain | 1 | 0x1728 at `0x608458d8` | none |
| 5 | `0x60847000`–`0x60cbe800` | 0x477800 | **LZ4** | 1 | 0xed40 at `0x60cafac0` | none |
| 6 | `0x60cbe800`–`0x61343800` | 0x685000 | **LZ4** | 1 | 0x8d40 at `0x6133aac0` | none |
| 7 | `0x61343800`–`0x6140e000` | 0xca800 | plain | 1 | 0x1b488 at `0x613f2b78` | none |
| 8 | `0x6140e000`–`0x61609800` | 0x1fb800 | plain | 1 | 0x1ffb8 at `0x615e9848` | none |
| 9 | `0x61609800`–`0x617e0fe0` | 0x1d77e0 | plain | 2 | 0x13d494 at `0x616a3b4c` | `0x6169b978` (0x7688) |

(E-P7 tails for comparison, sections 0–8: 0x3ffc, 0x1cc50, 0x3880, 0x128e4, 0x9300, 0x84650, 0x44398, 0x4000, 0x4e2afe.)

- **Only the loaded part of a section is in RAM [V dis, 2026-11-08].** The boot code copies each section from its flash source up to an *end*
  that is usually below the next section's start (the BCS span). The ends come from `0x600462f3` (jump table `0x600473dc`, each case
  `mov imm32,d0`); the copy is `sec_copy` `0x6004635c` (id n = table index n-1; size = end - dest). Loaded ends by index 0..9:
  `0x6023e800 0x605a7800 0x60620800 0x6082b000 0x60846000 0x60cb0000 0x6133b000 0x613f3000 0x615ea000 0x6169c000`.
  So the "Tail FF" column above is **only usable up to the loaded end**: usable tails are index 1 `0x605a76c8`-`0x605a7800` (0x138 B),
  2 `0x60620580`-`0x60620800` (0x280), 3 `0x6082afb4`-`0x6082b000` (0x4c), 4 `0x608458d8`-`0x60846000` (0x728), 5 `0x60cafac0`-`0x60cb0000`,
  6 `0x6133aac0`-`0x6133b000`, 7 `0x613f2b78`-`0x613f3000` (0x488), 8 `0x615e9848`-`0x615ea000` (0x7b8). Sections 0 and 9 have no tail
  (their FF runs start beyond the loaded end). **The earlier "largest tails" (section 9 `0x13d494`, section 1 `0x27138`, section 8
  `0x1ffb8`) are void**: that space is not copied to RAM. `patches/om5_common` now refuses a claim beyond the loaded end.
  The C3 label code (`0x613f2ba0`+0x2c) is inside the loaded part of section 7.
- **Load order [V dis for the call order, I that the calls run in this order]:** `sec_copy` is called for ids 1, 8, 2, 3, 4, 5, 6, 7, 9, 10, that
  is table indices 0, 7, 1, 2, 3, 4, 5, 6, 8, 9 (calls at `0x6004599f`, `0x600459d8`, `0x60045a7a` …), each setting bit n-1 of the u32 `0xa2693fa0`
  after its copy returns (`0x600463ff`). A hook must not call code in a section that is not yet copied; code that runs from a hook can test
  `(0xa2693fa0 & 0x3ff) == 0x3ff` first (the C5 gate does). The E-P7 order is 1, 7, 2–6, 8, 9 (`docs/BOOTLOOP_T3.md`); the OM-3's is 1..10.
  Earlier line: "Load order is unknown": superseded.
- Sections 5 and 6 are the compressed ones. Their tail FF (0xed40, 0x8d40) is real run-time free space, but using it
  means expanding, editing and **repacking** the container, then checking that the packed size still fits the flash span
  (about 2.4 MB and 4.1 MB of slack). No patch has been tried this way [I].
- The boot code at the start of main (flash `0x40780020`) was disassembled up to `0x40780140`: stack and register setup,
  a copy loop, a jump into `0x60021xxx`. No immediate refers to the table (`0x40786fd8`/`0x40786fdc`) or the `BCS` magic,
  so the section-load loop is **not located** and "flag 1 = LZ4" rests on the data and om-emu's notes, not on this loader [I].

## CPU address base: `0x60021020`, not `0x60020020`

With the E-P7's base the OM-5 validator's list pointers land inside other records. With **`0x60021020`** (the OM-3's
base) every list decodes cleanly (e.g. the saturation sectors read −5..5). So OM-5 runtime address = decoded offset +
`0x60021020`. `oly_container.py --code-base 0x60020020` was used for decoding; it only affects the analysis address
labels, not the decoded bytes. `port_sigmatch.py`'s "same address" column is meaningless for this body because of that
base difference (its byte searches are unaffected).

## Signature match (E-P7 v0.20 edits → OM-5 1.2 main)

> **Redone on the expanded image (2026-11-07).** The first run read the compressed spans as raw bytes and reported
> 65 of 294 = 22 %. That figure is void.

`tools/port_sigmatch.py analysis/om5_1200/main.expanded.bin`: **100 of 294 = 34 %** found once (3 same address, 97
shifted), 15 ambiguous, 179 absent. (E-M5 III 36 %, E-M10 IV 47 %.) The "same address" column is meaningless here (base
difference), so read "found" as same + shifted. By feature, grouped by the first words of the `why` fields in
`releases/v0.20/edits.json` (our grouping; the first run's table used another, so the raw run is repeated for comparison):

| Feature | Edits | Found (expanded) | Found (raw, void) | Ambiguous | Absent |
|---|---:|---:|---:|---:|---:|
| LCD tint | 4 | 4 | 4 | 0 | 0 |
| Aspect (tables, rects, jumps, validator, icons) | 150 | 86 | 45 | 7 | 57 |
| Grain / bloom / clarity / Art / job extension | 19 | 2 | 2 | 3 | 14 |
| SD loader (loader, TRIG hook, bundle table) | 4 | 2 | 2 | 0 | 2 |
| Tone hook | 7 | 0 | 0 | 4 | 3 |
| WB getter calls | 15 | 0 | 0 | 0 | 15 |
| Recording limit (`0x60319157`, `0x6031915c`) | 2 | 0 | 0 | 0 | 2 |
| fw_version | 5 | 1 | 2 | 0 | 4 |
| Log video (+925 hook) | 2 | 0 | 0 | 0 | 2 |
| Film page / looks / editor / mono | 86 | 5 | 10 | 1 | 80 |

- Aspect gains most (45 to 86): the size tables and many GUI rects sit in the compressed spans.
- The raw-run "found" counts for Film/mono and fw_version included free-space claims that matched `FF` at the same
  address. In the expanded image those addresses hold data, so they correctly drop out. Most "absent" Film/mono edits
  are claims on free space or on E-P7-only records, which no signature can match.
- **Recording limit:** the two operand sites (E-P7 `0x60319157` `mov 1740`, `0x6031915c` `mov 7200`, inside the function
  at `0x60319139` with the `cmp 6/3/8/9 → 1740, cmp 4 → 7200` shape) have **no counterpart**. The OM-5 image has no
  function with that compare chain. Its six `mov 1740` hits are an id pair (`0x60417b1a`, 1740/1745), a capture-size
  table (`0x606632e9`) and false matches inside longer immediates. Locate the OM-5 limit by hand [V static, negative].

Key functions, by 10-byte chunk match (OM-5 runtime addresses, base `0x60021020`):

| Function | E-P7 | OM-5 | Chunks matched |
|---|---|---|---|
| COLOR (per-colour hue/sat/light) builder | `0x603b0068` | `0x603e2f1c` | 112 / 118 |
| LV development handler (glow in live view) | `0x60321d3b` | `0x603511f4` | 50 / 63 |
| Art Effect → job flag converter | `0x603eab39` | `0x6041f049` | 29 / 48 |
| Luminance LUT (tone ends) builder | `0x603b0a70` | `0x603e3768` | 11 / 123 |
| Static settings validator | `0x604cc98c` (1333 records) | `0x6050a2a0` (1439 records) | layout identical |

The image-pipeline code (COLOR builder, LV handler) is close to the E-P7. Most patched GUI sites are found once the
compressed spans are expanded (next table). The editor and film-menu records are the part that is missing.

### E-P7 patch sites located on the OM-5 (expanded image, 2026-11-07)

By 8-byte window voting from the E-P7 bytes, then checked by disassembly. OM-5 addresses, base `0x60021020`.

| E-P7 site | OM-5 | Found | Confidence |
|---|---|---|---|
| fw_version Body value setter calls `0x60d7f271` (+940), `0x60d7f2cd` (+300) | **`0x60dc58f6`**, **`0x60dc5952`** | yes | high: the same 5-call run (+940/948/956/964/300, widget ids 26..19), same frame |
| text setter `0x60d4c9df` (target of those calls) | **`0x60d90341`** | yes | high |
| Body label descriptor `0x6115d5e8` (ids 1725..1728, text ptr `0x6115d5e4`) | **`0x61241c7c`** (ids 1725..1728, ptr `0x61241c78`) | yes | high: one data reference each, at +0x8c (`0x61241d08` / `0x6115d674`) |
| Picture Mode list builder `0x60de477b` | **`0x60e513eb`** | yes, differs | medium: same prologue (sids 111 and 26), but no `15272 == 1646` branch; it sets list id **1608** (E-P7 1645/1646) and tests sid 1327 (E-P7 1337) |
| Picture Mode label table `0x611686d4` (72 rows × 12 B) | **`0x61250afc`** (70 rows) | yes, trimmed | high: rows 7 0 1 2 5 3 6 20 28 21, then **57 58 59** (MONO 1–3 labels, strings 2757–2759), 8 10 12 …; the COLOR rows 54 55 56 64 and MONO 4 (65) are gone |
| Shorter tables with 54 and 57 (`0x611690b8`, `0x611698bc`) | `0x612514e0`, `0x61251cd8` | yes | high: rows 7 0 1 2 5 3 6 28 21 **54 57** 8 9 10, as on the E-P7 |
| Colour-wheel descriptor `0x60b40c4c` (cursor 1091 + 927–938) | none; the OM-5 reads the ids inline (see "Colour-wheel view producer") | no | high (negative) |
| Editor descriptor table `0x61186ba4` (keys 0x13b3 / 13b4 / 13b5 / 140c, child lists of `{0, id}` u32 pairs) | none: no 4-pair run at 922 / 934 / 946, none with cursor 1085 / 1090 / 1091 | no | high (negative) |
| Editor records `0x61185be8…` (Up `0x60e20e0a`, Down `0x60e2107a`), film-menu marker entries | none found | no | medium |

The label tables still hold the MONO 1–3 labels and one generic COLOR and one MONO row. That shows the strings and rows
survive, not that any list offers them.

### Colour-wheel view producer (new, 2026-11-07)

The E-P7 wheel view is a vtable method (`0x60932fa6`, 4 cases, ids from the descriptor `0x60b40c4c`). The OM-5 has its
counterpart in the expanded image at **`0x60a34cb5`**, an 11-case switch (jump table `0x60c34a58`) referenced from 12
vtable slots (`0x60c28c0c`, `0x60c2b438`, `0x60c369e8` …; the E-P7 has 10). Case 5 (`0x60a34d1b`) is the wheel:

- It reads **setting 903** (E-P7 908) through the getter `0x602d004a` and branches on 1 / 2 / 3.
- Per profile it reads a cursor and then 12 saturation values, one getter call each, stored as 12 bytes plus a cursor
  byte: profile 1 = cursor **1085** + **922–933**, profile 2 = **1090** + **934–945**, profile 3 = **1091** + **946–957**
  (E-P7 cursors 1091 / 1096 / 1097; the OM-5 ids are 6 lower and there is no COLOR4). The default is profile 1.
- The same idiom as the E-P7 case 1 (zero value → 255) follows the 903 read. Cases 6 and 7 read setting 968 and 982–992
  (the MONO profile).
- Twelve more places use the ids as constructor / compare stubs, one per setting (`0x60861061`–`0x60861352`, ids 939–957).

So the OM-5 still has a **view producer that reads COLOR 1–3 live from the settings** [V dis]. Whether any screen
opens it, and whether the label for `0x60a34cb5`'s views is reachable, is not known [I].

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
| Colour-wheel descriptor (cursor 1091 + 12 sat ids) | `0x60b40c4c` | **no table; the ids are inline in the view producer `0x60a34cb5` case 5**: setting 903, cursors 1085 / 1090 / 1091, ids 922–957 (36 getter calls `0x60a34d60`–`0x60a34fe8`). The first run said "not found"; that was the raw-bytes error | expanded image, dis [V] |
| Colour editor records and child lists (keys 0x13b3…, E-P7 `0x61186ba4`) | `0x61186ba4`, `0x61185be8`… | **not found** (no `{0, id}` runs at 922 / 934 / 946, no cursor 1085 / 1090 / 1091 pairs) | expanded image, byte search [V, negative] |
| Parameter-builder branches that load each profile's settings (17 ids for COLOR1, 10 for MONO1) | `0x602b814e`… (jump table `0x60460458`, modes 54–59) | **not found at that site** (section 1 is plain, jump table entries 54–59 go to the default). The 36 saturation ids are read in the view producer instead, see above | [V dis] |

### Disassembly trace (2026-11-07)

MN103 objdump built with `tools/setup_toolchain.sh` under WSL Ubuntu (strip the script's CRLFs first; fetch binutils
2.45 from `ftpmirror.gnu.org` if ftp.gnu.org is slow). OM-5 addresses, base `0x60021020`.

**Back end intact [V dis]:**
- Capture mode byte (cap+28727) → job key via `0x6041eef9` / mapper `0x6037a714` (E-P7 `0x603eaa04` / `0x60349297`).
- Converter `0x6041a5e7` `cmp 11,d0` copies the 12 saturation values from cap+28732 for mode 11 only.
- Selector `0x603e3175` → COLOR builder `0x603e2f1c` (only caller `0x603e41eb`).
- Property-driven fill `0x614cf122` (called from `0x614ce915`) still has mode 11 → `0x614cf736` and mode 12 → `0x614cf7da` (hash table
  `0x615de974`). The mode-11 handler reads properties 63178, 63194 and the 63226 array (3 profiles × 12); the profile
  index comes from 63601.

**Front end missing [V dis]:**
- The record table at cap+29490 (43 × 44 B, built by `0x602c9fa4` = E-P7 `0x603f1e9c`) has records only for modes
  9 7 1–6 10 and Art 100–124. The E-P7's COLOR/MONO templates (`0x6053e4b8`, `0x6053e4f8`) have no OM-5 counterpart.
- Picture-parameter builder `0x602dcce9` (E-P7 `0x602b7b71`), jump table `0x6049a4e0` (64 entries): entries 54–59
  all go to the default `0x602dd277`. The earlier count "256 getter sites against 440 on the E-P7" came from the raw
  bytes and is void; the profile ids are read by the view producer `0x60a34cb5` (see "Colour-wheel view producer").
- Nothing seen writes mode 11 into the capture mode byte, so the COLOR builder is dead code in stock [I]. Rechecked on
  the expanded image: the 28 `disp16 = 28727` sites are all outside the compressed spans (`0x6032…`–`0x6042…`,
  `0x614c…`, `0x6165…`), the same as in the raw file. The compressed GUI code never touches that byte directly.
- Color Creator is mode 10 (template fill `0x602ca387`) and takes the general builder path [I].
- **Void:** the "12-id record stream" at `0x60967b87` was LZ4 literal bytes read at the wrong place. In the expanded
  image that address is ordinary code with no ids 922–957. The real users of 922–957 are the view producer `0x60a34cb5`
  (36 getter calls, ids 922–957 plus cursors 1085 / 1090 / 1091) and 12 setting stubs at `0x60861061`–`0x60861352`
  (ids 939–957) [V dis].
- Picture Mode rows for modes 11 / 12: the validator list for sid 111 still has none (`0x6050…`, plain section). The
  expanded label tables keep rows 57 / 58 / 59 (MONO 1–3) and, in two shorter tables, 54 and 57; the COLOR rows 55 / 56
  / 64 and MONO 4 (65) are gone. Nothing in the expanded image was found that builds a list from these rows (the list
  builder `0x60e513eb` sets list id 1608 only) [V static, I for the builder].

**Consequence:** adding only a mode-11 Picture Mode row would most likely give a shot without the profile's saturation
values (defaults or an error path); a hang can't be ruled out. Reviving COLOR 1–3 needs a mode-11 record template and
fill (E-P7 style), the property wiring from settings 922–957 to 63226/63601 (unverified), labels and an editor screen.

So the profiles look like **settings, a view producer and pipeline leftovers without their menu entries, editor records or
mode-11 record template**, not a feature switched off by one flag. (Updated 2026-11-07: the first read said the
front end was missing entirely; the expanded image shows the colour-wheel view producer and the live reads of 922–957.) Un-hiding them would mean adding Picture Mode rows (a relocated validator list, as the aspect
patch already does), labels, a wheel editor screen and possibly the per-profile parameter packing. That is a sizeable
GUI port, but the image side (COLOR builder and its mode-11 branch) is already there, which the E-M5 III and E-M10 IV lack.

### Emulator (om-emu, 2026-11-07)

[flatpanda/om-emu](https://github.com/flatpanda/om-emu) at `147ca89` runs the firmware's own kernel, GUI and camera
application; OM-5 (E144) is one of its ported bodies, and its `BLOBS.sha256` pins the same OM-5 1.2 file.

Setup: Node 26, in WSL Ubuntu (om-emu's paths are POSIX-only; Windows Node fails with `C:\C:\…`). Clone it inside WSL
(a Windows clone gets CRLF line endings and one test fails). Copy the stock `.BIN`s into its `fw/`, then:

```bash
node emu/bin/fetch-fw.mjs --model E144 --bin fw/OLY_E_144_1200_0000_0000.BIN   # also E142 for the E-P7
sha256sum --ignore-missing -c BLOBS.sha256
node emu/bin/test-pure.mjs
node emu/bin/modelcheck.mjs E144 --no-port   # without --no-port it rebuilds the port and needs the OM-1 (E143) blocks
```

Results [run in emulator]:
- `test-pure`: all passed.
- `modelcheck E144 --no-port`: **ok on the TFT and the EVF**: start, S1, S2, PLAY, INFO, MENU/DOWN/MENU, the
  remaining-shots probe, and an 8 GiB SDHC card formatted by the firmware with S2's file read back.
- **COLOR builder, native run on both bodies** (`debug.mjs --serve`, model via `OM_EMU_MODEL`): entry E-P7 `0x603b0068` /
  OM-5 `0x603e2f1c`, stop at their `ret` (`0x603b0244` / `0x603e30f8`). Inputs per the disassembly: `d1` output block
  (12 × 24 B records from +688, pointers at +3124..+3160, `d0` stored at +976), `a0` source records (2 groups × 6 × 28 B),
  `a1` 12 signed saturation bytes indexed `2·sector + group`. Same fixture on both: saturation ramp `(i % 11) − 5`, all 0,
  all +5, all −5. **All four cases: outputs byte-identical, no fault, the same instruction count (1550–1562).** Saturation
  changes the output (0 against ±5).
- The two builders' code differs only in branch targets and the gains-pointer table (E-P7 `0x60531810`, OM-5
  `0x6056e1cc`); the 12 gain entries they point to are identical [V static].

So the OM-5's dormant COLOR builder computes exactly what the E-P7's does. This tests the builder alone: what feeds it
on the OM-5 (the missing front end above) is still the open part, and the image pipeline isn't emulated.

## Assessment

Two routes for the looks, to be decided by a small on-camera experiment:

- **A. Revive COLOR 1–3.** Port the missing front end (mode-11 record template and fill, Picture Mode rows, labels),
  then check on the camera that a photo comes out normal and that its saturation settings take effect. The back end
  (converter, selector, COLOR builder) is already there. After that, the OM-3 port's editor approach applies, with
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

1. Test ladder steps 1–3 on the body: stock 1.2 through the SD updater, restore, a firmware-screen label change.
2. Decide route A or B. For A, trace whether anything maps settings 922–957 to properties 63226/63601, and port the
   E-P7's mode-11 record template and fill before any Picture Mode row is added. A row on its own is not a safe test.
3. Work on the expanded image only: `tools/olycompress.py expand`, then `main.expanded.bin`. Any patch in sections 5 or 6
   (`0x60847000`–`0x61343800`) needs a repack and a packed-size check; sections 0–4 and 7–9 are plain. Decide where the
   OM-5 patch code goes using the section table (usable plain tails: the loaded-end list above; the first version of this line named tails that are not in RAM, void).
4. Open items from the redo: the section-load loop (not located), the recording-limit site (no E-P7 shape), what opens the
   views of `0x60a34cb5` (12 vtable slots), and the meaning of setting 903 (E-P7 908).

## C4 and C5: the S1 hook and the SD probe (2026-11-08, emulator only)

Nothing here has run on a camera. Tags: [V dis] disassembly, [V emu] fishcu engine or om-emu run, [I] inference.

**Hook site [V dis].** The E-P7 SD-loader trigger (`0x602c805f`, `call 0x602c818c,[d2,d3,a2,a3],36`, reached for events with low half
`0x100`/`0x101`) is the same code on the OM-5 at **`0x602ed768`** (`call 0x602ed895,[d2,d3,a2,a3],36`, 5 B, section 1, plain). The 33 bytes
around it match byte for byte except the displacement; the callee is a table-driven variant of the E-P7's tracker, with the same effect
(S1 press `0x02000100` sets bit 1 of the tracker word `0x61e3a640`, release `0x02000101` clears it) and the same canonical event written
through `a0`. Continuation `0x602ed76d` (`mov (16,sp),d0`); `d0`, `d1`, `a0`, `a1` are dead there. The hook is a 5-byte `jmp` (`dc`) to a
trampoline that replays the call on the same frame, then continues: registers and stack equal stock.

**Spare RAM word [V dis, I for the clear].** `0x6218a262`, u16: padding at +174 of the "OlyHfwEventManager" singleton `0x6218a1b4` (ctor
`0x6085bb3e` writes +172 u16 = -1 and +176 u32 = 0; getter `0x6085bd1a`, 67 callers; vtable `0x60b4e5f0`, 36 slots; the HLS_EVENT task creation
`0x6085bed2` stores its tid at +172). Same layout as the E-P7's `0x622cbcc2` and the OM-3's `0x62b4dd16`. Nothing references it (no word equals
any of `0x6218a260..67`, no class method, getter user or other class code touches +172..+175 except the ctor and the tid code), the next object
starts at `0x6218a268`, and the `.bss` is cleared at boot by the DMA fill `0x6002146d`: here three calls (`0x61ec9e80`, `0x6205bd80`-`0x62550800`
from `0x600281da`, `0x62550800`-`0x627318c8`); the word is in the third. The clear itself is [I], as on the E-P7. Verified by `tools/om5_verify_noop_hook.py`.

**C4 [V emu].** `patches/om5_noop_hook`: the trampoline at `0x605a7700` (section 1 FF tail, the hook's own section) counts S1 presses in the
word. `tools/om5_verify_noop_hook.py`: ALL PASS (D/T/X/E, 18 runs, 3 mutations caught). om-emu E144: counter 0 at boot, 1 after S1, 2 after S2
(it half-presses first), unchanged by PLAY/INFO, 3 and 4 after two more S1; stock leaves it 0. The 17-frame walk equals the C2 (stock content) frames
except the firmware screen, which shows "Caduceus : 0.2".

**C5 [V emu].** `patches/om5_sd_probe`: the same hook; at the first gated S1 press it reads `C:\OM5MOD\BUNDLES.BIN`'s 32-byte header into a
`malloc(1024)` block and stores 0 / `0xFFFF` / handle in the word. Hook and gate sit in the section-1 tail (`0x605a7700`), the loader in the section-8
tail (`0x615e9900`); the gate (all ten section bits, `get_tid` == HLS tid, a medium status word == `0xF000`) runs before the loader is called, so
the later section is loaded by then. OM-5 addresses found by structure against the E-P7 wrappers and checked by `tools/om5_verify_sd_probe.py`:
malloc `0x60023818`, free `0x6002384a` (class-1 pool `0xa1e27d80`-`0xa1e32ac0`, 129..2048 B), fopen `0x606ab541`, fclose `0x606ab54a`, fread
`0x606ab6d0`, ftell `0x606ab75e`, fseek `0x606ab7c8` (whence 1 END, 2 SET), get_tid `0x600317b4`; section bits `0xa2693fa0`; media words
`0x61e43338/40/48/50`. Verifier: ALL PASS (22 scenarios, 4 mutations caught). om-emu: with the section bits and one media word stood in (om-emu does
not run the boot section loader or the media manager's mount word, as the OM-3 notes say) and a card formatted by the firmware: no file -> `0xFFFF`;
`OM5MOD/BUNDLES.BIN` with magic `OM5B` -> `0x7bc1` and the header in the block; wrong magic or a 16-byte file -> `0xFFFF`; no fault, shooting continues,
frames as in C4 (label "Caduceus : 0.3"). Without the stand-ins the gate does not open and the state stays 0. The HLS tid word is set by the real
firmware in om-emu (0xa2). Open: on a real camera the status is not visible; whether the SD slot's media word is one of the four is [I].
`tools/om5_card_add_file.py` puts the test file on an om-emu card image.

## Camera packages (`out/`, not committed)

Each folder has `DCOLYMP/E1449999.BIN`, `restore/E1449999.BIN` (the official file) and `SHA256SUMS`; C5 adds `OM5MOD/BUNDLES.BIN` (32 bytes, `OM5B`).

| Step | `DCOLYMP/E1449999.BIN` SHA-256 | What it proves |
|---|---|---|
| C1 | `a0c91f3ac7caaf0925b2af609e711183cf3b5982b3333224bcef26c0c5943d11` | the official file through the SD updater (the trigger works on the OM-5) |
| C2 | `35411d71ec724c0367488fbd0c4eb98f3191b3c2f9cc66f20f6f0c41a79e53e9` | the bootloader accepts our LZ4 re-encoding |
| C3 | `337b91988d59b8e0df26428391ad062184043f8af5f12c75f61ad64087df90b7` | a patched byte in a compressed span runs; firmware screen "Caduceus : 0.1" |
| C4 | `dc84df0b8540a92fea74e819fa843bfe6b2ec3b65984998e13feb80a5cbf65cf` | our code runs at S1 without changing behaviour; "Caduceus : 0.2" |
| C5 | `1d4459c072807469e8aa25a6403131e158b60c0c26027e7540e315d84ea61b55` | file I/O from the hook is safe with and without the card file; "Caduceus : 0.3" |

All `restore/` files: `a0c91f3a…3d11`. C2–C5 are main-only packages.

Results on the owner's OM-5:

- **C1 passed [C, 2026-11-08].** Hold OK + power on started the SD updater. The front orange light was steady for about
  1 minute, then blinked about twice a second until switched off (the E-P7 success pattern). Setup > Firmware shows 1.2
  afterwards. The trigger and the success signal are the same as on the E-P7.
- **C2 passed [C, 2026-11-08].** Our LZ4 re-encoding of both compressed parts (main-only package `35411d71`): same
  steady-then-2 Hz pattern, firmware 1.2, photo, playback, menu and half-press normal. The bootloader accepts our encoder.
- **C3 passed [C, 2026-11-08].** Label patch (`337b9198`): Setup > Firmware shows "Caduceus 0.1"; photo, playback, menu
  and half-press normal. A patch inside a compressed span (section 7 tail code + descriptor) runs on the camera.
- **C4 passed [C, 2026-11-08].** S1 no-op hook (`dc84df0b`): "Caduceus 0.2"; half-press, focus, photos, playback, menu
  and video normal. A jump from the S1 call site (section idx1) to a trampoline in the idx1 loaded tail runs without side
  effects. The press counter itself is not visible on the camera.
