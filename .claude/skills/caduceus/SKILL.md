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

## Hard rules

1. **Never commit OM firmware bytes** or anything under `analysis/`, `stock_firmware/`, `out/`. They are gitignored;
   check with `git check-ignore` before adding a new output folder. Firmware belongs to the manufacturer.
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
  `docs/BODIES.md` point users to the right one.

## Where things are

| Need | Look at |
|---|---|
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
- Long emulator runs (hundreds of millions of instructions) go in the background with a generous timeout.
- Build output directories must be new and empty. Never overwrite a stock file.

## References

- `references/methodology.md`: how to analyse a firmware for a new body.
- `references/hardware.md`: body table, container, base addresses, sections, SD updater facts.
- `references/features.md`: each feature, patch folder, key sites, status per body.
- `references/testing.md`: verifier pattern, emulators, camera ladder, recovery.
- `evals/`: see the note there; cases are added as the skill gets used.
