# Test procedure

Rule of thumb: a package must pass every desk check and an emulator before the user flashes it, and the user flashes
one small, single-purpose step at a time.

## 1. Verifier pattern: D / T / X / E

Model: `tools/ep7_verify_lcd_tint.py`, `tools/om5_verify_fw_version.py`. One script per patch, offline, emulator only,
run in WSL (`~/ep7fw-toolchains/venv/bin/python -I tools/<verifier>.py [edits.json]`). Four sections, each printing
PASS/FAIL, ending `ALL PASS`:

- **D diff**: build the package with `build_package.py`; its decoded (and, on the OM-5, expanded) main equals stock
  plus the spec's edits and differs at no other byte; other components identical; restore == stock; the spec equals a
  fresh run of the patch's `build.py` (stock bytes asserted again). On the OM-5 also check only the edited LZ4 spans
  were re-encoded.
- **T targets**: every hooked site was the stock instruction you believe (same call target and register list); data
  you rewrite had the stock value.
- **X refs**: every reference to the touched bytes is accounted for (no branch into the middle of a hook, a rewritten
  descriptor is referenced only where expected, nothing points into the free space you used, the original callee's
  other callers are untouched).
- **E emulator**: run the patched routine on the fishcu MN103 engine on stock and patched with the downstream calls
  stubbed to capture arguments; registers, stack and arguments equal stock except what you mean to change; no fault.
  Keep the verifier **minimal plus anti-brick** (`docs/FIRMWARE_GUIDE.md` section 11): the feature's own checks, plus
  the package checks in D. Add deeper runs and mutation checks (break one edit, the verifier must fail) only when the
  risk is high, for example a hook that runs at power-on.
- **Emulator blind spots** (guide section 10): it does not fault on misaligned access, run the screen stack or
  renderer, model the cache-flush busy bit or DMA, or show ISP pixels. Cover them with static checks (alignment, store
  audit, forbidden opcodes) and with a diagnostic the camera can report back (a record on the Firmware screen, a card
  file written at half-press, a probe text in a label). Remove diagnostics before release. Fixtures must follow the
  real call order (grain s2 failed because the fixture had set the marker early).
- Run heavy emulator jobs one at a time with a memory cap (`systemd-run --user --scope -p MemoryMax=6G ...`).

Other gates: `tools/ep7_verify_package.py` (container checks), `tools/test_olycompress.py` (LZ4 round trip), and the
**regression gate**: `python caduceus.py build --release v0.29a` (or `build_package.py` on the release spec) must
reproduce `releases/v0.29a/SHA256SUMS` exactly after any builder change. Keep the E-P7 default unchanged.

## 2. SD updater probe (container acceptance)

`tools/om5_sd_update_probe.py` (E-P7: `tools/ep7_sd_update_probe.py`) runs the camera's own validator in the fishcu
engine on update containers held in emulator RAM. Run under WSL:
`~/ep7fw-toolchains/venv/bin/python -I tools/om5_sd_update_probe.py --patched PKG --restore RESTORE_PKG`.
It must end `ALL PASS`. The full stock file costs about 570M instructions (chained 100M slices): run it in the
background with a long timeout. It proves acceptance and write addresses, not that the camera boots.

## 3. om-emu (whole firmware boot and UI)

[flatpanda/om-emu](https://github.com/flatpanda/om-emu) in WSL at `/home/tadej/omemu` (Node 26 inside WSL; Windows
Node fails on paths). Setup and results: `docs/PORT_OM5.md` section "Emulator (om-emu)".
- `node emu/bin/test-pure.mjs` and `node emu/bin/modelcheck.mjs E144 --no-port` (E142 for the E-P7): boots, S1, S2,
  PLAY, INFO, MENU, formats an SDHC card.
- **Limits.** `nav.mjs` fails on E144. om-emu applies patches to the *stored* block, so for an OM-5 package whose
  edits sit in an LZ4 span use `python -I tools/om5_omemu_patch.py PACKAGE OUT.json` to convert it into an om-emu
  patch (edits against the expanded image), then run `debug.mjs --patch OUT.json`. The walk to Setup > Firmware is:
  MENU, DOWN x5, RIGHT, DOWN x6, OK. Compare frames to stock.
- Results so far: the C2 package (both spans recompressed) boots and matches stock on all 17 frames; the C3 package
  shows "Caduceus : 0.1" on the firmware screen.
- An emulator pass is a gate, not a camera pass: it does not model every peripheral (display controller, LUKE).

## 4. The camera ladder (per body)

| Step | Package | Emulator gate first | On the camera you check |
|---|---|---|---|
| C1 | Stock firmware, untouched, as the update file | validator probe accepts it | steady light then blink; camera normal; firmware version unchanged |
| C2 | Stock content, compressed spans re-encoded by us (OM-5 only) | om-emu boots it, expanded image == stock | same as C1: proves the bootloader accepts our LZ4 |
| C3 | Firmware-screen label change | om-emu walk shows the label | label shows; a test shot is fine; restore works |
| C4 | A no-op hook in a section tail | om-emu boot plus S1/S2 with the hook traced as hit | camera normal |
| C5 | SD probe: read a card file at half-press | om-emu with the card file present / absent | camera normal; probe state readable |

Then features, one per flash, ordered by risk (standalone ones first; the aperture ring, which runs from power-on,
last and alone). Every package ships with a hash-checked `restore/E<model>9999.BIN` and a `SHA256SUMS`.

## 5. Card folders and checklists

Layout for the SD card root: `DCOLYMP/E1449999.BIN` (the update) and `restore/E1449999.BIN` (the official stock file;
copy it to `DCOLYMP/` to go back). Verify with `sha256sum -c SHA256SUMS`. The one-page checklist for the user covers
what to copy, how to trigger (battery full, camera off, hold OK, switch on, keep holding until the light comes on),
what success looks like (steady, then blink about twice a second, then normal boot), what rejection looks like (fast
blink with no steady phase; nothing written), what to check after, how to go back, and deleting the update file from
the card afterwards (otherwise the next trigger flashes it again). The model is `out/om5-camera-checklist.md`.

## 6. Recovery

- Wrong trigger or rejected file: nothing was written; the camera boots as before.
- Bootloop after a flash: remove the battery, hold OK, switch on, flash the stock restore file. Proven on the E-P7
  once (test 3); unproven on other bodies. Keep OM Workspace installed on a computer as the official USB route.
- After any camera result, write a WORKLOG entry tagged `[C]` with the package hash, what was seen, and the next step.
