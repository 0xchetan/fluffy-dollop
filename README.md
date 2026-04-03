# PolyStreet Playbooks

Private strategy playbooks for PolyStreet — automated prediction market trading on Polymarket.

## Playbooks

| Playbook | Description |
|----------|-------------|
| [brain-hourly-btc](brain-hourly-btc/) | Hourly BTC direction bets on Polymarket using Gigabrain analysis + Kelly criterion sizing |

## Setup

Each playbook is a self-contained directory with:

- `playbook.yaml` — Manifest (name, skills, category)
- `soul.md` — Agent identity and strategy rules
- `bootstrap.md` — Interactive first-boot onboarding
- `scripts/` — Strategy scripts (run via `uv run`)

Deploy any playbook on a Gigabrain daemon agent. The bootstrap wizard walks through configuration on first boot.
