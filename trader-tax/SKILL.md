---
name: trader-tax
description: >-
  Tax Optimizer, Canadian rules. Keeps the adjusted cost base (ACB) of every
  holding at average cost in CAD (each purchase converted at its own Bank of
  Canada rate), pools identical shares across a taxpayer's non-registered
  accounts and both listings of interlisted stocks, shows unrealized gains
  and losses, works out what a sale would realize and its taxable half
  before you sell, finds tax-loss selling candidates, and checks the
  superficial loss rule (30 days before and after, across your spouse, your
  corporation, TFSA and RRSP). Writes harvest sells only as proposals. Not
  tax advice. Use when the user says "what are my gains", "ACB", "cost base",
  "tax-loss harvest", "tax-loss selling", "what would selling cost me in
  tax", "superficial loss", "wash sale", "capital gains", or before selling anything.
argument-hint: "[ticker]"
---

# trader-tax (Canada)

The Tax Optimizer does the arithmetic on the user's purchases so nobody sells
blind. It never decides what is right for the user's taxes. **This is not tax
advice**: their accountant decides what applies. Say that once, early, in
every answer that uses this skill.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/lots.py" fx --write                     # today's Bank of Canada USD/CAD rate
python3 "${CLAUDE_SKILL_DIR}/lots.py" report --lots                  # ACB, value, gain per holding (CAD)
python3 "${CLAUDE_SKILL_DIR}/lots.py" sell KO 2 --price 90.11        # what a sale would realize
python3 "${CLAUDE_SKILL_DIR}/lots.py" sell KO 2 --account corp       # when the stock is held in two accounts
python3 "${CLAUDE_SKILL_DIR}/lots.py" harvest --min-loss 100         # candidates, superficial loss checked
python3 "${CLAUDE_SKILL_DIR}/lots.py" harvest --min-loss 100 --write
python3 "${CLAUDE_SKILL_DIR}/lots.py" wash KO                        # would buying KO now spoil a recent loss?
python3 "${CLAUDE_SKILL_DIR}/lots.py" sell KO 2 --price 90.11 --fx 1.39 --fee 1 --record   # after it fills
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/portfolio.json` | holdings and their purchases: `{"ticker", "account", "lots": [{"id", "date", "shares", "cost", "fx", "fee"}]}`; shape: `portfolio.example.json` here |
| `trader/prices.json` | current prices (trader-alerts) and `"fx": {"USD": rate}` (`lots.py fx --write`) |
| `trader/orders.json` | planned buys count for superficial losses; `harvest --write` replaces it with sell proposals |
| `trader/state/tax.json` | the realized sales it logs with `sell --record` (or trader-broker's `fill`) |

- `cost` and `fee` are in the stock's own currency; `fx` is CAD per unit on
  the trade date. USD stocks in a taxable account need `fx` on every
  purchase; the tool refuses to guess. `lots.py fx --date YYYY-MM-DD` looks
  it up from the Bank of Canada (CRA accepts its daily rate).
- `account` is `personal` (default, non-registered), `corp` (the user's
  professional corporation), `tfsa`, `rrsp`, `rrif`, `fhsa`, `resp` or
  `spouse`. List registered and spouse holdings too, even though they have
  no gains: they are what make a loss superficial.
- Purchases come from trader-broker fills or the broker's statements. If a
  holding has shares but no purchases, ask for the dates, costs and rates.
  Never invent a cost base.

## The rules it applies (federal Income Tax Act, arithmetic only)

- **ACB is average cost.** All identical shares one taxpayer owns form one
  pool: every non-registered account at every broker, and both listings of
  an interlisted stock (RY and RY.TO, CNI and CNR.TO). There is no FIFO, no
  choosing lots, and no short or long term in Canada.
- **CAD.** Cost converts at the rate on the purchase date, proceeds at the
  rate on the sale date, so the currency move is part of the gain.
- **Inclusion.** Half of a net capital gain is taxable. A net capital loss
  only offsets capital gains: this year, carried back 3 years, or forward
  indefinitely. The tool shows the taxable half, never a tax bill or a rate.
- **Superficial loss.** The loss is denied if the user, their spouse, or a
  corporation they control buys identical shares in any account (TFSA and
  RRSP included) in the 30 days before or after the sale and still holds
  them on day 30. Denied part = loss × min(sold, bought in the window, held
  at the end) / sold. It is added to the ACB of the shares bought back
  (deferred, not lost), except in a registered account, where it is gone for
  good. Buying in the 30 days *before* counts too: selling 10 of 12 shares
  when 2 were bought last week denies a fifth of the loss.
- **Corporation.** The user's professional corporation is a separate
  taxpayer with its own ACB pool and its own gains, but it is affiliated for
  superficial losses. How corporate investment income is taxed (passive
  income, the capital dividend account) is for their accountant.

It sees only what is in `trader/portfolio.json`. Purchases by a spouse or in
accounts the user did not list still count. Say so.

## Steps

1. Refresh prices and the USD rate (`alerts.py quote --holdings --write`,
   then `lots.py fx --write`), then `lots.py report --lots`. Show gains and
   losses per holding in CAD, and which holdings are registered.
2. Before any sell (rebalance, a stop, a change of mind), run `lots.py sell`
   with the planned shares and show the gain or loss and its taxable half.
3. For harvesting: `lots.py harvest --min-loss 100`. For each candidate show
   the estimated loss, any superficial-loss conflict and the first day it
   can be bought back by anyone in the household or the corporation.
   Candidates with a conflict are not proposed. Late in the year, note that
   a sale counts in the year it **settles**. With T+1 settlement, trade no
   later than the second-last business day of the year; confirm the cutoff
   with the broker.
4. Only if the user wants proposals: `harvest --write`, then `/trader-risk`,
   then `/trader-broker`, one yes per order.
5. After a sale fills, record it once: `ticket.py fill ... --fx RATE` or
   `lots.py sell ... --record`, not both.
6. If `report` warns that a loss sale became superficial after the fact (a
   later purchase), tell the user plainly and suggest they raise it with
   their accountant.

## What it never does

It never files anything, never gives a tax rate or a tax bill, never tells
the user what they owe, never places an order, never proposes a harvest it
can see would be superficial, and never invents a cost or an exchange rate.

## Safety

- Not tax advice, and education and tooling, not financial advice. Check with an accountant.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only with trader-risk's PASS for that
  exact order today and the user's explicit yes for that one order, plus the final approval in the
  broker (IBKR, Questrade) or Claude's Allow once card (Alpaca).
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker sign-ins and keys stay in the connector or the Claude Desktop config, never in the chat.
