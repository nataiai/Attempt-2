"""List the job-board accounts (Personio, Recruitee, softgarden, Greenhouse …) that a Common Crawl snapshot has seen.

Each account is one company's job board, so this reaches companies that no fair list or company register names –
mostly Mittelstand and startups, who use these job systems. Only the crawl's index is read: its cluster.idx says
which compressed CDX blocks hold the job-system hosts, and only those blocks are fetched (a few MB per snapshot).

  python discovery/cc_accounts.py CC-MAIN-2026-43 CC-MAIN-2026-39 --out research/cc_accounts.json
  python discovery/cc_accounts.py --todo todo.txt     # "platform,account" lines for check_boards.py
"""

import argparse
import bisect
import gzip
import json
import re
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

from common import USER_AGENT

DATA = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/"
# platform → SURT prefix of its job boards. Ending in "," = the account is the subdomain, else the first path part.
PREFIXES = {
    "personio.de": "de,personio,jobs,", "personio.com": "com,personio,jobs,", "recruitee": "com,recruitee,",
    "dvinci": "com,dvinci-hr,", "softgarden": "io,softgarden,", "coveto": "de,coveto,", "teamtailor": "com,teamtailor,",
    "join": "com,join)/companies/", "greenhouse": "io,greenhouse,boards)/", "greenhouse.jb": "io,greenhouse,job-boards)/",
    "lever": "co,lever,jobs)/", "ashby": "com,ashbyhq,jobs)/", "smartrecruiters": "com,smartrecruiters,jobs)/",
    "workable": "com,workable,apply)/",
}
NOT_ACCOUNTS = {"robots.txt", "sitemap.xml", "embed", "api", "www", "jobs", "careers", "career", "app", "apps", "support",
                "help", "static", "assets", "cdn", "integrations", "tt", "login", "signup", "favicon.ico", "js", "css",
                "v1", "en", "de", "search", "widget", "widgets"}
_BAD = re.compile(r"^pcw-|-tile$|cdn|\.(txt|xml|ico|js|css|png|jpe?g|svg)$")


def account_of(prefix, key):
    """The account name in one SURT key below a prefix, or ""."""
    rest = key[len(prefix):]
    if prefix.endswith(","):
        host = rest.split(")", 1)[0]
        name = host.split(",")[-1] if "," in host else host
    else:
        name = rest.split("/", 1)[0].split("?", 1)[0]
    name = name.strip().lower()
    if not re.fullmatch(r"[\w.%-]{2,80}", name) or name in NOT_ACCOUNTS or _BAD.search(name):
        return ""
    if not prefix.endswith(",") and "." in name:  # path accounts never contain a dot; "foo.html" is a page
        return ""
    return name


def load_cluster(path):
    keys, blocks = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            key, rest = line.split(" ", 1)
            cols = rest.rstrip("\n").split("\t")
            if len(cols) >= 4:
                keys.append(key)
                blocks.append((cols[1], int(cols[2]), int(cols[3])))
    return keys, blocks


def blocks_for(keys, prefix):
    """Indexes of the CDX blocks that can hold keys starting with prefix (the block before the first match, too)."""
    i = max(0, bisect.bisect_left(keys, prefix) - 1)
    j = i
    while j + 1 < len(keys) and keys[j + 1].startswith(prefix):
        j += 1
    return range(i, j + 1)


def fetch(url, byte_range=None, tries=5):
    headers = {"User-Agent": USER_AGENT}
    if byte_range:
        headers["Range"] = "bytes=%d-%d" % byte_range
    for attempt in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300).read()
        except Exception as e:  # the crawl's storage answers 503 when busy
            print(f"retry {url}: {e}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"could not read {url}")


def accounts(crawl, cluster_path, platforms=PREFIXES):
    """platform → {account: [captures, captures in German]} for one crawl."""
    keys, blocks = load_cluster(cluster_path)
    base = DATA.format(crawl=crawl)
    found = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for platform, prefix in platforms.items():
        for n in blocks_for(keys, prefix):
            name, offset, length = blocks[n]
            for line in gzip.decompress(fetch(base + name, (offset, offset + length - 1))).decode("utf-8", "replace").splitlines():
                key = line.split(" ", 1)[0]
                acc = account_of(prefix, key) if key.startswith(prefix) else ""
                if acc:
                    rec = found[platform][acc]
                    rec[0] += 1
                    rec[1] += bool(re.search(r'"languages": "[^"]*deu', line))
        print(f"{crawl} {platform:16} {len(found[platform]):6} accounts", flush=True)
    return found


def merge(into, found):
    for platform, accs in found.items():
        for acc, (n, deu) in accs.items():
            rec = into.setdefault(platform, {}).setdefault(acc, [0, 0])
            rec[0] += n
            rec[1] += deu
    return into


CHECKABLE = {"personio", "recruitee", "dvinci", "softgarden", "teamtailor", "coveto", "greenhouse", "lever", "ashby"}


def todo_lines(accounts):
    """'platform,account' lines for check_boards.py (Personio accounts keep their domain: 'acme.de')."""
    out = []
    for key, names in sorted(accounts.items()):
        platform, _, variant = key.partition(".")
        if platform in CHECKABLE:
            out += [f"{platform},{n}.{variant}" if platform == "personio" else f"{platform},{n}" for n in sorted(names)]
    return list(dict.fromkeys(out))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("crawls", nargs="*", help="crawl ids, e.g. CC-MAIN-2026-43 (see index.commoncrawl.org)")
    ap.add_argument("--cache", default="discovery/.cache")
    ap.add_argument("--out", default="research/cc_accounts.json")
    ap.add_argument("--todo", help="write the accounts of --out as a to-do list for check_boards.py and stop")
    args = ap.parse_args(argv)
    merged = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else {}
    if args.todo:
        lines = todo_lines(merged)
        Path(args.todo).write_text("".join(f"{x}\n" for x in lines))
        print(f"{len(lines)} accounts written to {args.todo}")
        return
    for crawl in args.crawls:
        cluster = Path(args.cache) / f"cluster-{crawl}.idx"
        if not cluster.exists():
            cluster.parent.mkdir(parents=True, exist_ok=True)
            cluster.write_bytes(fetch(DATA.format(crawl=crawl) + "cluster.idx"))  # ~100 MB
        merge(merged, accounts(crawl, cluster))
        cluster.unlink()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(merged, sort_keys=True))
    print({p: len(a) for p, a in merged.items()})


if __name__ == "__main__":
    main()
