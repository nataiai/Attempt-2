"""Read each job-board account the way attempt-1's collector will, and count its IT jobs by country and verdict.

Feeds (Personio, Recruitee, d.vinci, Greenhouse, Lever, Ashby) go through attempt-1's ATS readers; boards without a
feed reader (softgarden, Teamtailor, coveto) through its page readers. Polite like the collector: robots.txt is obeyed
and each host gets one request per 2–4 s. Results are appended, so an interrupted run continues where it stopped.

  python discovery/check_boards.py todo.txt research/accounts.csv --workers 12
  (todo.txt: one "platform,account" per line, e.g. "personio,zollsoft.com" or "greenhouse,celonis")
"""

import argparse
import html as htmllib
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

from common import DE_REGIONS, FIELDS, append_rows, board_url, place_country, read_rows

from jobcollector.fetch import Fetcher, FetchError
from jobcollector.language import judge
from jobcollector.model import AtsRef, Site
from jobcollector.readers.ats import READERS
from jobcollector.readers.common import Ctx, unglue
from jobcollector.readers.generic import embedded_postings, jsonld_postings, link_postings, next_page_url
from jobcollector.relevance import best_country, classify_title, in_scope, off_profile

FEEDS = {"personio", "recruitee", "dvinci", "greenhouse", "lever", "ashby"}
LISTS = {"softgarden", "teamtailor", "coveto"}
BUDGET = 120  # seconds per account

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_GENERIC = re.compile(r"^(stellenangebote|jobs?|karriere|careers?|homepage|home|startseite|offene stellen|job openings|"
                      r"open positions|vacancies|stellenbörse|jobbörse|bewerberportal|karriere ?board|karriereportal|"
                      r"jobportal|jobs bei|jobs at|trabajos en|offres d'emploi|vacatures)$", re.I)
_NOT_SITE = re.compile(r"personio|greenhouse|lever\.co|ashbyhq|recruitee|smartrecruiters|dvinci|softgarden|teamtailor|"
                       r"coveto|facebook|instagram|linkedin|xing|youtube|twitter|x\.com|tiktok|kununu|google|apple\.com|"
                       r"cookiebot|usercentrics|onetrust|w3\.org|schema\.org|gstatic|jsdelivr|cloudflare|github|vimeo|"
                       r"wistia|glassdoor|wa\.me|whatsapp|pinterest|spotify|indeed|stepstone|bit\.ly|cdn|static|assets|"
                       r"adobeaem|amazonaws|azureedge|cloudfront|akamai|hubspot|typekit|gravatar|recaptcha|hotjar|matomo",
                       re.I)


def country_of(posting):
    """ISO2 country of a job: attempt-1's detection, then German regions, then GeoNames place names."""
    c = best_country(posting.location, posting.country)
    if not c and DE_REGIONS.search(posting.location or ""):
        c = "DE"
    return c or place_country(posting.location)


def company_name(title, slug):
    """Company name from a board page title ("Jobs bei zollsoft GmbH", "Karriere Board | Wavestone Germany AG")."""
    title = re.sub(r"^(jobs|stellenangebote|karriere|careers?|offene stellen|trabajos|vacatures)\s+(bei|at|@|en|bij)\s+",
                   "", title or "", flags=re.I).strip()
    parts = [p.strip() for p in re.split(r"\s[|–-]\s|:\s", title) if p.strip()]
    parts = [re.sub(r"\s+(jobs|careers|karriere)$", "", p, flags=re.I) for p in parts]
    parts = [p for p in parts if not _GENERIC.match(p) and len(p) > 1]
    if not parts:
        return slug.split(".")[0].replace("-", " ").title()
    return parts[-1] if re.search(r"(?i)board|portal", title) else parts[0]


def website_of(html, slug, name):
    """The company's own site among the board page's links: one naming the company, else the most linked one."""
    tokens = {w for w in re.findall(r"[a-z0-9äöü]{3,}", f"{slug.split('.')[0].replace('-', ' ')} {name}".lower())
              if w not in ("gmbh", "jobs", "group", "gruppe", "careers", "karriere", "und", "the", "com")}
    hosts = Counter(urlsplit(link).netloc.lower() for link in re.findall(r'href=["\'](https?://[^"\'#?\s]+)', html or ""))
    best = sorted((not any(t in h for t in tokens), -c, h) for h, c in hosts.items() if h and not _NOT_SITE.search(h))
    return f"https://{best[0][2]}/" if best and (not best[0][0] or -best[0][1] >= 2) else ""


def _read(platform, slug, fetcher):
    url = board_url(platform, slug)
    if platform in FEEDS:
        site = Site(site_id=f"{platform}-{slug}", name=slug, company_ids=[], careers_url=url)
        ctx = Ctx(fetcher, site, deadline=time.monotonic() + BUDGET * 0.6)
        return READERS[platform](ctx, AtsRef(platform, slug, url))
    posts, seen, page_url = [], set(), url
    for _ in range(3):  # the list and up to two "next page" links
        page = fetcher.get(page_url, retries=0)
        for p in jsonld_postings(page.text, page.url) + embedded_postings(page.text, page.url) + \
                link_postings(page.text, page.url):
            if (p.url, p.title) not in seen:
                seen.add((p.url, p.title))
                posts.append(p)
        nxt = next_page_url(page.text, page.url)
        if not nxt or nxt == page_url:
            break
        page_url = nxt
    return posts


def check(platform, slug, fetcher):
    """One research row: jobs listed, IT jobs per country, their verdicts, and the company's name and website."""
    row = {f: "" for f in FIELDS}
    row.update(platform=platform, slug=slug, careers_url=board_url(platform, slug))
    try:
        posts = _read(platform, slug, fetcher)
    except FetchError as e:
        row.update(status="blocked" if "robots" in str(e) else "error", notes=str(e)[:150])
        return row
    except Exception as e:  # one broken board must not stop the scan
        row.update(status="error", notes=f"{type(e).__name__}: {str(e)[:120]}")
        return row
    it = [p for p in posts if classify_title(p.title) == "it"]
    where, scoped = Counter(), []
    for p in it:
        c = country_of(p)
        where[c if c in ("DE", "GB", "NL") else "?" if not c else "EU" if in_scope(c, []) else "X"] += 1
        if in_scope(c, []):
            scoped.append(p)
    verdicts, english = Counter(), 0
    for p in scoped:
        text = unglue(p.description or "")
        if len(text) >= 300:
            v = judge(p.title, text)
            verdicts[v.verdict] += 1
            english += v.ad_language == "en"
    row.update(jobs=len(posts), it_jobs=len(it), it_in_scope=len(scoped), it_de=where["DE"], it_gb=where["GB"],
               it_nl=where["NL"], it_eu_other=where["EU"], it_unknown=where["?"], it_non_eu=where["X"],
               highlight=verdicts["HIGHLIGHT"], check=verdicts["CHECK"], exclude=verdicts["EXCLUDE"],
               english_ads=english, on_profile=sum(not off_profile(p.title) for p in scoped),
               status="ok" if posts else "empty", notes="feed" if platform in FEEDS else "list page",
               sample_titles=" | ".join(p.title[:70] for p in scoped[:5]),
               locations=" | ".join(sorted({(p.location or "")[:40] for p in scoped})[:6]))
    if scoped:
        row["company"], row["website"] = _identity(platform, slug, fetcher)
    return row


def _identity(platform, slug, fetcher):
    name = ""
    try:
        if platform == "recruitee":
            offers = fetcher.get(f"https://{slug}.recruitee.com/api/offers/").json().get("offers") or []
            name = next((o.get("company_name") for o in offers if o.get("company_name")), "")
        elif platform == "greenhouse":
            name = fetcher.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}").json().get("name", "")
        page = fetcher.get(board_url(platform, slug), retries=0)
    except (FetchError, ValueError):
        return name or slug.split(".")[0].replace("-", " ").title(), ""
    m = _TITLE.search(page.text)
    title = htmllib.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    if not name or len(name) <= 3:
        name = company_name(title, slug)
    if platform == "personio" and _GENERIC.match(name.strip()):  # title without the name: use the job-group header
        group = re.search(r'jobs-group-header">([^<]{3,120})<', page.text)
        name = htmllib.unescape(group.group(1)).strip() if group else slug.split(".")[0].replace("-", " ").title()
    return name[:120], website_of(page.text, slug, name)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("todo")
    ap.add_argument("out")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)
    todo = [tuple(line.strip().split(",", 1)) for line in open(args.todo) if "," in line]
    done = {(r["platform"], r["slug"]) for r in read_rows(args.out)}
    todo = [t for t in dict.fromkeys(todo) if t not in done and t[0] in FEEDS | LISTS]
    print(f"{len(todo)} accounts to check ({len(done)} done before)", flush=True)
    fetcher = Fetcher(timeout=25, retries=1)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check, p, s, fetcher): (p, s) for p, s in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            append_rows(args.out, [r])
            if n % 100 == 0 or r["it_in_scope"]:
                print(f"[{n}/{len(todo)}] {r['platform']:10} {r['slug'][:28]:28} {r['status']:7} jobs {r['jobs'] or 0:>4} "
                      f"IT {r['it_jobs'] or 0:>3} DE {r['it_de'] or 0:>3} H {r['highlight'] or 0:>3} {r['company'][:30]}",
                      flush=True)


if __name__ == "__main__":
    main()
