---
name: caduceus
description: How to work in the Caduceus repo, which patches Olympus / OM System camera firmware (PEN E-P7, OM-3, OM-5, and other bodies). Use this skill for ANY task here - firmware analysis or disassembly, writing or verifying a patch, porting a feature to a new body, building or checking an update package, emulator runs (fishcu MN103 engine, om-emu), SD-card updates, camera test steps (C1-C5), releases and SHA256SUMS, the Control Panel (webapp) and film looks, or editing docs/PORT_*.md, WORKLOG.md, CHANGELOG.md. Read it before touching tools/, patches/, webapp/src/targets or stock_firmware, even for a "small" change.
---

# Caduceus: firmware patches for Olympus / OM System cameras

The repo turns a camera's official update file into a patched one (film looks, on-camera editor, aspect ratios,
recording limit, aperture ring and more), with one Control Panel and one look format for every camera. The E-P7 is
the reference body. The OM-3 has an outside port, and the OM-5 is being ported on branch `om5-port`.

A bad flash can leave a camera in a boot loop. Everything below exists so that a package reaches a camera only after
it has been checked against the bytes and run in an emulator, and so that a restore file always exists.

**Precedence.** `AGENTS.md` and `docs/FIRMWARE_GUIDE.md` outrank this skill. If they disagree with anything here, they
win and this skill is the one to fix. For patching or porting, read the guide first: it holds the E-P7 firmware
facts, the hook techniques (section 9) and the incident table behind each safety rule (section 10). The skill only
adds the OM-5 and workflow details on top.

## Hard rules

1. **Never commit OM firmware bytes** or anything under `analysis/`, `stock_firmware/`, `out/`, and never a built
   `*.pkg.bin` package or a decoded component. They are gitignored; check with `git check-ignore` before adding a new
   output folder. Firmware belongs to the manufacturer; the repo ships edit lists and a builder that downloads the
   official file and checks its SHA-256.
2. **Never touch a camera.** Do not put files with an SD-update name (`E1xx9999.BIN`) where a camera could read them.
   Build into `out/` or a scratch directory. Camera steps are done by the user, one at a time, after your review.
3. **Constants are per body.** Base address, section tails, setting ids and model code all differ. Take them from
   `tools/bodies.py` and `references/hardware.md`, never from memory of another body. A wrong base shifts every
   derived address by 0x1000 and still looks plausible.
4. **Emulator before camera, one feature per flash, a restore file with every package.** The E-P7 test 3 bootloop
   came from flashing seven feature groups at once (`docs/BOOTLOOP_T3.md`); one feature per flash is the only way to
   find the culprit.
5. **Work on a branch.** Never commit on `main`; never push or amend unless asked. Commit each verified step.
6. **Check results yourself.** Rerun the command and compare hashes before you commit or report. A subagent saying
   "all pass" is not a result.
7. **Tag evidence** in docs and WORKLOG: `[V]` verified (disassembly or a native emulator run, with addresses), `[I]`
   inference, `[C]` seen on a camera (who, when), `[R]` reported by someone else. A signature match is not `[V]`: it
   says code looks alike, not that an edit is safe.
8. **Don't add OM-3 features to the E-P7** until they can be tested here (`docs/OM3_NOTES.md`).

## One Control Panel for every camera (AGENTS.md)

- Add a camera, don't fork the panel: one entry in `webapp/src/cameras.ts`, one export target in
  `webapp/src/targets/` (listed in `targets/index.ts`).
- Keep the pack format and share codes shared. Camera differences live in the profile and the export translation.
  Keep a wider value in the look until the user moves that control.
- Files copied from another port stay unchanged with a source line (`webapp/src/om3.ts`, `om3names.ts`); our glue
  goes in `targets/`; credit goes in `NOTICE`.
- Firmware stays with its port: each port keeps its own repo, install guide and bug reports. The About dialog and
  `docs/BODIES.md` point users to the right one. The OM-5 port has its own fork, https://github.com/tadejs/caduceus-om5;
  OM-5 firmware work belongs there. Branch `om5-port` here is the earlier home of that work.
- For the OM-3, `androidfanman/caduceus-om3`'s panel retires into this one only once that is agreed with its author.
  Don't announce or act on it before then.

## Patch safety rules (from `docs/FIRMWARE_GUIDE.md` sections 9 and 10)

- **No absolute stores into the loaded image.** State lives in heap P or proven .bss. Stock never stores there, and the
  one build that did crashed.
- **`.balign 4` before `.long` tables, `.balign 2` before `.hword`.** A misaligned read crashed a camera and the
  emulator did not catch it.
- **No `fb`-prefixed opcodes** in hook code (the builds reject any `0xfb` byte). Keep `mul`, `div`, `ext` and `setlb`
  out of hooks that leave by `retf`, because MDR must stay intact.
- **Replay displaced instructions exactly** and confirm nothing branches into the middle of a jmp site
  (`tools/ep7_xref.py --short`). Preserve flags when the displaced code ends in compare and branch.
- **Every miss path is stock**: no card, no file, a bad file or an unknown job runs stock code with stock registers.
- **Never CPU-read an image buffer through a raw descriptor address.** Use the window mapper, lock and flush.
- **Card files:** range-check every byte in the loader and in every reader, and make the loader accept every byte the
  UI can save (the MOVIE.BIN +49 incident). Save order: `.NEW` and read back, old to `.BAK`, then `.BIN` and read back.
- **Injecting a stock feature into a new mode skips its preconditions** (COLOR Blur restarted the camera).
- **Prove a hook is reachable** from a real event before building on it (SD loader v1 hooked a dead path).
- **Prefer getter redirects** over changing stock state, and keep the 16-byte FF guards around claimed code space.

## Where things are

| Need | Look at |
|---|---|
| E-P7 firmware facts, hook techniques, incident table, porting checklist | `docs/FIRMWARE_GUIDE.md` (read first) |
| Body facts (stock name/SHA, model, base, update name, card folder) | `tools/bodies.py` (`EP7`, `OM5`); `caduceus.py` keeps a minimal copy, keep both in step |
| Build a package | `python -I tools/build_package.py SPEC --body om5 --out DIR [--recompress-all] [--stock FILE]`; writes `patched.pkg.bin`, `restore-stock.pkg.bin`, `build.json` |
| Release build | `python caduceus.py build --release v0.29a` must reproduce `releases/v0.29a/SHA256SUMS` |
| LZ4 spans (OM-5) | `tools/olycompress.py` (`expand`, `info`, `roundtrip`) |
| Decode a stock file | `external/oly_fw_analysis/tools/oly_container.py` (fishcu submodule); output goes to `analysis/` |
| Match E-P7 edits on another body | `tools/port_sigmatch.py`, `tools/ep7_port_map.py` |
| Who owns which code bytes | `docs/CODE_SPACE_MAP.md` (claim a range before using it) |
| Patch folders | `patches/<feature>/build.py`; OM-5 toolchain and claims in `patches/om5_common/` |
| Verifiers | `tools/ep7_verify_*.py`, `tools/om5_verify_*.py` |
| Port status per body | `docs/BODIES.md`, `docs/PORT_OM5.md`, `docs/PORT_EM5III.md`, `docs/PORT_EM10IV.md` |
| History and camera results | `WORKLOG.md`, `CHANGELOG.md` |

## Workflow for a task

1. **Read first.** Open the relevant `docs/` page and the WORKLOG entries for the feature. Most questions already
   have an answer, often a negative result you should not repeat.
2. **Find the facts in the bytes** (`references/methodology.md`): the right base, the expanded image on the OM-5,
   the function by signature, then disassembly to confirm.
3. **Write the patch** as `patches/<name>/build.py` that asserts the stock bytes it replaces and emits an
   `edits.json` fragment. Claim code space in `docs/CODE_SPACE_MAP.md`. Hook code in an early section must not call
   into a later section: it is not loaded yet (see the hardware reference).
4. **Write a verifier** in the D/T/X/E pattern (`references/testing.md`) and run it under WSL.
5. **Run an emulator**: om-emu for a whole boot and UI, the fishcu engine for single routines.
6. **Package** with `build_package.py`; check restore == stock; record hashes.
7. **Camera**: give the user a card folder and a one-page checklist. Record the result in WORKLOG with `[C]`.
8. **Document** what was found, including what failed, and commit.

## Environment gotchas

- Windows host, Git Bash. Python is stdlib only; run with `python -I` so a stray `json.py` in the cwd cannot hijack
  imports. Emulator scripts need Linux: run them in WSL Ubuntu with `~/ep7fw-toolchains/venv/bin/python -I` from
  `/mnt/c/Users/tadej/workspace/caduceus-ep7`.
- Pass WSL commands as script files: `MSYS_NO_PATHCONV=1 wsl -d Ubuntu -- bash /mnt/c/.../x.sh`. Inline
  `bash -lc '...'` loses `$VARS` between the layers.
- **CRLF.** Shell scripts need LF to run in WSL (`tools/*.sh` such as `setup_toolchain.sh`: `sed -i 's/\r$//'`
  first). Clone om-emu inside WSL; a Windows clone gets CRLF and one test fails. In the other direction, release
  files checked out under `core.autocrlf` get CRLFs and `caduceus.py`'s SHA256SUMS check then fails: compare hashes
  on the bytes, not on a converted checkout.
- The fishcu MN103 engine is a Linux binary at `external/oly_fw_analysis/toolchains/` (ignored), built from the
  binutils-gdb commit fishcu pins, `87ffb882`, after checking pristine file hashes (the tarball hash is not reproducible). The MN103 binutils
  (`mn10300-elf-objdump`, `-as`, `-ld`) are in `~/ep7fw-toolchains/mn10300/bin`.
- Long emulator runs (hundreds of millions of instructions) go in the background with a generous timeout, one heavy
  job at a time, under a memory cap (`systemd-run --user --scope -p MemoryMax=6G ...`).
- Build output directories must be new and empty. Never overwrite a stock file.

## References

- `references/methodology.md`: how to analyse a firmware for a new body.
- `references/hardware.md`: body table, container, base addresses, sections, SD updater facts.
- `references/features.md`: each feature, patch folder, key sites, status per body.
- `references/testing.md`: verifier pattern, emulators, camera ladder, recovery.
- `evals/`: see the note there; cases are added as the skill gets used.
