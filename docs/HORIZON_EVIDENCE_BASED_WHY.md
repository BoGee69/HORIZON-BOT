# HORIZON BOT Evidence-Based Why Answers

This patch makes HORIZON BOT answer owner questions like `kenapa masih ada yang belum rename?` from evidence instead of generic assumptions.

## Behavior

When the owner asks `kenapa`, `kok`, `why`, `apa penyebabnya`, or `alasan`, HORIZON BOT should:

1. Show the facts it can prove from live inventory or runtime summaries.
2. Use R2 maintenance skip counters when available.
3. Show sample logs/errors when available.
4. Clearly say when evidence is insufficient.
5. Avoid presenting assumptions as facts.

## R2 rename evidence

The R2 maintenance summary now records machine-readable skip reason counts:

- `Skip no AppID`
- `Skip missing game name`
- `Skip unsafe game name`
- `Skip target exists`
- `Skip oversized ZIP`

These counters let HORIZON BOT explain leftover rename work from actual maintenance observations.

## Important limitation

Old maintenance summaries created before this patch may not contain skip reason counters. After the next R2 maintenance run, new summaries will include the evidence needed for more precise explanations.
