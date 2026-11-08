# Notes for agents

**Porting Caduceus to another body, or patching the firmware?** Start with [`docs/FIRMWARE_GUIDE.md`](docs/FIRMWARE_GUIDE.md): how the E-P7 1.1 firmware is built, how to patch it safely, the lessons behind each safety rule, and a porting checklist.

## One Control Panel for every camera

The goal is **one Control Panel, one look format and one set of share codes**, hosted from this repo, with each camera as a selectable profile. Separate per-camera panels are meant to retire into this one. For the OM-3, that means `androidfanman/caduceus-om3`'s panel, once this has been agreed with its author.

Build panel work so it ends up here:

- **Add a camera, don't fork the panel.** A camera is one entry in `webapp/src/cameras.ts` (what the editor offers) and one export target in `webapp/src/targets/` (translation to its card files), listed in `targets/index.ts`.
- **Keep the pack format shared.** Camera differences belong in the profile and the export translation, not in a new look format or share code. Keep a wider value in the look until the user moves that control.
- **Keep other repos' code easy to resync.** Files copied from a port stay unchanged with a source line (`webapp/src/om3.ts`, `om3names.ts`), glue goes in `targets/`, and credit goes in `NOTICE`.
- **Firmware stays with its port.** Each port keeps its own repo, install guide and bug reports. The panel's About dialog and `docs/BODIES.md` point people to the right one.

Findings from the OM-3 port, including what's unverified, are in `docs/OM3_NOTES.md`. Don't add its features to the E-P7 until they can be tested here.
