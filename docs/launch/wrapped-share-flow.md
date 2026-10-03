# Wrapped share flow

1. Build a database with `sherd dig PATH`, or use `sherd demo` for made-up data.
2. Run `sherd wrapped --out wrapped` (add `--demo` for the demo DB), or open `/wrapped` through `sherd web --demo` for the demo. The default year is the last full calendar year in the data. A year with missing source data can produce fewer than six cards.
3. Download the PNGs from `/wrapped`, or use those written to `wrapped/` by the CLI. The web route renders cards in memory until download; the CLI writes local files. Neither posts them.
4. Review each image at full size, then choose and post only the cards you want. Contacts appear as initials and currency amounts are omitted by default. The Names and Amounts toggles and CLI `--names`/`--amounts` opt into more disclosure. Cards contain no message text. Dates, patterns and other statistics can still be identifying.

## Manual checklist for Vlad

- [ ] Use demo-only images for launch posts; confirm the demo banner or synthetic-data label is visible in context.
- [ ] Check each of the six PNGs for accidental names, amounts or sensitive inferences before posting.
- [ ] Keep Names and Amounts off in the recording and screenshots.
- [ ] Confirm the install path and public docs link at launch time. TODO(vlad): insert the live docs URL once Pages is on.
- [ ] Read the destination community's current rules before posting.
