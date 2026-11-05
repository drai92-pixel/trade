---
name: trader-broker
description: >-
  Order Executor, set up for Canada. Sends APPROVED orders to your broker one
  at a time, never without your explicit yes. Shows each order as a plain
  ticket with its listing, currency and account (personal, corporation,
  TFSA, RRSP), records your yes word for word, and drafts it with the
  Interactive Brokers or Questrade connector so the final approval happens in
  your broker account. Records each fill with its Bank of Canada CAD rate so
  trader-tax keeps the ACB right. Alpaca (a US broker) remains as an option.
  Use when the user says "place the orders", "buy it", "execute", "send it
  to my broker", "connect Interactive Brokers", "connect Questrade", "set up
  paper trading", or after /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user approved it in their broker account** (IBKR, Questrade), or,
   with Alpaca, clicked Allow once on Claude's own permission card.

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol AAPL --side buy --qty 3 --price 341.27 --fx 1.3787 --fee 1
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RY.TO --side buy --qty 5 --price 170 --account tfsa
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, and keeps the log in `trader/state/broker.json`.

## Setup for Canada: Interactive Brokers or Questrade

Both have connectors in Claude's directory that **draft** orders: the order
appears in the user's broker account and only goes to the market after the
user approves it there. That approval is the third gate.

1. Customize > Connectors > Browse connectors, add **Interactive Brokers
   (IBKR)** or **Questrade**, and sign in there, never in the chat.
2. In `trader/config.json` set `"broker": "ibkr"` or `"broker": "questrade"`
   (start from `trader-budget/config.canada.example.json`).
3. Set the connector's tools that create or change orders (for IBKR the
   order-instruction tools, for Questrade `create_order_instruction` and
   `create_option_instruction`) to **Needs approval**. Option tools: Blocked
   unless the user says otherwise. Read-only tools (balances, positions,
   quotes) can be Always allow.
4. Check it: ask for the account summary (read only) and confirm which
   accounts the connector can see. Each order says which account it is for;
   the user picks that account when they approve it in the broker.

Paper trading: IBKR has a separate paper account (sign in to it with the
connector to practise). Questrade has a practice account. Keep `"paper": true`
until the user decides otherwise.

Things to tell the user once:

- **Currency.** A US stock bought from a CAD balance is converted by the
  broker at its rate. Holding USD (IBKR's FX conversion, or Norbert's gambit
  at Questrade) usually costs less; that is the user's call.
- **Fractional shares.** Canadian accounts often cannot buy fractions of
  shares. Size with `--whole-shares` in trader-budget.
- **Which account matters for tax.** A sale at a loss in the personal
  account followed by a buy of the same stock in the TFSA or RRSP within 30
  days loses that loss for good (trader-tax: superficial loss).

## Setup B: Alpaca (US broker)

Alpaca's own MCP server is github.com/alpacahq/alpaca-mcp-server. It is a US
broker; check that it accepts the user's residency before setting it up. If
the user does use it: paper API keys typed into the Claude Desktop config
(`claude_desktop_config.example.json` here), never into the chat;
`ALPACA_PAPER_TRADE` true; `place_stock_order` and every tool that places,
changes or closes anything set to **Needs approval**; option, crypto and
"close all" tools Blocked. In Claude Code keep the order tool under `ask` in
`.claude/settings.json`, never `allow`. With Alpaca the third gate is
Claude's own Allow once card, and tickets show the exact `place_stock_order`
JSON.

## Steps, for each order

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "yes to all", or a yes that names a different
   stock or amount). Ask again. Never turn a reply into a yes yourself.
4. Only after "Recorded your yes": with IBKR or Questrade, draft exactly
   that order (symbol, side, quantity, type, time in force) for the account
   the ticket names, and tell the user to approve it in their broker. With
   Alpaca, call `place_stock_order` with exactly the JSON the ticket printed;
   Claude's card appears: Allow once or Deny.
5. Record what happened: `ticket.py result --index N --status placed
   --broker-id <id from the response>`, or `--status denied` if they pressed
   Deny, or `--status failed` with `--note` for an error. A failed order is
   not retried without a new check and a new yes.
6. When the fill is known (the broker's order status or positions), record
   it once with `ticket.py fill --symbol S --side buy|sell --qty Q --price P
   --fx RATE --fee F --account ACCOUNT`. `--fx` is the Bank of Canada rate
   for the trade date (`trader-tax/lots.py fx --date DATE`); CAD stocks do
   not need it. A buy becomes a purchase in `trader/portfolio.json`; a sell
   leaves at average cost and trader-tax logs the gain or loss. Then the
   next ticket.

## Example: the reel

```
ORDER 1 of 5, PAPER account, Interactive Brokers
  BUY 3 shares of AAPL, market order, good for today
  US listing, USD (your broker converts CAD unless you hold USD); account: TFSA
  READY: trader-risk PASS, waiting for your yes
  Claude drafts this with the Interactive Brokers connector. Interactive Brokers sends it to your
  account, and it only goes to the market after you approve it there.
  Reply "yes" to place this one order, or "no" to skip it.
```

The user replies "yes". Claude records it, drafts the order with the IBKR
connector for the TFSA, and the user approves it in IBKR.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys or passwords.
- Never switches `ALPACA_PAPER_TRADE` or `"paper"` to live, or a connector from the paper to the live account. The user does that themselves.
- Never calls `close_all_positions`, `cancel_all_orders`, option or crypto order tools on its own.

## Safety

- Education and tooling, not financial advice. No order here is a recommendation, and nothing promises returns.
- Paper trading by default (`"paper": true`). Going live is the user's own decision.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user approved it in their broker (IBKR, Questrade) or clicked Allow once on Claude's card (Alpaca).
  Order tools stay on "Needs approval"; never "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
