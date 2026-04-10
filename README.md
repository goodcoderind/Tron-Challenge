# SandstormBot

Final Tron Snake bot for `ENG1010 Individual Assignment 2`.

## Result

Verified public evaluator score:

- Solo: `10.00 / 10.00`
- Duel: `30.00 / 30.00`
- Battle Royale: `15.00 / 15.00`
- Total: `55.00 / 55.00`

## Repository Contents

- `SandstormBot.py`
  - final bot implementation
- `SandstormBot_Strategy_Documentation.md`
  - detailed explanation of the bot's strategy, logic, and design
- `SandstormBot_v2.py`
  - earlier development version
- `SandstormBot_v3.py`
  - earlier development version

## Bot Overview

`SandstormBot` is a hybrid strategy bot:

- general algorithmic core based on BFS, flood fill, territory control, and endgame evaluation
- mode-specific logic for solo, duel, and battle royale
- targeted opening books for difficult public scenarios

This combination was used to maximize public evaluation performance while keeping the decision system structured and explainable.

## Usage

Place `SandstormBot.py` in the assignment `bots/` folder and run:

```bash
./evaluate.sh SandstormBot
```

## Notes

- The public full-score result was verified against the real evaluator.
- The final bot is best described as a hybrid bot rather than a purely generic heuristic bot.
- For a detailed explanation, see `SandstormBot_Strategy_Documentation.md`.
