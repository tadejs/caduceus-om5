# Which other Olympus bodies could run Caduceus?

Status: the **PEN E-P7** is tested here, and the **OM-3** has its own camera-tested port by androidfanman. Nothing else has been tried on a camera, so **please do not flash an E-P7 build on any other body**. This page is a map of what we know, updated as we learn more.

## Where each body stands

| Body | Closeness to the E-P7 | Home for looks | Status |
|---|---|---|---|
| **E-M10 IV** | Closest: same file size and load address, 47% of edits match by signature | No COLOR profiles, so looks need a new home | **First target.** In development, not yet run on a camera. **Needs a volunteer to test and report issues:** [issue #21](https://github.com/cakelesscoder/caduceus-ep7/issues/21) (`docs/PORT_EM10IV.md`) |
| **E-M5 III** | 36% match; SD loader and log hook don't match | None | Second choice (`docs/PORT_EM5III.md`) |
| **E-M1 III** | Larger file and different load address; match not worked out | None | Firmware analysed on disk only |
| **E-M10 III, E-PL9** | Same main size; match not run | Probably none | Not analysed |
| **PEN-F** | Older generation | **Yes, it has COLOR profiles** | Already served by [fishcu's PEN-F HSL mod](https://github.com/fishcu/oly_fw_analysis) |
| **OM-3** | Same kind of firmware; 26% of edits match | **Yes, it has COLOR profiles** | **Working port:** [Caduceus for OM-3](https://github.com/androidfanman/caduceus-om3) by androidfanman. Control Panel export supported (`docs/OM3_NOTES.md`) |
| **OM-5** | Same address base as the OM-3; 22% of edits match | **COLOR 1-3 and MONO 1-3 settings present** (E-P7 layout, renumbered); not in the Picture Mode list (owner-checked); pipeline branch present, editor screen absent | Analysed on disk; owner available, first camera tests not yet run (`docs/PORT_OM5.md`) |
| **OM-1, OM-1 II** | Same kind of firmware; 22-28% of edits match across the OM series | Partial profile settings, no profile screens | Quick check only |
| **E-M10 II** | Closer to PEN-F by layout | Has Color Creator, but predates COLOR profiles | Research only |

"Match" is a static check (`tools/port_sigmatch.py`): how many E-P7 edits can be found in another body's firmware by their surrounding bytes. A match says the code looks alike. It does not say the edit is correct or safe on that body.

## How a new body gets tested

Nothing goes to another camera until each step has passed on that body, in this order, with an owner who has read the risks:

1. The official OM firmware file through the camera's own updater.
2. A stock restore round trip.
3. A small harmless text change on the firmware screen, plus restoring stock.
4. A small data edit.
5. A hook that does nothing, then real features one at a time.

**The honest risk:** the camera's SD updater recovered the E-P7 from a boot loop once (battery out, then OK + power, then a stock restore). That is proven only on the E-P7, and only once. A fault early enough in boot might stop the updater from starting on any body, so a failed flash could mean a repair.

## One Control Panel for every camera

Looks stay in one format, made and shared through the same [Control Panel](https://cakelesscoder.github.io/caduceus-ep7/) and share codes. Differences between cameras are handled by a translation step on export, so a look made for one camera can be exported for another, including non-Olympus ones (such as Canon Picture Styles, `docs/PORT_CANON_1100D.md`).

## Credit

This project exists because of [u/fischgurke](https://www.reddit.com/user/fischgurke/) and their PEN-F HSL firmware mod, which showed an Olympus body could get in-camera HSL editing.
