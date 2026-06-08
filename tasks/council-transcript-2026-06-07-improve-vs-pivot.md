# LLM Council Transcript — Improve vs Pivot the Production Strategy

**Date:** 2026-06-07
**Question owner:** kunpark04
**Skill:** llm-council (5 advisors → anonymized peer review → chairman synthesis)

---

## Original Question

> How can we improve the current strategy? Maybe pivot instead?

## Framed Question (given to all 5 advisors)

How should we improve or pivot the production Kalshi weather-trading strategy?

**Context (evidence-grounded from the repo):**
- Production = `market_wing + drop_lower_ask`, MODEL-FREE, flat $2.50/trade, equal-payout, anchors on the market, fires once/day at each city's local 1 PM. Buys the market's modal bucket + one adjacent (cheap coverage).
- Edge is selection/market-structure, NOT forecasting: market modal ~82% accurate at the 1PM anchor; model adds nothing (LOO α→0; Brier 0.69 model vs 0.43 market). Maker execution is adversely selected (winning legs fill less in 14/20 cities) → production is a limit-at-ask taker on purpose.
- Durability: 3.4yr × 20-city, 80/20 IS/OOS split → ONLY Chicago has a durable, non-decaying edge (+5.0¢/$1 OOS, positive all 4 years). Every other city decayed as order books matured (NYC +9.8¢ 2023→−0.8¢ 2026; Miami +17.4¢→−1.0¢). Chicago +3.0¢ 2023→+7.3¢ 2026.
- Variant graveyard (all FAILED): joint_kelly (−$795), two_bucket_arb, variance (−$366), hrrr_bias (−$457), regime_confident, wing_any (−$149), drop_higher_ask PLACEBO (−$431, confirms the adjacency signal). Midnight anchor untradeable.

**Critical facts:**
- "96% WR / +32%" is 22 **in-sample** days only. ZERO forward / true-OOS evidence on the production strategy.
- LIVE Chicago bot armed since 2026-06-02 but placed ZERO real trades in 5 days — every wing fails the `max_ask_sum < 0.90` affordability gate on the $23.56 account. Correct behavior, not a bug. Balance unchanged.
- ~24 fillable Chicago fires/yr → forward significance takes YEARS.
- 20-city PAPER shadow + L2 orderbook logger collecting forward data free since 2026-06-02.
- Strategy is a strict once-daily snapshot — no intraday signal / exit. L2 logger makes an intraday/microstructure dimension newly testable.
- Local state files stale since 2026-05-30 — monitoring blind spot.

**Recommendation to pressure-test:** Don't touch the signal yet; fix monitoring; decide whether to fund the account to ~$250 (or accept it's a demo); accumulate ~30–60 fires before reopening improve-vs-pivot; intraday/microstructure is the novel R&D direction.

---

## Advisor Responses

### The Contrarian
The single most important fact is being papered over: you have **zero forward evidence** on the production strategy, and the structure guarantees you can't get any. 24 fires/year means a 30-fire sample takes 15 months — and that's fires, not the affordability-filtered subset, which on a funded account will be fewer. "Wait for forward data" isn't a plan; it's a euphemism for "this experiment will not conclude within a useful horizon."

Avoided questions: (1) The 96%/+32% is 22 in-sample days; the +5.0¢ OOS comes from a *separate* 3.4yr study. Has the *exact production config* been run through that OOS Chicago tape end-to-end? If not, you have OOS evidence for a *cousin* of what you run. That gap is where edges die. (2) Funding to $250 to "clear the gate" is self-deception — the gate exists because the strategy is uneconomic at small wing prices; ~12¢/trade × ~24 trades = under $3/year of EV before slippage. That's not a trading account; it's tuition.

Honest reframe: this is a **research asset**, not a P&L engine. The single-city, single-shot edge can't scale to significance. Only the L2 microstructure dimension has real option value — put compute there. Fix monitoring (free), keep paper shadow (free), and **don't fund live** — funding is the bad deal here.

### The First Principles Thinker
Strip it down: you are not running a trading strategy. You have **one** data point that matters — Chicago, +5¢/$1 OOS, non-decaying — and a delivery problem dressed up as a strategy problem. "Improve or pivot the signal?" is the wrong question. You have no forward evidence, so there is nothing to tune. Tuning now is curve-fitting to 22 in-sample days.

But the recommendation is too passive. Reframe the goal: what are you trying to learn, and what is the cheapest experiment that learns it? You care whether the single durable edge is real forward. What blocks forward fills? The affordability gate on a sub-scale account — that's the binding constraint, not signal quality. Highest-EV move: fund Chicago to clear the gate, fix monitoring, instrument fills.

The intraday/microstructure direction is **a different business**, not an improvement — different edge, risk, validation. Don't smuggle it in as "R&D on the side." Logging L2 is free; *acting* on it now means abandoning your one validated edge to chase an unvalidated one because waiting is boring. Boredom is not a signal.

One thing nobody asked: is 24 fires/year × one city *ever* worth your time, even if real? Compute the annual dollar edge at fundable size. If it's lunch money, the honest pivot is "successful research project; stop trading it." Answer that before funding anything.

### The Expansionist
Chicago isn't a single edge — it's a *proof of method*. You've built a machine that detects which order books haven't matured yet, and Chicago is the one that still hasn't. That detector is the undervalued asset, not the +5¢.

The 3.4yr study isn't a graveyard, it's a **map of decay curves**. NYC +9.8¢→−0.8¢, Miami +17.4¢→−1.0¢ are timestamps of order-book maturation. The same maturation happens on *new* Kalshi temperature markets and event types continuously. Your edge is "be early to a thinly-traded daily settlement market before market-makers arrive" — repeatable across every new city/series Kalshi launches. The next Miami-2023 is a market that doesn't exist yet.

Second hidden asset: the **L2 logger across 20 cities is now a maturation-detector in real time** — depth/spread/fill-asymmetry tell you live, collapsing the "years to significance" problem. Build the screen, not more Chicago fires.

Highest-EV move: fund to ~$250 (clears the gate, buys real fill data) **and** ship an L2 maturation-screen that ranks all 20 cities by exploitability weekly. The screen is the product; Chicago is its first hit. And the once-a-day snapshot leaves money on the table: if the 1PM modal-adjacency edge is real, the pre-1PM convergence is tradeable too — could be 5x bigger.

### The Outsider
Three things an intelligent stranger finds strange.

First: you're about to fund an account to chase 5¢ per dollar on ~24 days a year — roughly $3/year of expected edge at $2.50/trade. Even at $250 and bigger sizing, the dollar edge is trivial. Nobody asks the naive question: **what is the actual dollar goal?** If it's "prove the edge is real," fine — but say it out loud, because as a money-making venture this is a rounding error, and that reframes every downstream choice.

Second: **why is "the market beats your own model" the central pillar, yet you're betting on the market being wrong?** You buy a wing because the high lands where the market says it's most likely — but if the market is so accurate you abandoned your model, your edge has to come from the market being *mispriced*, not *accurate*. What exactly is the 5¢ capturing? Slow odds updates? A structural fee/liquidity quirk? If you can't name the mechanism in one sentence, you don't have an edge — you have a backtest.

Third: a monitoring file silently stale for a week, treated as a side fix. Reverse it. If you can't see what the bot does, you have **zero** real-future data regardless of how long you wait. Fix monitoring first, prove you can see a single trade end-to-end before adding a cent.

Smart move: don't add money to "trade more." Add the minimum to clear the filter, get ONE real fill observed cleanly, confirm the mechanism. Instrumentation project, not a trading one.

### The Executor
Fund it. That's Monday's first action and the only move that buys forward truth.

**Monday AM (30 min):** Fix the monitoring blind spot first — it's free and blocks everything. SSH to the droplet, confirm the systemd bots are alive, set up a daily rsync/scp of `positions.json` + `live_log.parquet` to local. You can't make a funding decision while blind to whether the live bot is even firing. First step is concrete: `systemctl --user status`, check journald, pull the state files.

**Monday PM (15 min):** Wire $250 into Kalshi. At $23.56 you have a bot that has proven it can correctly *decline* to trade — worth zero forward evidence. You cannot answer "does the in-sample edge hold forward?" without fills. $250 to clear the 0.90 gate at ~$2.50/trade is the cheapest experiment you'll ever run; downside capped and known.

Moves with no Monday action: "Pivot to intraday/microstructure now" — no first step, you have one logger-week of L2 depth. Park it as a *parallel data-accumulation* task, not a pivot. "Abandon as sub-scale toy" — premature.

Fastest path to a real answer: fund live + trust the 20-city paper shadow as your N-multiplier (20x the fires/year for the decay question); live gives the one number paper can't — real fill quality on the single durable city. Reassess at 30 fires. Don't touch the signal until then.

---

## Peer Reviews (anonymization mapping: A=Expansionist, B=Contrarian, C=Executor, D=First Principles, E=Outsider)

**Review 1:** Strongest E — only one to demand the mechanism be named in one sentence; without it, +5¢ is a backtest artifact. Biggest blind spot A — romanticizes into a "maturation detector" product, extrapolating a whole business from one non-decayed city with zero forward evidence. All missed: **the per-fire EV is corrupted by the very gate they want to clear** — the affordability gate rejects expensive wings, so the ~24 fillable fires are a biased subsample (cheap wings = low-prob/wide outcomes); funding to $250 changes *which* fires fill, breaking comparability with the OOS study.

**Review 2:** Strongest B — only response connecting all load-bearing facts; raises the genuinely new question of whether the production config was ever run end-to-end through the OOS tape; separates free moves from the bad one. Biggest blind spot C — rushes to "fund Monday," ignores that even funded you wait 15 months for 30 fills. All missed: **the 20-city paper shadow already generates ~480 simulated fires/yr with no affordability gate — that IS the fast significance engine**; the binding question is whether paper fills can be made realistic enough (modeled slippage/fill-rate) to substitute for live.

**Review 3:** Strongest E — nails the mechanism gap; if the market is so accurate you dropped your model, your edge requires the market being *mispriced*. Biggest blind spot A — heaviest build on the flimsiest base; most likely to burn months building infrastructure atop an unconfirmed edge. All missed: **settlement-mechanics and counterparty reality** — who you trade against on a thin book, whether 24 fires/yr survives Kalshi delisting/rule changes, and critically whether +5¢ survives realistic taker fees/slippage at fundable size (fee = ceil(7%·N·P·(1−P)) could erase 5¢). Nobody computed break-even after fees.

**Review 4:** Strongest E — interrogates the mechanism, correctly sequences monitoring-first and "clear the gate for ONE clean fill." Biggest blind spot C — funds Monday PM while monitoring is still being fixed that morning (contradicts its own ordering); swallows +5¢ as durable; treats 20-city paper as a clean 20x multiplier ignoring that paper has no fill-quality signal. All missed: **fee mechanics** — the gate failing isn't a funding problem but evidence wing prices leave near-zero post-fee EV; and the cheap unblock of **lowering per-trade size / the affordability threshold** to test the gate's calibration before spending real money.

**Review 5:** Strongest E — names the load-bearing contradiction (mispriced vs accurate); correctly scopes funding to "observe one clean fill." Biggest blind spot A — the 19 decayed cities are evidence the method *expires*, not that it generalizes; hand-waves mechanism + sample size into a roadmap. All missed: **the paper shadow (~480 fires/yr) answers "does the signal hold?" far faster than 24 live Chicago fires** — paper tests the *signal*, live tests only *execution quality*; separate them and most funding urgency dissolves.

**Peer-review tally:** Strongest → E (Outsider) ×4, B (Contrarian) ×1. Biggest blind spot → A (Expansionist) ×3, C (Executor) ×2.

---

## Chairman Synthesis

### Where the Council Agrees
1. **Don't touch the signal now** — unanimous. 22 in-sample days is nothing to tune against; tuning is curve-fitting.
2. **Fix monitoring first** — unanimous, and free. While state files are stale you have zero *observable* forward data no matter how long you wait; every accumulated trade is unobserved.
3. **This is currently a research/instrumentation project, not a P&L engine** — multiple advisors independently: ~$3/yr EV at current size is a rounding error. Name the dollar goal out loud.

### Where the Council Clashes
- **Fund vs don't-fund.** Executor + First Principles + Expansionist: wire ~$250, the cheapest experiment that buys forward fills. Contrarian + Outsider: funding is the bad deal — at 24 fires/yr you still wait ~15 months for 30 fills, the EV is tuition; add only the minimum to observe ONE clean fill and confirm the mechanism.
- **Reframe up vs reframe down.** Expansionist: Chicago is a "proof of method" → build a 20-city L2 maturation-screen, the screen is the product. Contrarian/First-Principles: that's heavy infrastructure on one unconfirmed-forward city; the 19 decayed cities are evidence the method *expires*, not that it generalizes.

### Blind Spots the Council Caught (peer review only)
1. **Gate-selection bias.** The affordability gate rejects expensive wings → the fillable subsample skews to cheap (low-prob, wide-outcome) wings, and funding to $250 changes *which* fires fill — breaking comparability with the +5¢ OOS study. Forward fills may not measure the same edge.
2. **The paper shadow is the real significance engine.** 20 cities × ~24 ≈ ~480 forward fires/yr, gate-free, answering the signal/decay question ~20x faster than live Chicago. Live only adds *execution* quality. Separate "does the signal hold?" (paper) from "can I execute it?" (live) and most funding urgency dissolves.
3. **Net-of-fee break-even is undecided.** Does +5¢/$1 survive ceil(7%·N·P·(1−P)) at fundable size? Fee bites hardest at P≈0.5 wings. Nobody computed it; it's the number that decides fund-vs-demo.
4. **A cheap unblock exists:** lower the flat stake / affordability threshold to test the gate's calibration before spending real money.

### The Recommendation
**Don't fund yet, and don't pivot the signal.** The council's center of gravity — reinforced by every peer review — is that funding is premature because two *free* questions that determine whether funding is even worth it haven't been answered:

1. **Is the edge real net-of-fees on the EXACT production config?** Run `market_wing + drop_lower_ask` (with the affordability + agreement gates as configured) end-to-end through the OOS Chicago tape, net of the real fee formula, and write down the one-sentence mechanism the +5¢ captures (slow odds updates? fee/liquidity quirk?). If you can't name it and it doesn't clear fees → backtest, not an edge → stop.
2. **Can the paper shadow substitute for live?** It already produces ~20x the forward fires. The gap is fill realism — add modeled slippage/fill-rate so paper answers the signal-decay question without funding.

Funding live becomes a *later, optional* experiment whose only purpose is measuring execution quality on the one durable city — and only if (1) confirms a net-of-fee edge AND you've decided a ~$3–30/yr dollar edge justifies your time. The Expansionist's L2 maturation-screen is the right long-horizon bet but a *different* project — keep logging (free); don't build it on one unconfirmed city.

This sides with the Contrarian/Outsider dissent over the 3-advisor "fund it" majority, because peer review showed funding doesn't buy *timely* truth (15-month sample), biases the sample (gate selection), and skips the free questions that gate the decision.

### The One Thing to Do First
**Fix the monitoring blind spot:** SSH the droplet, confirm both bots + the logger are alive under systemd, restore the daily state-file sync, and verify you can watch a single paper fill end-to-end. Until you can see, every other move — fund, pivot, wait — is blind.
