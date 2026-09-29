# Finding 07: Can we ignore the price if our model is good?

**Idea tested (Jack, 2026-09-28):** a model picks the winner and the stake; we enter at
whatever the price is. The reasoning: Kalshi prices can't influence players, so price and
outcome should be independent.
**Chart:** `python scripts/finding07_price_vs_model.py`. This is an illustration, not a
pre-registered test.

![Price vs model](img/16_price_vs_model.png)

## Left: the math

- $10 buys **10 ÷ price** contracts, and each contract pays $1 if it wins.
- **Expected profit = $10 × (model probability ÷ price − 1).** At 75%: +$8.75 at 40¢, $0 at 75¢, −$0.62 at 80¢.
- Ignoring the price means profit = **how often our picks win − the average price paid.** We already ran that bet: `favourite-desk` lost −$292.

## Right: the independence test

- ✅ **Prices don't cause outcomes.** That part of the idea is right.
- ❌ **But price and outcome are strongly related,** because both come from team strength.
- **The test:** regress the outcome on the Elo model **and** the price together (714 settled bets, 473 games, 99% intervals by game):
  - **Market price: +1.30** (0.95 to 1.88), a strong predictor.
  - **Elo model: −0.03** (−0.65 to +0.43). **It adds nothing once the price is known.**
  - Accuracy (Brier score, lower is better): **market 0.120**, model 0.218.
- **So when the model and the market disagree, the market is usually right**, and a price-blind bet pays for every one of those disagreements.

## What it means for v2

- The price must be **in the decision**. It can't be ignored.
- **The model's job** is to find where it's **more right than the price**, which is Gate 2 in [NEXT_CHAPTER_v2.md](../NEXT_CHAPTER_v2.md).
- **The stake should depend on the edge,** i.e. (model probability − price) and our confidence, not just on the model's probability.
- **Caveat:** this uses the weak Elo model on bets the desk chose. A stronger model has to be tested the same way. That test is Phase 1.
