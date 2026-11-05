# AGENTS.md

This repo finds more companies for the job collector in the private repo `attempt-1`, mostly German Mittelstand and startups. It checks each company's job board with attempt-1's own code and writes rows in attempt-1's `config/targets.csv` format. Start with [`README.md`](README.md).

## Commands
```bash
pip install -e ../attempt-1 pytest         # the scripts import attempt-1 (set ATTEMPT1=/path if it lives elsewhere)
pytest -q                                   # offline
python discovery/check_boards.py todo.txt research/accounts.csv --workers 12
python discovery/select_new.py research/accounts.csv --out new_targets.csv
```

## Never
- Copy attempt-1's code, data (`data/`, `config/seed_links.csv`), docs or history into this repo. **This repo is public; attempt-1 is private.**
- Store contact data (names, emails, phone numbers) or personal details of the job seeker.
- Scrape aggregators for jobs. arbeitnow and Y Combinator data are used for company names only.
- Ignore robots.txt, log in, bypass CAPTCHAs or Cloudflare challenges. JOIN's job sitemap sits behind a challenge, so leave it.
- Put model names or IDs in commits, code or docs.

## Conventions
- Same style as attempt-1: small functions, plain data, few comments.
- Keep the checks polite: one request per 2–4 s per host. The Fetcher from attempt-1 enforces this.
- `research/accounts.csv` is append-only evidence. Re-check an account by deleting its row.
