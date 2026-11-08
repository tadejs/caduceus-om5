# Caduceus firmware guide: the E-P7 1.1 firmware, and how to port Caduceus

This is the entry point for agents and developers who want to port Caduceus to another Olympus/OM body.
It covers what this project has learnt about the PEN E-P7 1.1 stock firmware, how the firmware is built,
and how to patch it without bricking a camera. It gives facts with example addresses and links to the
detailed docs. It does not repeat them.

- **Tags.** **[V]** = verified in code (disassembly, byte dumps), in the emulator, or on a camera.
  **[I]** = inference. Where a doc adds **[C]**, that means a camera observation. When a line has no tag, the
  linked doc has the detail.
- **Addresses** are E-P7 1.1 (build 1100) main-program CPU addresses, unless marked as an OSD offset. They are
  examples. **None of them carries over to another body.** On another body every address has to be found again.
- **Scope.** Firmware stays with its port: each port has its own repo, install guide and issues (see
  [`../AGENTS.md`](../AGENTS.md) and [BODIES.md](BODIES.md)). Ports are being asked for in issues #21, #22, #25,
  #27 and #29. A camera-tested OM-3 port already exists: [androidfanman/caduceus-om3](https://github.com/androidfanman/caduceus-om3)
  ([OM3_NOTES.md](OM3_NOTES.md)).

Contents:
1. [The firmware package](#1-the-firmware-package)
2. [The main program](#2-the-main-program)
3. [The settings system](#3-the-settings-system)
4. [The UI](#4-the-ui)
5. [The OSD component](#5-the-osd-component)
6. [The image pipeline](#6-the-image-pipeline)
7. [CPU access to image buffers](#7-cpu-access-to-image-buffers)
8. [SD card I/O from firmware](#8-sd-card-io-from-firmware)
9. [Hook techniques](#9-hook-techniques)
10. [Safety lessons](#10-safety-lessons)
11. [Toolchain and workflow](#11-toolchain-and-workflow)
12. [Porting checklist](#12-porting-checklist)
13. [Licence and etiquette](#13-licence-and-etiquette)

---

## 1. The firmware package

Details: [FINDINGS.md](FINDINGS.md), [SD_UPDATE.md](SD_UPDATE.md), [SD_REJECT.md](SD_REJECT.md), [INSTALL.md](INSTALL.md).

**The update file** is `OLY_E_142_1100_0000_0000.BIN`, 111,017,984 B, SHA-256 `9d1610…eaeb` [V]. Model code
0x1420. It is a sequence of components:

| Kind | Size (payload) | Container address | Role |
|---|---:|---|---|
| 0x100 | 26,214,336 | 0x406a0020 | Main program (CPU base 0x60020020) [V] |
| 0x101 | 26,214,336 | 0x421a0020 | OSD: bitmaps, strings, fonts [V] (§5) |
| 0x106 | 45,219,776 | 0x43aa0020 | Common resources; holds an ARM Linux Wi-Fi/BT image [V strings] |
| 0x10e | 262,080 | 0x00000020 | Sub-CPU firmware [I, strings only] |
| 0x102 | 13,107,136 | 0x467e0020 | Picture resources: 221 JPEGs [V] |

**Container format** [V] (`caduceus.py` `parse`/`reseal`, and fishcu's `oly_container.py`):
- Each component is a 32-byte header, the payload and a 32-byte trailer, so `len + 64` bytes.
- Header bytes 0–1 are `OE`, bytes 2–3 the model (`14 20`), +12 the payload size (u32), +20 the kind (u16).
- The trailer repeats header bytes 0–27. Trailer +28 is the checksum word.
- **Checksum:** the 32-bit little-endian word sum over header + *decoded* payload + trailer must be 0.
- **Scramble:** every byte is inverted, and each 16-byte group is permuted by lanes
  `1,3,0,2,6,4,7,5,11,10,9,8,13,14,12,15`. Decode and encode are exact inverses; the round trip of the stock file
  is byte-identical [V].
- The format was documented publicly by Altsoph and mx3 (CHDK forum thread 2471). `caduceus.py` is our own
  small implementation of it.

**Patching** keeps the payload length, header and trailer bytes 0–27, and recomputes only the checksum word. A
package may contain **any non-empty subset** of components. Main must come first when present. Main-only and
OSD-only packages both work [V emulator, V camera].

**How the updater flashes it** [V code unless marked]:
- The user holds **OK** while switching on [V camera]. The code that reads that button combination was **not
  found** in main. It is probably pre-main boot code or the sub-CPU [I].
- Task 88 `FIRMUP_CLI` (0x60c95bab) looks in `DCOLYMP` for `E142????BIN` and picks the file with the **largest hex
  suffix above 0**. So `E1429999.BIN` is chosen and `E1420000.BIN` never is [V emulator].
- Validator 0x60c964c1 checks header shape, model 0x1420 and the checksum of **every** component before the
  first write. It does **not** read the version field: same version and downgrades are accepted [V emulator].
- Components are written in a fixed order with **main last**. Each one is erased in full, programmed and read
  back. There is no rollback and no compare-and-skip except for the BOOT kind [V].
- **LED:** steady while it reads and writes, then a blink of about 2 Hz = **success** [V camera, proven by an OSD
  string probe]. A fast blink with no steady phase = rejected, nothing written. The code-derived reading of the
  LED modes was wrong at first ([SD_REJECT.md](SD_REJECT.md)); the camera probe settled it.
- `FIRMUP_SRV` is started only in normal startup, after section 2 has loaded and its initialisers have run
  (USRSTART5, 0x601922f0) [V]. So a fault early in boot could make the SD updater unreachable [I]. **In practice
  the E-P7 recovered once from a bootloop** (battery out, then OK + power with the stock restore) [V camera]. That
  is proven on one body, once.

**Restore path.** The builder always writes a restore file: the exact stock bytes of the same components as the
patched package. Flashing it is a full rewrite back to stock. Every release checks restore == stock [V].

**Never distribute OM firmware.** The repo holds edit lists, not images. `caduceus.py` and `tools/fetch_stock.sh`
download the official file from OM's update server (`fw01.om-digitalsolutions.net/ww_inform/fcache.do`, the same
request OM Workspace makes) and check its SHA-256.

---

## 2. The main program

Details: [HSL_PORT.md](HSL_PORT.md) (code space), [BOOTLOOP_T3.md](BOOTLOOP_T3.md) (boot facts),
[SD_V2_CRASH.md](SD_V2_CRASH.md) (memory), [CODE_SPACE_MAP.md](CODE_SPACE_MAP.md) (allocation).

**CPU.** Panasonic MN103 / AM33 (binutils target `mn10300-elf`, objdump machine `am33-2`) [V]. Decoded main
offset 0 is CPU address **0x60020020**, and container address 0x406a0020 [V]. `0x6…` is the cached alias, and
`0xa…` (+0x40000000) the uncached alias of the same memory [V]. There are several CPUs; some tasks run on another
core (`(0x8050)` holds the CPU id) [V].

**Sections.** The descriptor table at **0x60027068** has nine 12-byte entries `{start, logical source,
destination}`. The end getter **0x60045786** is a jump table of `mov imm32,d0` stubs. The loader 0x600457e7 copies
each section and sets bit (id−1) in **0xa272e420** [V]:

| Section | Range (end exclusive) | Free FF tail inside the load |
|---|---|---|
| base (resident) | 0x60020020–0x600d7800 | – |
| 1 | 0x600d7800–0x60211000 | 0x602108ec–0x60211000 |
| 2 | 0x60280800–0x6055e800 | 0x6055e5b0–0x6055e800 |
| 3 | 0x6057b200–0x605b7800 | 0x605b7180–0x605b7800 |
| 4 | 0x605baa00–0x60748000 | 0x60747f1c– (avoid: a zero-count initialiser points here) |
| 5 | 0x6075a800–0x60b9e000 | 0x60b9dd00–0x60b9e000 |
| 6 | 0x60ba7000–0x61243000 | 0x612429b0–0x61243000 |
| 7 | 0x612c7000–0x61379000 | 0x61378c68–0x61379000 |
| 8 | empty | – |
| 9 | 0x613c1000–0x6143a800 | 0x6143a350–0x6143a800 |

- **Load order is 1, 7, 2, 3, 4, 5, 6, 8, 9** [V]. Code a hook calls must be in the **same section as the hook or
  one loaded earlier**. Section 1 loads first, so most Caduceus code lives there. The OM-3 port bootlooped once
  when a section-2 hook called section-8 code that wasn't loaded yet [I, its report] (§10).
- Boot waits on each section in turn (USRSTART2, wait(2) at 0x60190e18). A bus monitor at 0x99700000 narrows over
  the not-yet-loaded sections and is disabled after wait(9) [V]. It is not a write protect, and was ruled out as
  the test-3 cause [I strong].
- The base image is about 26 MB; the loaded image runs to about **0x6191ffe0**. Stock writable statics all sit
  at **≥ 0x61b2xxxx** (heap pools, then .bss 0x621caf00–0x62796888, zeroed at boot) [V].
- **Stock never stores into the loaded image.** A linear sweep of all 32,217 absolute stores found 0 into
  0x60020020–0x6191ffe0 [V]. The one Caduceus build that did crashed (§10).

**Code space.** There are two kinds:
- **FF tails** inside each section's load (table above), about 8.8 KB in all [V]. Static scans found no
  references into them. That does not rule out computed or DMA access [I].
- **Dead EVF gamma tables in section 1**, from about 0x601de13c up to 0x601e3b3c: R/G/B tables of 256 B per
  "step", each pointed to by one step-table word (0x601e3b40–0x601e3d68) [V]. The EVF programmer runs only for the
  EVF device (COLORTEMP, 0x60174d19), and **the E-P7 has no EVF** [I strong]. Code in these tables has run on the
  camera since test 11. **Do not reuse them on a body with an EVF.**
- Every claim is registered in [CODE_SPACE_MAP.md](CODE_SPACE_MAP.md) first. Each build script then re-checks
  that the range is unreferenced (abs32 scan on both aliases and the flash source alias, plus PC-relative
  encodings), pins known false positives after a `dis.sh` check, and keeps 16-byte FF guards [V].

**Heap** [V code, [SD_V2_CRASH.md](SD_V2_CRASH.md)]:
- `malloc` 0x60022782 and `free` 0x600227b4, both with a `[],4` frame. They serve fixed-block pools of ≤128,
  ≤2048 and ≤16384 bytes. Above 16 KB malloc logs an error.
- Class 1 (≤2048) is the pool **0xa1f8d800–0xa1f98540** (44 KB), class 2 (≤16384) **0xa1dd8d40–0xa1e85800**.
  Both return **uncached** addresses, so all cores see the same bytes without cache maintenance.
- Large image buffers (40 MB at full size) come from the job buffer pool (0x60350560, freed by 0x60350843) [V].
- Caduceus keeps all runtime state in one class-1 block "P" (1024 B) whose handle is a u16 at **0x622cbcc2**, a
  padding halfword in the HLS event-manager object (.bss, 0 at power-on) [V camera, probe P2]. P =
  0xa1f8d7ff + h. The layout grew per feature (bundle table at +32, names at +544, editor state at +680, grain
  record pointer at +1000); see [SD_BUNDLES_V3.md](SD_BUNDLES_V3.md) and [LOOK_EDITOR.md](LOOK_EDITOR.md) §2.3.

---

## 3. The settings system

Details: [PROFILE_INDEX_WB.md](PROFILE_INDEX_WB.md), [LOOK_EDITOR.md](LOOK_EDITOR.md) §1.3–1.4,
[BUTTONS.md](BUTTONS.md) §2, [LV_BUNDLES.md](LV_BUNDLES.md) §2.3, [MOVIE_COLOUR.md](MOVIE_COLOUR.md) §2.

**Sids and properties** [V]:
- Every setting has a settings id (sid). Table **0x604d6c14** maps sid 0..1768 to `{u16 property key, u16 index}`.
  The key is `id<<4 | type`; the type gives the width (1 byte, 2 half, 4 word) and signedness.
- Property getter **0x6005e971**(d0 = key, d1 = index, a0 = 0). It reads a RAM registry, so settings have no fixed
  RAM address. Getter by sid: 0x602cbc0a.

**The GUI facade** `OlyApiSettingControllerController` (singleton 0x607f9062) has one vtable at **0x60acb0dc**
[V]. The editor reads and writes everything through it:

| Slot | Word | Stock | Role |
|---|---|---|---|
| +0 | 0x60acb0dc | 0x607fa129 | SetValue(id, flags, value) |
| +8 | 0x60acb0e4 | 0x607f9549 | GetValue(id, &out) → 0x602af887 |
| +24 / +28 | 0x60acb0f4 / f8 | – | count / value by index |
| +152 | 0x60acb174 | 0x607f964a | GetDefault(id, &out) |

The set path is GUI setter → `call 0x60827440` at **0x607fa355** (settings manager) → set 0x602af890 → store
0x602b9deb → post-set 0x602b143e → the sid's descriptor callback at descriptor+24 [V]. Descriptors are 60-B
records in the registry at 0x604317ac (439 records). An unchanged value still runs the callback [V], so
re-writing a value is a cheap way to make listeners redraw.

**Validators and allowed values** [V]:
- The static validator **0x604cc98c** has 1333 records of 20 B: `{u16 sid, u16 attr, u32 list, u32 alt, u16 bytes,
  u16 elem, u8 sub, u8 type, u16 0}`. They are hashed by `sid & 127` into RAM 0x62248528 at boot.
- Count 0x602b9f70, value by index 0x602b9fdb and dial step 0x602b9cda all read this list. For most sids, **the
  static list is the whole story**: it decides what menus offer and what the dial walks.
- To allow new values, point the record's list at a new list and change its byte count (aspect: record 0x604cec14 →
  list at 0x60210f70; buttons: in-place lists) [V camera].
- Menus, Live Control and the SCP have **separate value→label lists**. Adding a value to the validator does not add
  a label, and vice versa (§4).

**Derived settings** [V, [MOVIE_COLOUR.md](MOVIE_COLOUR.md) §2]:

| Sid | Key | Meaning |
|---|---|---|
| 1645 | 0xc9b2[0] | Normal picture-mode list (Vivid 1, Natural 2, Muted 3, …, art 100–124). **Movie always uses this one** |
| 1646 | 0xc9b2[1] | Profile list: COLOR (11) + slot 1–4, MONO (12) + slot |
| 15272 | virtual | Which of 1645/1646 is active. Sid 44 == 1 (movie) forces 1645 |
| 111 | 0x9481 | Effective picture mode, derived through table 0x6043dfec. What the pipeline reads. GUI values: COLOR1–4 = 54/55/56/64, MONO1–4 = 57/58/59/65 |
| 908 / 973 | 0xF6B1 / 0xF731 | Effective COLOR / MONO profile number |
| 915–926 | 0xF6EA | Effective 12-sector saturation |
| 927–962, 1655–1666 | – | Per-profile saturation, COLOR1–3, then COLOR4 |

Traps:
- **The GUI enum of sid 111 is not the pipeline enum.** Vivid is 0 in GUI 111 and 1 in 1645 and in the record [V].
- The picture-mode refresh in the settings manager runs only for sids 111/1645/1646/15271, not for the sector ids
  [V].
- Stock code at three sites sets 1645 := 21 (Color Creator) when 15272 == 1645. That trap switched movie mode to
  Color Creator in one v0.26 build [V camera].

**Persistence.** Stock settings are saved by the camera. Caduceus never adds settings: its state lives in heap P
and in card files (§8). A boot tail (0x60192961, the per-boot resets) is where stock resets volatile settings; the
aspect patch once clamped there with the stock reset primitive 0x602b972b [V], and later removed the clamp.

---

## 4. The UI

Details: [LOOK_EDITOR.md](LOOK_EDITOR.md), [LOOK_EXTRAS.md](LOOK_EXTRAS.md), [FILM_MENU_V2.md](FILM_MENU_V2.md),
[FILM_LIBRARY.md](FILM_LIBRARY.md) §5.1 and §8, [ASPECT.md](ASPECT.md) §1b–1j, [BUTTONS.md](BUTTONS.md),
[MOVIE_COLOUR.md](MOVIE_COLOUR.md) "Wheel", [LOOK_MENUS.md](LOOK_MENUS.md).

**Screens, records, children** [V]:
- A screen is reached through a screen group and a state. Live Control is group 20 (actuator 0x60e23b98). Its
  art-variation route (state 4,7) opens group 18 with an editor key; Caduceus reuses that to open the COLOR4 wheel
  from movie Vivid.
- Editors are keyed. The descriptor table 0x61186ba4 lists `{u16 key, u16 n, u32 child list}`; the COLOR1–4
  editors are keys 0x13b3/0x13b4/0x13b5/0x140c, each with a cursor sid and 12 sector sids.
- **Parent controller records** (56 B, 0x61185be8.. for COLOR1–4): +4 init, +12 Up, +16 Down, +20/+24 Left/Right,
  +28/+32 dial, +36 catch-all, +44 INFO, +48/+52 more keys. Init copies them into the parent object, and the
  router 0x60e20cdc dispatches events to them. **Re-pointing a record word is a data-only hook.**
- **Property children** (144 B, vtable 0x61155a7c): +84 sid, +88 count, +90 current index, +96 codes, +100 list.
- Events [V]: step 0x9004/0x9006, sector 0x9008/0x900a, commit 0x900c, INFO 0x9014, MENU 0x900e, PLAYBACK 0x9010,
  Movie 0x9012, Delete 0x9016, held OK 0x903e. **On camera only INFO reaches the COLOR editor;** Movie and Delete
  never arrive, and MENU/PLAYBACK leave it [V camera]. The emulator does not run the screen stack, so key routing
  has to be tested on a camera.
- **Views** rebuild their state from part tables in .data. The COLOR view's guide label is part 3 of the table at
  RAM 0x61fc27d0; its **flash init image word** 0x6025aa98 is what Caduceus edits [V camera].

**Labels** [V]: list entries are `{u32 value, label source}`, where a label source is `{u16 kind, u16 id, u32 text}`.
Two builders fill labels: **0x60df250b** (look up by value; 277 callers) and **0x60df25b5** (ready source; 239 callers).

| Kind | Draws |
|---|---|
| 3 | Bitmap id |
| 4 | String id (OSD string table) |
| 1, 2 | String id and text |
| **5, 6, 12** | **Text from a `char*`**, widened to UTF-16 at draw time (32 characters max) |
| 7, 8 | Bitmap and text |

- A **kind 4 → kind 5** change with a text pointer renames a row with no new string resources (Firmware screen
  "Caduceus", button row "Right Fn") [V camera].
- Runtime names come from hooks in both builders at their kind-4 points (0x60df255f, 0x60df25dc). The hooks swap in
  heap text only for marked entries and replay the 6 stock bytes otherwise, so with no card the labels are stock
  [V camera, test 8].
- **Keep text short.** Widget widths aren't decoded. "Body Caduceus" clipped on the Firmware screen; editor labels
  are capped at 16 characters.

**Item tables and viewers.** One setting can appear in several lists, each needing its own edit. For Image Aspect
there were five [V camera]:
- the menu descriptor list (0x6117af14 → 0x6117a834);
- the Lv_Control select array record 0x61176fc0, which is the SCP/LC option strip;
- row 16 of the 74-row item table 0x6116ae1c;
- the info-display lists;
- the SCP current-value cell, a separate widget record 0x610a3c38 with its own 106×31 part bitmaps.
Find them all with a stride search for the stock value sequence and its string ids ([ASPECT.md](ASPECT.md) §1b).
Each one missed showed up as a separate camera bug.

**Button functions** ([BUTTONS.md](BUTTONS.md)) [V]: buttons are sids whose value is a function code. Key table
0x60f389a8 maps key code → sid, and the dispatcher 0x60cc872f maps sid → value → action through the function table
0x60f38af8 (55 entries), with no per-key check. The allowed codes are the validator lists. The Button Function
screen's rows come from a `Cursor_Move` item list (record 0x61150568, mode 42); relocating it added the arrow
rows. Arrow sids are 50 up, 51 down, 52 left, 53 right [V camera]; up/left have no validator record.

**How we added pages and rows** (all camera-tested):
- **Editor pages:** an axis byte in heap P selects the page (Film, Saturation, Hue, Light, Extras). INFO
  (record +44) steps it.
- **Values on those pages:** the facade +8/+152 words and the setter call at 0x607fa355 answer from the heap
  table when the axis isn't Saturation.
- **Wheel polygon:** a call at 0x609330f9 inside the C3D producer (view vtable +4972 = 0x60932fa6) makes the
  polygon follow the page.
- **Redraw:** re-writing the cursor setting through the facade makes the listeners redraw.
- **Layers:** a new layer re-points words an older layer owns and falls through to it on a miss (look_extras
  over look_editor).
- **New option values:** validator list + every label list + icons (§5).
- **New rows:** relocate the screen's item list and add label records.

---

## 5. The OSD component

Details: [OSD.md](OSD.md), `tools/osd_extract.py`, [ASPECT.md](ASPECT.md) §1g–1j.

**Layout** (offsets in the decoded 0x101 payload) [V structural]:
- Directory at 0: 128 × `{u32 ptr, u32 size, 0, 0}`, 70 used. Pointers are absolute in 0x421a0020 space.
- Resource 0 is the main bitmap bank, runtime base **0xa3091000**. Its header +16 points at the bitmap table
  (7576 descriptor pointers), and +0 at the symbol table (245 `@A0xx@` symbols).
- Resources 1–34 are the language string tables, shared runtime base **0xa407f540**, 3645 UTF-16LE strings each.
  English is resource 8 (string 2749 = "Color Profile 1").
- Resources 35–68 are fonts; resource 69 is a second bitmap bank at **0xa3611000**.
- The loader copies the directory and banks from flash with no CRC; the bank-69 copy is bounded by 0xf26140 [V
  code]. The bitmap getter 0x60d10ba1(id) returns `&table[id]`, with no bounds check.

**Bitmaps** [V]:
- Descriptor: `u16 fmt, u16 w, u16 h, u16 field6, 0, 0, u32 encoded size, u32 payload ptr`. fmt 14 is a 24-B
  record; fmt 1 is an empty 20-B placeholder.
- RLE stream: a 4-byte header (h−1, w−1, mode), then tokens packed into 64-bit groups, with `run = (tok &
  run_mask) + 1` and `index = tok >> run_bits`.
- Modes: **11** = 8-bit index, 4-bit run (12-bit tokens, most icons); **10** = 4/4; **1** = 2-bit symbols;
  **9** = 2-bit font glyphs.
- All 5514 bitmaps decode strictly, and our encoder reproduces all 4976 stock mode-11 payloads byte for byte [V].
  The PEN-F's mode 3 doesn't exist here, so PEN-F bitmaps must be re-encoded.

**Editing bitmaps:**
- **In place works** [V camera, v0.25 strip icons and v0.28 SCP cell]: re-encode into an existing id, keep the
  stream ≤ the original payload size, zero-pad the rest, set the encoded size exactly, keep the colour key. The
  descriptor width may shrink. No directory or table edit is needed.
- Pick donor ids the body can't draw (headphone icons on a body without a jack, the hidden High Res Shot "50M F").
  Unused is [I]: ids can be computed, so a reference scan isn't proof.
- Appending new bitmaps after resource 69 (growing the directory size) is sound in code but **never confirmed on
  camera** [I].

**Strings:**
- Edit in place at the same UTF-16 length. The first camera proof of the updater was string 134 "Firmware" →
  "FIRMWARE" in an OSD-only package [V camera].
- Symbol codes differ by body: `@A032@` is Delete on the E-P7 and `@A047@` on the PEN-F.

**Not decoded:**
- Which of the 58 palettes in main (0x61110a74..) each layer uses at run time.
- The font code-point map beyond ASCII.
- Wheel geometry. The colour wheel is not a bitmap: C3D draws it at run time.

---

## 6. The image pipeline

Details: [HSL_PORT.md](HSL_PORT.md), [V02_BUNDLES.md](V02_BUNDLES.md), [GRAIN_PORT.md](GRAIN_PORT.md),
[BLOOM_HALATION.md](BLOOM_HALATION.md), [HALATION_RED.md](HALATION_RED.md), [TONE_ENDS.md](TONE_ENDS.md),
[SHARPENING_OFF.md](SHARPENING_OFF.md), [LV_BUNDLES.md](LV_BUNDLES.md), [LV_ART_PREVIEW.md](LV_ART_PREVIEW.md),
[MOVIE_COLOUR.md](MOVIE_COLOUR.md), [VIDEO_LOG.md](VIDEO_LOG.md), [VIGNETTE.md](VIGNETTE.md).

**Settings to capture to job** [V]:
- The capture population 0x602a857e.. reads settings into a capture record: picture mode at capture+24053,
  12 × S at +24058, profile number at +24083, mono settings at +24084..86.
- **Converters** copy capture fields into job params (params = job+36): 0x603e62d1 is shared by four still
  builders, 0x603e8af1 is the CPU2 path, and 0x603e91e4 the recording path. Many copies are gated on the mode,
  for example grain only for mode 12 at 0x603e66ab.
- **The job carries no profile number.** Caduceus adds a 24-B job extension: constructor 0x603537eb, size 15488
  → 15512 (0x603537f0), tail memset 16 → 40. The converters write +15488 profile, +15489 mode, +15490 marker 0xB2
  [V camera].
- Live-view jobs never pass a converter, so their readers take the profile from property 0xF6B1 instead.

**Jobs** [V]:
- The key at **job+11308**: 11 COLOR, 12 MONO; art filters 0x0101_00yy (Type I) / 0x0102_00yy / 0x0103..;
  Soft Focus is 0x01010065. The hidden log and Flat modes are 2001 and 2000.
- Job flags at job+16 turn effects into passes: 0x80 Soft Focus, 0x40 Star Light, 0x400 Pin Hole, 0x100/0x200
  Diorama, 0x80000 COLOR shading. The converter 0x603eab39 maps the Art Effect id to these.
- Job type at job+20: 0x1000 ordinary still, 0x3x live view and movie, 0x40 recording, 0x110/0x120 special capture
  kinds. The class at job+11341 decides the reduce sizes.
- **Stock strips some flags on some job types (0x60353b15), but only for art keys.** Injecting art flags on key
  11 skips those strips; that is how a COLOR Blur restarted the camera (#20, §10).

**Work list and requests** [V]:
- The work-list builder **0x60357415** dispatches on the key (key-12 handler 0x6035778b; keys < 12 go to the
  default 0x6035749f), then adds passes for each flag. It runs **inside the constructor, before the converter
  writes the marker** (grain s2, §10).
- The work list runs synchronously in the capture task (0x60354214), so every added pass delays the save.
- The request is embedded at job+11364 (1884 B). Fields: +8..11 key, +12 stage, +14 job type, +20 and +84 input
  buffers, +148 third port (shading mask), +1795 sharpness, +1816 S ×12, +1828 grain level, +1838 effect code.
- The packer 0x60354ce4 clears the request and fills it from params.

**Development stage list** **0x603b1466**, run by 0x603b17e6/0x603b1819 for request kinds 0/16 [V]:
- **0x603b0252** is the COLOR selector. It calls the builder **0x603b0068** only for key 11 (test at 0x603b02c1).
  The builder writes 12 output records (+688, stride 24: hue +18, sat +20, L +22) from the COLOR table 0x6051de2c.
  This is the H/L hook site (0x603b02d9), the same shape as the PEN-F.
- **0x603b0a70** builds the 256 × u16 luminance tone LUT (10-bit output), bent by tone control into O+2612.
  Blacks/Whites hook the call after it (0x603b14ff).
- 0x603b132a is the chroma stage (which makes MONO grey); 0x603ac0c1 the sharpening gain stage (one byte from
  Sharpness, 0 at Sharpness −5 on key 11).
- There is no separate live-view colour builder: live view and stills share this list. The difference is the input
  and the transport. The second builder 0x603afb94 (art tables) bypasses the H/L hook [V].

**Combiners** [V]:
- The selector **0x603b5200** maps (key, stage, effect) to a combiner. Config 0x603a013b uses hash 0x60524508; the
  bank is at base+7364.
- Combiners 11 and 12 are a linear mix (weights w and 1024−w). 31 and 32 are grain, with gain tables
  0x604ec0ce / 0x604ec0d4. 33 is the vignette or shading mask. 136 is additive (Star Light).
- Combiner 11 is diagonal in YCbCr. It has no cross-channel term, so a coloured glow isn't possible through it
  [V camera].
- **Clarity** is combiner 11 with w > 1024, an unsharp mask [V camera].

**Soft Focus passes** (flag 0x80, job classes 0–3) [V]: reduce buffer 0x1000 → 0x1060 (160×120, format 0x0305,
4 B/px), upscale → 0x1061 (format 0x0302, 2 B/px), then blend 0x1000 + 0x1061 → a new 0x1000. Live-view classes
use a single pass to combiner 12. The only blur is that down/up scale. Bloom and clarity reuse these passes on key
11 with our weight.

**Shading mask** [V code]: the cache 0x6036b635 and generator 0x6036cec0 build a 128×128 u16 Q10 mask (format
0x080C). The CPU writes it, and the request copies its descriptor to request+148. A buffer in another format at that
port gives garbled tiles [V camera].

**Movie and live-view path** [V, [MOVIE_COLOUR.md](MOVIE_COLOUR.md) §1]:
- Picture mode goes property 0x9481 → record +294, then the key mapper 0x60316bc7 → 0x60324d6b → 0x60349297.
- The job comes from the IPCR handler 0x60321d36, through the same constructor (call at 0x60321ea8). S arrives
  by message → record R+2119 → params+5538.
- Movie mode never has picture mode 11. The v0.26 Movie slot changes only the key given to the mapper
  (0x60316bfb).
- Log video sets the hidden byte +925 = 2 at the settings message (0x6030df3d), which selects effect id 2001 and
  its gamma table 0x604f4478 [V camera].

---

## 7. CPU access to image buffers

Details: [GRAIN_PORT.md](GRAIN_PORT.md) "CPU grain" sections (t1, d1–d3, s1–s3, r1/r2). The method follows the OM-3
port (credited in `NOTICE`); the E-P7 code is our own.

- **Buffer records** [V]: job+6592 holds 48 records of 88 B (lookup 0x60350975). Fields: +0 bus address, +8 format,
  +10 stride in px, +12 width, +14 height, +16/+18 active size, +20/+22 x/y, +72 size. Buffer 0x1000 is the
  developed image, format 0x0302 at 2 B/px; Y is in the even bytes [V camera].
- **Window mapper 0x60067146** [V emulator]:
  - Bus addresses below 0xb0000000 are direct. 0xb0000000–0xdfffffff are mapped through a per-CPU window (registers
    at 0x98100058 plus a CPU stride; mode array 0xa2716034; get/set 0x600672ee / 0x600672fe).
  - It refuses 0xc0000000–0xc3ffffff and anything from 0xe0000000.
  - A full-size buffer can **cross a window boundary**. The real one crossed 0xd0000000 (mode 3 below, mode 4
    above), so passes go row by row with their own lock per piece.
  - 0x40000000 and up is the logical flash range. **Never write there** [I brick risk].
- **Lock, flush, restore** [V]:
  - Lock 0x6006a922 and unlock 0x6006a96b mask this CPU's interrupts through PSW. Other CPUs keep running.
  - Cache flush is 0x60066fc0. Its busy bit needs hardware, so the emulator only counts calls.
  - Restore the previous window mode before each unlock, because another task on this CPU may rely on it.
- **Timing:** wait for the producing request (0x60354930) before reading. The blend setup 0x6035ca31 returns with
  its buffer complete [V emulator].
- **Cost:** about 15 instructions per pixel, so a few seconds per 20 MP photo [V camera "decently slow but
  workable"]. Any new per-pixel CPU effect costs the same order of time and blocks the capture task.
- **What crashed:** reading buffer 0x1060 through its descriptor address directly, without the mapper. The camera
  crashed about 1 s after the shot ([HALATION_RED.md](HALATION_RED.md) §10e). **Never CPU-read an image buffer
  through a raw descriptor address.**

---

## 8. SD card I/O from firmware

Details: [SD_BUNDLES.md](SD_BUNDLES.md) (API, format), [SD_BUNDLES_V2.md](SD_BUNDLES_V2.md) (trigger),
[SD_BUNDLES_V3.md](SD_BUNDLES_V3.md) (heap state), [SD_V2_CRASH.md](SD_V2_CRASH.md), [LOOK_EDITOR.md](LOOK_EDITOR.md)
§6 (writes), [FILM_MENU_V2.md](FILM_MENU_V2.md), [MOVIE_COLOUR.md](MOVIE_COLOUR.md) "Movie slot".

**File API** (section 4) [V camera]:

| Call | Address | Frame | Arguments |
|---|---|---|---|
| fopen | 0x60634fbc | `[],4` | a0 path (`C:\EP7MOD\…`), a1 mode "r" / "w" → a0 handle or 0 |
| fclose | 0x60634fc5 | `[d2,a2,a3],16` | a0 handle |
| fread | 0x6063514b | `[d2,d3,a2,a3],24` | a0 buf, d0 size, d1 count, a1 handle → d0 |
| fwrite | 0x60635041 | – | as fread |
| ftell | 0x606351d9 | `[d2,a2,a3],16` | → d0 position |
| fseek | 0x60635243 | `[d2,d3,a2,a3],20` | d0 offset, d1 whence: **2 = SET, 0 = CUR, 1 = END** |

- From any ordinary CPU0 task, each call is a **synchronous RPC to the FILECLBK task** (task 16) that waits with
  no timeout. So never call it from a task FILECLBK might wait on [V].
- Stock does the same sequence when it loads Linux from the card (0x6005a509) [V].

**Trigger: the first shutter half-press** [V camera].
- The hook is at **0x602c805f**, in the HLS_EVENT task's trigger tracker. It replays `call 0x602c818c,[d2,d3,a2,a3],36`.
- It runs on every half-press event 0x02000100, so the gate must be cheap:
  - state 0 (load not yet tried);
  - all 9 sections loaded (0xa272e420 & 0x1ff);
  - the caller is HLS_EVENT (tid at 0x622cbcc0);
  - a medium reports MOUNT OK (0xF000).
- **Nothing is read at boot.** No card, or no `EP7MOD` folder, means stock behaviour. Saves run on a later
  half-press in the same context (`sd_back` at 0x605b7420).
- The first loader (v1) hooked the media "MOUNT OK" path. That path turned out to be dead, so the loader never ran
  ([REVIEW_v04.md](REVIEW_v04.md)). Prove reachability before you trust a hook.

**Card files** (all little-endian, CRC-32 zlib, under `EP7MOD/`):

| File | Size | Magic | Content |
|---|---|---|---|
| `BUNDLES.BIN` | 544 or 680 B | `EP7B` | 32-B header (version, build 1100, main SHA prefix) + 8 slots × 64 B (+ names). Slot layout: `patches/bundles/layout.json` |
| `LOOKS.BIN` | varies | – | The look library the Film page steps through. Format: `webapp/README.md`, `webapp/src/format.ts`, `tools/ep7_looks_bin.py` |
| `MOVIE.BIN` | 96 B | `EP7V` | One 64-B slot in COLOR layout + a 16-B name |

`NAMES.BIN` and `LABELS.BIN` are the OM-3 port's files (rendered look names and value labels), not the E-P7's
([OM3_NOTES.md](OM3_NOTES.md)).

**Rules:**
- **Range-check every byte**, in the loader and again in every reader. A torn or old file must always give a valid
  look or stock, never a bad pointer.
- **The loader must accept every byte the UI can save.** The first v0.26 build saved slot +49 = 1 after a Film pick,
  but its MOVIE.BIN loader required +49..63 = 0. From the next power-on the camera rejected its own file
  ([MOVIE_COLOUR.md](MOVIE_COLOUR.md)). When you add a field, update the loader's range table, the zero check, the
  tools and the Control Panel together.
- **Save order:** write `.NEW` and read it back, copy the old `.BIN` to `.BAK`, then write `.BIN` and read it back.
  Clear the dirty flag only after a verified write [V camera]. One test card got FAT corruption on a last shot in
  test 11, cause never found, so keep writes rare and recoverable.
- Older firmware rejects files with new fields set. Tell users to turn the field off before going back.

---

## 9. Hook techniques

What works on this firmware. Examples: [CODE_SPACE_MAP.md](CODE_SPACE_MAP.md) "Hook sites"; patterns in
`patches/*/build.py` (for example `patches/sharp_off/build.py`, `patches/aspect_fix/build.py`).

| Technique | How | Examples |
|---|---|---|
| **rel32 call retarget** | Change the 4-byte displacement of a `call d32` (`dd` rel32 regs imm8) at site+1. The stub runs on the caller's frame and tail-jumps to the old target on a miss | WB getter ×14, shading/NR/Sharpness reads (`sc_get`, `nr_get`, `sh_get`), `call ed_poly` |
| **jmp stub** | Replace ≥ 5 whole instructions with `jmp d32` (`dc` rel32) + `nop` padding. The stub replays the displaced instructions **exactly**, then jumps back to site+N | H/L 0x603b02d9, loader 0x602c805f, label builders 0x60df255f/0x60df25dc |
| **Data-only** | Re-point a vtable word, a record word, a `.data` init-image word, or a list pointer and count. Change a label kind. In-place OSD bitmaps | facade 0x60acb0e4, COLOR records +12/+16/+44, 0x6025aa98, validator lists, Button Function rows |
| **Chaining** | When a word is already ours, re-point it and fall through to its previous owner, with registers, stack and MDR unchanged | look_extras over look_editor; sharp_off over the movie message hook |

Constraints, each enforced by the build scripts:
- **Check the site.** Every edit carries the stock `expect` bytes. Confirm with `tools/ep7_xref.py --short` that
  nothing branches into bytes +1..+N−1 of a jmp site.
- **Register contract.** d0/d1/a0/a1 are arguments and scratch. `call addr,[regs],frame` saves the listed
  registers and sets **MDR** to the return address. Code that leaves by `retf` needs MDR intact, so don't use
  `mul`, `div` or `ext` there. `setlb` uses LIR/LAR. The builds ban mul/div/setlb in hook code by default.
- **Flags.** If the displaced instructions end in a compare and branch, replay them so PSW is right at the resume
  point. Otherwise show that the flags are dead there.
- The verifiers compare d0–d3, a0–a3, sp, MDR, PSW and the live stack against stock at the resume point.
- **No absolute stores into the loaded image** (0x60020020–0x6191ffe0). State goes in heap P or in proven .bss
  (0x622cbcc2, plus a 1-B padding latch at 0x6203c8ba). The builds run a store audit, and the verifiers map the
  image read-only.
- **Align data.** Put `.balign 4` before `.long` tables and `.balign 2` before `.hword`. A misaligned word read
  crashed the camera (§10). Build checks: label alignment and traced accesses.
- **No `fb`-prefixed instruction forms.** They misbehaved on the OM-3 camera; the builds reject any `0xfb` opcode
  byte.
- **Section order.** A hook calls only code in its own section or an earlier-loaded one (§2).
- **Prefer reads.** Most Caduceus features are getter redirects that change a value at the moment it is read. They
  don't change stock state.
- **Every miss path is stock.** No card, no file, a bad file or an unknown job must all run stock code with stock
  registers.

---

## 10. Safety lessons

Each lesson has the incident behind it.

| # | Incident | Lesson |
|---|---|---|
| 1 | **Test 3 bootloop** ([BOOTLOOP_T3.md](BOOTLOOP_T3.md)): seven feature groups in one flash. The prime suspect was the aperture-ring patch, which runs at power-on on every lens request and stored into section-1 RAM. Recovered by battery out, then OK + power with the stock restore [V camera] | Add **one feature group per flash**. Anything that runs before the first shot is high risk. Bisect from the last good build. The SD restore can work from a bootloop, but that is proven only once |
| 2 | **Test 7a**: a byte store into section-3 RAM crashed the camera on the first half-press. Probe P2, with the same path storing to .bss 0x622cbcc2, passed ([SD_V2_CRASH.md](SD_V2_CRASH.md)) | Never store into the loaded image. When a crash has several new operations, flash a probe that isolates one |
| 3 | **Grain s1**: opening Extras crashed on a misaligned word read at 0x601e356c. The emulator didn't catch it ([GRAIN_PORT.md](GRAIN_PORT.md) s2) | `.balign` every data table. Add a static alignment check, and trace every load and store in the emulator to check its alignment |
| 4 | **Grain s2**: no grain, because the work list is built inside the constructor **before** the converter writes the marker. The emulator fixtures had set the marker in advance. This is probably also why v0.4–v0.21 grain never showed on full-size photos [I] | Fixtures must follow the real call order. A guard readout on the card (GRAIND.BIN) found it in one shot |
| 5 | **OM-3 bootloop** (its report): a section-2 hook called section-8 code that wasn't loaded yet ([OM3_NOTES.md](OM3_NOTES.md)) | Hook targets go in the same section or an earlier-loaded one. Load order is body-specific: read the descriptor table |
| 6 | **Image-buffer read**: a probe read buffer 0x1060 through its descriptor address, and the camera crashed about 1 s after the shot ([HALATION_RED.md](HALATION_RED.md) §10e) | Use the window mapper, the lock and the flush (§7), or don't touch the buffer |
| 7 | **Red mask probe**: a 0x0305 buffer fed to the mask port gave a garbled tile grid (§10b) | Hardware ports have formats the CPU side can't check. Copy a stock descriptor shape exactly |
| 8 | **COLOR Blur** (#20, [ISSUES_19_20.md](ISSUES_19_20.md)): it restarted the camera. Key-11 jobs skip the stock flag strips, so Blur reached job types stock never gives it | Injecting a stock feature into a new mode skips that feature's preconditions. Blur was removed |
| 9 | **Firmware screen**: "Body Caduceus" clipped and the value showed ": 1.1" (v0.15) | Widget widths aren't known. Change values, not labels, and keep text short |
| 10 | **MOVIE.BIN +49**: the loader rejected the file the camera itself had saved (§8) | Loaders accept every byte the UI can write. Test the save → power cycle → load loop |
| 11 | **View Assist** (v0.14): its display LUT was baked into recordings, cancelling log. Removed ([LOG_VIEW_ASSIST.md](LOG_VIEW_ASSIST.md)) | Display paths may feed the encoder. Test every output, not just the screen |
| 12 | **Keys that never arrive**: Movie and Delete never reach the COLOR editor, and MENU/PLAYBACK close it ([LOOK_EDITOR.md](LOOK_EDITOR.md) §6) | The emulator doesn't run the screen stack. Put a key probe in a label, then build on the key that arrives |
| 13 | **LED misread**: code analysis said the 2 Hz blink meant failure, but it means success ([SD_REJECT.md](SD_REJECT.md)) | Settle hardware behaviour with a harmless camera probe (a one-string OSD change) |
| 14 | **Dead hook**: SD loader v1 hooked a path the app never takes (REVIEW_v04) | Trace the path from a real event before you build on a hook |

**Emulator blind spots, and how to compensate:**
- **What it doesn't do:** fault on alignment, run the screen stack or renderer, model the cache-flush busy bit or
  DMA, show ISP pixel results, or model fopen timing.
- **What to do instead:**
  - Add static checks (alignment, store audit, forbidden opcodes).
  - Write diagnostics the camera can report:
    - a record shown on the Firmware screen (grain d1);
    - a card file written on half-press (`GRAIND.BIN`, `LOGDBG.TXT`);
    - a probe text in a guide label.
  - Measure camera JPEGs and makernotes, and frame-grab clips with ffmpeg.
  - Remove the diagnostics before release.

---

## 11. Toolchain and workflow

- **Disassembler:** GNU binutils 2.45 for `mn10300-elf`, built by `tools/setup_toolchain.sh` (signature-checked).
  `tools/dis.sh START STOP` disassembles main by CPU address (`objdump -b binary -m am33-2 --adjust-vma=0x60020020`).
  The assembler and linker from the same build assemble the `.s.in` sources.
- **Decode:** `tools/fetch_stock.sh` downloads and hash-checks the stock file. `tools/decode_stock.sh` decodes it into
  `analysis/ep7_1100/` and checks every component hash against `docs/ep7_1100_components.sha256`.
- **Search:**
  - `tools/ep7_xref.py` finds abs32 values and call/jmp/branch encodings at every byte offset. Expect false
    positives, and confirm with `dis.sh`.
  - `tools/ep7_code_space.py` audits section tails.
  - `tools/osd_extract.py` decodes the OSD.
- **Emulator:** fishcu's MN103 simulator harness (`external/oly_fw_analysis`, a git submodule) runs real firmware
  functions on synthetic inputs. Run it with a memory cap (`systemd-run --user --scope -p MemoryMax=6G …`), one
  heavy job at a time. Its limits are listed in §10.
- **A patch** is a folder in `patches/` with a `.s.in` source (placeholders filled by `build.py`) and a `build.py`
  that:
  1. checks the stock bytes at every site and that the claimed range is free;
  2. assembles, disassembles and audits the code (forbidden opcodes, `fb` forms, absolute stores, alignment, fit);
  3. emits or layers an `edits.json` (`{components, edits: [{kind, addr, expect, new, why}]}`).
  Most builds are `apply(spec)` layers on the previous release.
- **Layered specs.** `tools/merge_specs.py` merges specs and rejects duplicate addresses.
  `tools/build_package.py SPEC --out DIR` builds the package and the restore, and refuses on any check failure:
  - the stock hash, or the exact container round trip;
  - an `expect` mismatch, or overlapping edits;
  - changed metadata;
  - a changed-byte count that differs from the sum of the edits;
  - a restore that isn't exact stock.
  Outputs are named `*.pkg.bin`, never SD-loadable names.
- **Verify pattern.** Every patch has a verifier (`patches/*/verify.py`, `tools/ep7_*verify*.py`) that runs the real
  patched code against stock in the emulator and checks that only the intended values change. Keep it **minimal
  plus anti-brick**: the feature's own checks, plus P (package parses, decoded == stock + edits, restore == stock,
  no stray bytes). Add deeper runs or mutation tests only when the risk is high.
- **Releases are edit lists.** `releases/vX.YY/` holds `edits.json`, `build.json`, `SHA256SUMS` and the card files.
  `caduceus.py build --release vX.YY` downloads stock, applies the edits with the same checks, and writes nothing
  unless every output hash matches `SHA256SUMS`.
- **Never distribute OM firmware**: no stock files, decoded images or packages in git. `stock_firmware/` and
  `analysis/` stay local, and `*.pkg.bin` packages are never committed.

---

## 12. Porting checklist

Start with [BODIES.md](BODIES.md) (where each body stands, and the test order on a new body). Then read
[PORT_EM10IV.md](PORT_EM10IV.md) and [PORT_EM5III.md](PORT_EM5III.md) (worked first-glance assessments), and
[OM3_NOTES.md](OM3_NOTES.md) (lessons from a finished port).

**1. Get and decode the firmware.**
- Fetch the body's file from OM's catalogue (`external/oly_fw_analysis/tools/om_firmware_catalog.py`, or the request
  in `tools/fetch_stock.sh` with the body's model code). Keep it out of git.
- Decode with `oly_container.py` and check the round trip is byte-identical. The E-M10 IV has the same layout as
  the E-P7; the E-M5 III and E-M1 III differ in sizes and addresses [V].

**2. Find the skeleton.**
- **Section table and end getter:** find the loader by its shape (nine `{start, src, dst}` entries, a jump table of
  `mov imm32,d0`). Then the load order, the FF tails, and the loaded-mask word.
- **SD updater:** search for the `E1xx????BIN` pattern. The model code gives the card name (E-M10 IV:
  `E1409999.BIN` is likely [I], unconfirmed).
- **Static validator:** search for the 20-B record shape `{u16 sid, u16 attr, u32 list, …}` with sids in order. It is
  at 0x604ceed4 on the E-M10 IV, with 1214 records; older bodies (E-M5 III, E-M1 III) don't have it in this form
  [V].

**3. Map functions.** Techniques that worked:
- **Signature match:** `tools/port_sigmatch.py TARGET_MAIN [edits.json]` looks for each E-P7 edit's bytes plus
  32/16/8 bytes of context. E-M10 IV 47 %, E-M5 III 36 %, OM-3 26 %, OM-1/OM-5 22–28 % ([BODIES.md](BODIES.md)). A
  match means the code looks alike, not that the edit is correct.
- **Shape match** (how the PEN-F → E-P7 map in [HSL_PORT.md](HSL_PORT.md) was made, `tools/ep7_port_map.py`):
  - the same function length;
  - the call at the same offset inside it;
  - the same preceding instructions (`add 1816,a3; mov TABLE,a0`);
  - the only caller of a function.
  Expect struct offsets to change (job size 14920 → 15488, request 1132 → 1884) and sids to shift (PEN-F 931 → E-P7
  927).
- **String anchors:** debug strings (`FIRMUP_CLI`, `OlyHfwEventManager`, `Lv_Control_Mode_Viewer`,
  `BackGroundCtrl::drawC3dAsync`, `COLORTONE_*`, `PROFILE`) lead to code by xref.
- **Table shapes:** for UI lists, stride-search for stock value sequences (1,3,2,4,9 for aspect) and string-id
  sequences. Validator records lead to sids, and sids to the registry and descriptor callbacks.
- **Mask PC-relative operands and pointers** when matching, or shifted code won't match.

**4. Decide what's body-specific.**
- **Code space:** use only FF tails until you have proved dead tables. **On a body with an EVF, don't reuse the
  E-P7's dead EVF gamma tables.**
- **UI:** the OSD (0x101) differs between bodies (16.6 M bytes differ on the E-M10 IV), so every bitmap, string
  and menu id must be re-derived. Mode dials, EVFs and extra buttons change the screens.
- **The COLOR profile system** (sids 1645–1707, the COLOR1–4 editors, key 11) is E-P7/PEN-F/OM-3 territory. The
  E-M10 IV, E-M5 III and E-M1 III don't have it; the OM-1/OM-5 have partial profile settings without the screens.
- **Features that need profiles:** looks per slot, the on-camera editor and Film page, Extras, bundles, per-profile
  WB/NR/shading/sharpening/grain, the Movie slot and the mono presets. On a body without profiles they need a new
  host (Art Filter, picture mode, Color Creator or a My Set). That is a redesign, not a relocation.
- **Features that stand alone:** recording limit, log video (if the hidden 2000/2001 modes exist), LCD tint,
  aspect tables, the Firmware-screen version label, button lists and video rotation.

**5. Test on the body**, in the order of [BODIES.md](BODIES.md), with an owner who has read the risks:
1. the official file through the updater;
2. a stock restore round trip;
3. a harmless text change (the Firmware screen) plus a restore;
4. a data edit;
5. a do-nothing hook;
6. features one at a time.
Keep the restore file for every build. Never let anyone flash an E-P7 build on another body.

**6. Join the shared Control Panel** (per [`../AGENTS.md`](../AGENTS.md)). Don't fork it.
- Add a camera entry in `webapp/src/cameras.ts` (ranges, which controls exist).
- Add an export target in `webapp/src/targets/` (translation to the body's card files), listed in
  `targets/index.ts`.
- Keep the look format and share codes shared. Body differences belong in the profile and the export translation.
- Vendored files from a port stay unchanged with a source line (`webapp/src/om3.ts`), and credit goes in `NOTICE`.
- Open question: the OM-3 and E-P7 camera tests point opposite ways for a positive hue value
  ([OM3_NOTES.md](OM3_NOTES.md)).

---

## 13. Licence and etiquette

- **Caduceus is Apache-2.0** ([`../LICENSE`](../LICENSE), [`../NOTICE`](../NOTICE)). Ports may reuse code and docs.
  Keep the `NOTICE` and add your own credits.
- **Credit ports and contributors in `NOTICE`.** Two examples: the CPU grain method from the OM-3 port, and the 65:24
  AF fixes adapted from flickering-star's fork. Credit community film recipes to their authors
  ([LIBRARY_V1.md](LIBRARY_V1.md)).
- **fishcu/oly_fw_analysis** is a submodule used by the developer verification tools only. Its code is GPL-3.0 and
  keeps that licence. Caduceus doesn't need it to build or install, and doesn't copy its code. Other projects'
  patches (PEN-F HSL, aperture-ring PR) were read as maps, and our code was written fresh.
- **No OM firmware in the repo**, ever: no stock files, decoded components or built packages. Ship edit lists and
  a builder that downloads from OM.
- Not affiliated with OM Digital Solutions. "Olympus", "PEN" and "OM SYSTEM" are their owners' trademarks.
