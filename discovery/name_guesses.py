"""Guess job-board accounts of startups from public company lists, for check_boards.py.

Sources: the arbeitnow job API (employers with IT jobs in Germany; used only for the names) and the open Y Combinator
company data (active companies with a German or Dutch office). Startup boards (Greenhouse, Lever, Ashby) usually use
the company name as the account, so "Isar Aerospace" → greenhouse/lever/ashby "isaraerospace", "isar-aerospace".

  python discovery/name_guesses.py --out todo-names.txt
"""

import argparse
import json
import re
import time
import unicodedata
import urllib.request
from collections import Counter

from common import USER_AGENT

from jobcollector.relevance import best_country, classify_title

ARBEITNOW = "https://www.arbeitnow.com/api/job-board-api?page={page}"
YC = "https://yc-oss.github.io/api/companies/all.json"
LEGAL = {"gmbh", "mbh", "ag", "se", "kg", "co", "ug", "haftungsbeschraenkt", "inc", "ltd", "llc", "limited", "bv", "sa",
         "e", "v", "kgaa", "partg", "mbb"}


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def slug_guesses(name):
    """Likely account names for a company name, best first."""
    n = name.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", " ", re.sub(r"\(.*?\)", " ", n)).split()
    core = [w for w in words if w not in LEGAL]
    out = ["".join(core), "-".join(core)] + ([core[0]] if len(core) > 1 else [])
    return [s for s in dict.fromkeys(out) if 2 <= len(s) <= 60]


def arbeitnow_names(max_pages=60):
    """Employers with at least one IT job in Germany (or with no place given) on arbeitnow."""
    names = Counter()
    for page in range(1, max_pages + 1):
        data = get_json(ARBEITNOW.format(page=page))
        for j in data.get("data") or []:
            if classify_title(j.get("title")) == "it" and best_country(j.get("location") or "") in ("DE", ""):
                names[j["company_name"]] += 1
        if not (data.get("links") or {}).get("next"):
            break
        time.sleep(3)
    return names


def yc_names():
    return {c["name"] for c in get_json(YC) if c.get("status") == "Active" and
            re.search(r"Germany|Netherlands", str(c.get("all_locations", "")))}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="todo-names.txt")
    args = ap.parse_args(argv)
    names = set(arbeitnow_names()) | yc_names()
    todo = [(p, s) for name in sorted(names) for s in slug_guesses(name)[:3] for p in ("ashby", "greenhouse", "lever")]
    with open(args.out, "w") as f:
        f.writelines(f"{p},{s}\n" for p, s in dict.fromkeys(todo))
    print(f"{len(names)} company names → {len(todo)} account guesses in {args.out}")


if __name__ == "__main__":
    main()
