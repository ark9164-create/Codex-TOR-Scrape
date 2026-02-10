# Codex-TOR-Scrape

Playwright scraper for Top of the Rock ticket times and prices.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

```bash
python scraper.py --date 2026-02-10 --output-prefix tor_prices
```

Outputs:
- `tor_prices.json`
- `tor_prices.csv`

Each record contains:
- `date`
- `time`
- `price`
- `source` (`network-json` or `dom`)

## Notes

- The script opens `https://www.rockefellercenter.com/buy-tickets/top-of-the-rock/`, waits for widget/network activity, and extracts any 10-minute slot times with prices.
- If Cloudflare blocks your environment, run from a residential IP or headed browser (`--headed`).
