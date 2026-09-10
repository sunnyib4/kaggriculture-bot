# kaggriculture-bot

An agent for **Kaggriculture**, a two-player farming-sim competition built on
[`kaggle_environments`](https://github.com/Kaggle/kaggle-environments). Each
player manages a farm and competes to end the 30-day season with the most
money, by planting and selling crops, expanding land, and hiring help.

## What it does

`main.py` runs a crop economy across wheat, carrot, and melon:

- Buys seed in bulk per crop so every hired unit always has something to plant
- Rotates which crop gets planted next so no single crop crowds out the others
- Prioritizes harvesting and watering over everything else, since two missed
  waterings turns a crop into a permanently lost weed tile
- Dedicates a unit to weed clearing once weeds pass a threshold, without
  permanently pulling a unit off crop duty
- Expands land and hires farm hands once there's a cash buffer to support it

It reliably beats both built-in baseline agents (`random` and `starter`) in
local testing, typically finishing the season with 5-8x the starting money.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install kaggle-environments
```

## Test locally

```bash
python -c "
from kaggle_environments import make
env = make('kaggriculture', debug=True)
env.run(['main.py', 'random'])
print([(i, s.reward, s.status) for i, s in enumerate(env.steps[-1])])
"
```

Swap `'random'` for `'starter'` to test against the other built-in baseline.

## Project notes

This bot went through several iterations, each driven by running the real
environment and reading the actual turn-by-turn data rather than trusting
assumptions about the rules. Real bugs found and fixed along the way:

- Units never leaving their starting tile to plant fresh seed
- A harvest-readiness check that fired before a crop was actually mature
- Seed stock too thin to keep multiple hired hands busy
- One crop always winning seed-selection priority, crowding out the others
- Weeds structurally starved of attention once the farm got busy
- A `FERTILIZE` action that silently no-ops under the actual game mechanics

An animal husbandry side-quest (goose/egg production) was attempted across
three further iterations, uncovered three more real bugs along the way, but
never turned a profit versus the crop-only economy -- it's deferred rather
than shipped. See the version history in `main.py`'s docstring for the full
detail on what was tried and why.

**Known gaps**, left for a future pass:

- No fertilizer usage (removed after confirming it silently no-ops)
- No animal husbandry (attempted, reverted -- see notes above)
- No opponent-aware strategy
- Movement is greedy-nearest, not globally optimal for many units
