"""
Kaggriculture Agent v11 (FINAL)
=================================
v11 reverts to v7's crop-only economy. This is a deliberate decision, not a bug fix.

The animal husbandry side-quest (v8, v9, v10) is documented here because each attempt
found and fixed a genuinely real bug, in case it's ever worth revisiting:

  v8: assumed FERTILIZE and a shed-purchased animal both worked like the seeds bank
      (usable without being carried). FERTILIZE assumption was wrong -- confirmed via
      real test data as a silent no-op that got units trapped retrying it forever,
      collapsing watering by ~70% and costing the only loss to `starter` all project.
      FERTILIZE was removed entirely as a result.
  v9: fixed goose-keeper role assignment, which had been based on "last unit in the
      roster" -- broke because the roster grows during the day as hands get hired,
      orphaning an in-progress goose delivery. Fixed with a state-based design (any
      unit opportunistically services the coop; the farmer, always a stable index,
      owns the active seeking behavior). Confirmed working for delivery.
  v10: fixed feeding being the lowest-priority fallback, so it almost never got a
      turn against the farmer's near-constant crop opportunism. Elevated it to the
      same urgency tier as an active goose delivery.
  Final root cause (found via turn-by-turn tracing, not just aggregate stats): the
      wheat reserve (WHEAT_FEED_RESERVE) and the fetch-trip trigger condition
      (`shed wheat > WHEAT_FEED_RESERVE`) worked against each other. The sell logic
      pins shed wheat at exactly the reserve level by design -- so the "go fetch
      more feed" condition, which required exceeding that same reserve, could never
      fire. The goose starved on a strict 2-day cycle regardless of how much wheat
      was sitting right there, because "enough to protect" and "enough to bother
      fetching" were defined as the same number.

Across three versions, each real bug got fixed, and the mechanic still never turned
a profit: $13,310/$10,383 (v10) vs v7's proven $21,713/$24,274 -- a net loss vs the
crop-only baseline every single time. Per the exit criterion v10 itself documented,
that's the signal to stop investing in this rather than chase a fourth fix. If ever
revisited, the concrete next step is trivial: change the fetch condition to
`shed.get(WHEAT, 0) > 0` (or some amount below the reserve, not above it).

Everything below this point is v7's crop economy, unchanged and proven:

Changelog from v6->v7 (weed handling): a unit is only pulled onto weed patrol once
weeds exceed WEED_THRESHOLD, confirmed by test data to recover most of v6's lost
production (a full-time weeder) while still reducing season-end weed count.

Changelog from v4->v5 (crop selection): crop selection rotates through a cycle
(CROP_CYCLE) that only advances on actual planting, confirmed by test data to
unlock real carrot/melon sales that a fixed-order pick always crowded out.

Changelog from v3->v4 (seed supply): seed stock sat at 1-2 units all game causing
mass PLANT no-ops. Fixed with bulk SEED_TARGETS buying.

Changelog from v2->v3 (harvest timing): `yield_units > 0` alone fires the instant a
seed is planted, before any growth -- caused an infinite plant/fail-harvest loop
with zero watering ever happening. Fixed with a maturity (HARVEST_DAY) check.

Changelog from v1->v2: no weed clearing, planting stalled once any plant existed,
land bought before income was flowing. Fixed with priority-based per-turn dispatch.

Test locally before trusting this:
    pip install kaggle-environments
    python -c "
from kaggle_environments import make
env = make('kaggriculture', debug=True)
env.run(['main.py', 'random'])
print([(i, s.reward, s.status) for i, s in enumerate(env.steps[-1])])
"
"""

WHEAT, CARROT, MELON = "WHEAT", "CARROT", "MELON"
SELL_CHUNK = 4
HAND_HIRE_CASH_BUFFER = 150
LAND_BUY_CASH_BUFFER = 1200
LAND_BUY_MIN_DAY = 6
SEED_BUY_BUFFER = 50
WEED_THRESHOLD = 10

SEED_COST = {WHEAT: 10, CARROT: 20, MELON: 80}
HARVEST_DAY = {WHEAT: 4, CARROT: 3, MELON: 10}
SEED_TARGETS = {WHEAT: 8, CARROT: 4, MELON: 3}
CROP_CYCLE = [WHEAT, CARROT, WHEAT, MELON]

_cycle_pos = 0


def manhattan_step(src, dst):
    sx, sy = src
    dx, dy = dst
    if sx == dx and sy == dy:
        return None
    if sx < dx:
        return "EAST"
    if sx > dx:
        return "WEST"
    if sy < dy:
        return "SOUTH"
    if sy > dy:
        return "NORTH"
    return None


def nearest(pos, candidates):
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c[0] - pos[0]) + abs(c[1] - pos[1]))


def scan_tiles(tiles, day):
    """Single pass over the board, categorizing every tile once per turn."""
    harvestable, needs_water, weeds, empties = [], [], [], []
    for y, row in enumerate(tiles):
        for x, t in enumerate(row):
            if t is None:
                empties.append((x, y))
            elif isinstance(t, dict):
                if t.get("kind") == "WEED":
                    weeds.append((x, y))
                elif t.get("kind") == "PLANT":
                    age = day - t.get("planted_day", day)
                    mature = age >= HARVEST_DAY.get(t.get("crop"), 0)
                    if mature and t.get("yield_units", 0) > 0:
                        harvestable.append((x, y))
                    if not t.get("watered_today", False):
                        needs_water.append((x, y))
    return harvestable, needs_water, weeds, empties


def agent(obs):
    player = obs["player"]
    me = obs["farms"][player]
    private = obs["private"]
    day = obs["day"]
    money = me["money"]
    tiles = me["tiles"]

    units = [("farmer", me["farmer"])] + [("hand", h) for h in me["hands"]]
    seeds = private.get("seeds", {})
    shed = private.get("shed", {})

    market_orders = []

    # ---- Sell shed inventory, throttled -------------------------------
    for item, qty in shed.items():
        if qty <= 0:
            continue
        market_orders.append(["SELL", item, min(qty, SELL_CHUNK)])

    # ---- Buy seeds up to target stock levels, so every unit has work ---
    for crop in (WHEAT, CARROT, MELON):
        have = seeds.get(crop, 0)
        shortfall = SEED_TARGETS[crop] - have
        if shortfall <= 0:
            continue
        cost = SEED_COST[crop] * shortfall
        if money - cost >= SEED_BUY_BUFFER:
            market_orders.append(["BUY_SEED", crop, shortfall])
        else:
            affordable = int((money - SEED_BUY_BUFFER) // SEED_COST[crop])
            if affordable > 0:
                market_orders.append(["BUY_SEED", crop, affordable])

    # ---- Land expansion: gated by day AND a large cash buffer ---------
    quadrants_owned = len(me["unlocked_quadrants"])
    land_costs = [1000, 2000, 4000]
    if day >= LAND_BUY_MIN_DAY and quadrants_owned <= 3:
        land_cost = land_costs[quadrants_owned - 1] if quadrants_owned >= 1 else land_costs[0]
        if money >= land_cost + LAND_BUY_CASH_BUFFER:
            market_orders.append(["BUY_LAND"])

    # ---- Hire a hand once affordable -----------------------------------
    hires_today = me.get("hires_today", 0)
    fib = [1, 1, 2, 3, 5, 8, 13, 21]
    hire_cost = fib[min(hires_today, len(fib) - 1)]
    if money >= hire_cost + HAND_HIRE_CASH_BUFFER and len(units) < 4:
        market_orders.append(["HIRE"])

    market_orders = market_orders[:10]

    # ---- Per-unit dispatch ----------------------------------------------
    harvestable, needs_water, weeds, empties = scan_tiles(tiles, day)
    claimed = set()
    unit_actions = []
    seed_pool = {c: seeds.get(c, 0) for c in (WHEAT, CARROT, MELON)}

    def peek_crop():
        global _cycle_pos
        n = len(CROP_CYCLE)
        for i in range(n):
            idx = (_cycle_pos + i) % n
            c = CROP_CYCLE[idx]
            if seed_pool.get(c, 0) > 0:
                return c, idx
        return None, None

    def consume_crop():
        global _cycle_pos
        c, idx = peek_crop()
        if c is not None:
            seed_pool[c] -= 1
            _cycle_pos = (idx + 1) % len(CROP_CYCLE)
        return c

    weeder_idx = (len(units) - 1) if (len(weeds) > WEED_THRESHOLD and len(units) > 1) else None

    for i, (kind, pos) in enumerate(units):
        x, y = pos
        tile = tiles[y][x]
        action = ["PASS"]

        if (x, y) in harvestable:
            action = ["HARVEST"]
        elif (x, y) in needs_water:
            action = ["WATER"]
        elif (x, y) in weeds:
            action = ["DIG"]
        elif tile is None and peek_crop()[0]:
            crop_to_plant = consume_crop()
            action = ["PLANT", crop_to_plant]
        else:
            if i == weeder_idx:
                candidates = (
                    [t for t in weeds if t not in claimed]
                    or [t for t in harvestable if t not in claimed]
                    or [t for t in needs_water if t not in claimed]
                    or ([t for t in empties if t not in claimed] if peek_crop()[0] else [])
                )
            else:
                candidates = (
                    [t for t in harvestable if t not in claimed]
                    or [t for t in needs_water if t not in claimed]
                    or [t for t in weeds if t not in claimed]
                    or ([t for t in empties if t not in claimed] if peek_crop()[0] else [])
                )
            target = nearest(pos, candidates)
            if target:
                claimed.add(target)
                step = manhattan_step(pos, target)
                action = [step] if step else ["PASS"]
                if target in empties and peek_crop()[0]:
                    consume_crop()

        unit_actions.append(action)

    return {
        "farmer": unit_actions[0],
        "hands": unit_actions[1:],
        "market": market_orders,
    }
