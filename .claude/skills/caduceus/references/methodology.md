# Analysis methodology (new body or new feature)

Every step below was used on the OM-5 port (`docs/PORT_OM5.md`); that page is the worked example.

## 1. Get and decode the stock file

- Download the official file with `tools/fetch_stock.sh` or by hand, keep it in `stock_firmware/<body>/`, check its
  SHA-256 against a known source (fishcu's `oly_fw_analysis` pins the same files). Never commit it.
- Decode with `external/oly_fw_analysis/tools/oly_container.py` into `analysis/<body>/`. Confirm the round trip is
  byte-identical and every checksum is valid before trusting anything else.
- The container has components: main 0x100 (the program), OSD resource 0x101 (menu text and pictures), 0x102, 0x106,
  and a boot component 0x10e on some files. Main is the one we patch. `-model` bytes: E-P7 `1420`, OM-5 `1440`.

## 2. Find the runtime base (the address of decoded offset 0)

Different bodies load main at different addresses (E-P7 `0x60020020`, OM-3 and OM-5 `0x60021020`). A wrong base
makes every pointer land in the wrong place, so find it from data, not by assumption:

- Decode the **static settings validator**: a table of records
  `{u16 sid, u16 attr, u32 list, u32 alt, u16 bytes, u16 elem, u8 sub, u8 type, u16 0}`. Each `list` is a pointer to
  that setting's allowed values.
- Try candidate bases. With the right one, every list pointer lands on a clean list (the OM-5 saturation sectors read
  -5..5). With the wrong one, pointers land inside other records. The OM-5 failed at `0x60020020` and decoded cleanly
  at `0x60021020`; 1439 records, layout identical to the E-P7's 1333.
- Also check that the flash layout is contiguous, so runtime address = file offset + base everywhere.
- `port_sigmatch.py`'s "same address" column is meaningless when bases differ. Its byte searches are unaffected.

## 3. Expand compressed spans before reading anything

The OM-5 main has two LZ4 "olycompress" spans (sections 5 and 6, `0x60847000`-`0x61343800`). In the raw decoded
file those bytes are LZ4 tokens and literal runs, which look like code in places. Searches in the raw file gave
wrong "not found" results and a wrong 22% match rate. Always run
`python -I tools/olycompress.py expand <decoded main> analysis/<body>/main.expanded.bin` and analyse the expanded
image. Check whether the body has compression at all by reading the BCS table (see hardware reference): flag 1 means
compressed. The E-P7 has none.

## 4. Find sites on the new body

- `tools/port_sigmatch.py <expanded image>` searches the E-P7's edit sites on another image by the bytes around them.
  Results: found once, ambiguous, absent. Treat "found" as a candidate, then disassemble to confirm.
- Chunk matching (10-byte chunks of a whole function) tells you whether a function is the same code: the OM-5 COLOR
  builder matched 112 of 118 chunks.
- Setting ids shift between bodies (OM-5 profile ids are the E-P7's minus 5, OM-3 differs by a three-digit shift).
  Re-derive them from the validator, do not copy numbers.
- A data table moves as a whole: find it by its row pattern (OM-5 Picture Mode label rows `7 0 1 2 5 3 6 ...`).
- Absence is a result: no function with the recording-limit compare chain exists on the OM-5, so it must be
  located by hand. Write negatives down with their evidence (`[V static, negative]`).

## 5. Disassemble

- Build the MN103 toolchain with `tools/setup_toolchain.sh` under WSL Ubuntu (strip the script's CRLFs first; fetch
  binutils 2.45 from `ftpmirror.gnu.org` if ftp.gnu.org is slow). Use `tools/dis.sh`.
- Use `tools/ep7_xref.py ADDR...` for brute-force xrefs (abs32 values, call/jmp forms at every byte offset).
- Confirm a hook by checking: what the stock call looked like, its register list and frame, who else calls the target.

## 6. Check free space and section order

Find code space in the FF tails of the loaded sections (section table in the hardware reference), claim a range in
`docs/CODE_SPACE_MAP.md` and keep a guard of untouched FF bytes at both ends. Sections load in table order, so hook
code must not call into a later section. For compressed sections, editing means expand, patch, repack with
`olycompress.py`, and check that the packed size still fits the flash span.

## 7. Record findings

Write what you found in `docs/PORT_<body>.md` with evidence tags `[V] [I] [C] [R]`, including void results and why.
When an earlier finding turns out wrong (the OM-5 "not found" results from compressed bytes), mark it void in place
rather than deleting it, so nobody repeats it.

## Pitfalls seen

- Reading the compressed bytes as code (OM-5).
- Assuming a feature is "switched off by a flag" when its settings, builder and pipeline branch exist but the menu
  entries, editor records and record template are gone (OM-5 COLOR 1-3).
- Trusting a count made from raw bytes ("256 getter sites against 440") without redoing it on the expanded image.
- Free-space claims that matched FF at the same address in a different image; on the new image those addresses hold data.
