"""Turn the checked job-board accounts into new target rows for attempt-1 (config/targets.csv format).

Keeps companies with IT jobs in Germany, the UK or the Netherlands that attempt-1 does not target yet, drops public
bodies, non-profits, schools, hospitals and staffing agencies, merges duplicate boards of one company and guesses the
segment (startup / Mittelstand / large) and area from the evidence.

  python discovery/select_new.py research/accounts.csv --out new_targets.csv
"""

import argparse
import csv
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from common import ATTEMPT1, USER_AGENT, norm_name, num, place_country, read_rows, reg_domain

from jobcollector.detect import detect
from jobcollector.registry import TARGET_FIELDS
from jobcollector.relevance import area

FEED_PLATFORMS = {"personio", "recruitee", "dvinci", "greenhouse", "lever", "ashby"}
STARTUP_BOARDS = {"greenhouse", "lever", "ashby"}  # the job systems of VC-backed startups and scale-ups
ATS_DOMAINS = {"personio.de", "personio.com", "greenhouse.io", "lever.co", "ashbyhq.com", "recruitee.com",
               "smartrecruiters.com", "softgarden.io", "softgarden.de", "join.com", "dvinci-hr.com", "dvinci-easy.com",
               "teamtailor.com", "workable.com", "myworkdayjobs.com", "coveto.de", "umantis.com", "onlyfy.jobs"}

NOT_A_COMPANY = re.compile(
    r"\bggmbh\b|gemeinn(ü|ue)tzig|\be\.\s?v\.?(?=\s|$|,)|eingetragener verein|stiftung|foundation|"  # non-profits
    r"universit|hochschule|fachhochschule|\bschule\b|school|\bkita\b|"  # education
    r"klinik|krankenhaus|hospital|pflege|senioren|diakon|caritas|\bawo\b|\bdrk\b|rotes kreuz|lebenshilfe|johanniter|"
    r"malteser|agaplesion|alexianer|helios|asklepios|vivantes|"  # health and social care
    r"\bstadt\b|stadtwerke|landkreis|kreisverwaltung|gemeinde|bezirksamt|ministerium|bundesamt|landesamt|bundesanstalt|"
    r"polizei|bundeswehr|beh(ö|oe)rde|kommunal|zweckverband|landesbetrieb|jobcenter|"  # public sector
    r"kirche|bistum|di(ö|oe)zese|evangelisch|katholisch|"  # churches
    r"\binstitut\b|institute of|forschungszentrum|fraunhofer|max-planck|helmholtz|leibniz|"  # research bodies
    r"personal(dienst|vermittlung|beratung|service|partner|management|leasing)|zeitarbeit|arbeitnehmer(ü|ue)berlassung|"
    r"recruit|staffing|headhunt|executive search|\btalents?\b|personnel|jobgether|\bhays\b|randstad|ferchau|brunel|"
    r"amadeus fire|orizon|adecco|manpower|experis|akkodis|job wrapping",  # staffing agencies and recruiters
    re.I)
AREA_GROUP = {"Embedded": "hardware", "Data & AI": "data/ai", "SAP & ERP": "other IT", "IT support & admin": "other IT",
              "Security": "other IT", "IT (other)": "other IT"}


def covered(attempt1=ATTEMPT1):
    """Refs, domains and names attempt-1 already targets (config/targets.csv and the results branch)."""
    refs, domains, names = set(), set(), set()
    rows = read_rows(Path(attempt1) / "config/targets.csv")
    for r in rows:
        for url in (r["careers_url"], r["website"]):
            d = reg_domain(url)
            if d and d not in ATS_DOMAINS:
                domains.add(d)
            refs |= {(x.platform, x.slug.split(".")[0].lower()) for x in detect(url or "") if x.slug}
        if r["platform"] and r["slug"]:
            refs.add((r["platform"], r["slug"].split(".")[0].lower()))
        names.add(norm_name(r["name"]))
    found = subprocess.run(["git", "-C", str(attempt1), "show", "origin/results:discovered_sources.csv"],
                           capture_output=True, text=True).stdout
    for r in csv.DictReader(found.splitlines()):
        for p, s in zip((r.get("platform") or "").split(", "), (r.get("slug") or "").split(", ")):
            if p and s:
                refs.add((p, s.split(".")[0].lower()))
    return refs, domains, names, {r["site_id"] for r in rows}


def exploration_ids(attempt1=ATTEMPT1):
    """Ids of companies in attempt-1's data/companies.csv, by normalized name and by domain (to keep the link)."""
    by_name, by_domain = {}, {}
    for r in read_rows(Path(attempt1) / "data/companies.csv"):
        by_name.setdefault(norm_name(r["company"]), r["id"])
        for col in ("website", "domain"):
            d = reg_domain(r.get(col) or "")
            if "." in d and d not in ATS_DOMAINS:
                by_domain.setdefault(d, r["id"])
    return by_name, by_domain


def in_target_countries(r):
    """IT jobs in DE/GB/NL; jobs whose place was not recognised count when a listed place or the company is German."""
    if num(r["it_de"]) + num(r["it_gb"]) + num(r["it_nl"]):
        return True
    if not num(r["it_unknown"]):
        return False
    places = {place_country(loc) for loc in (r["locations"] or "").split(" | ") if loc.strip()}
    if places & {"DE", "GB", "NL"}:
        return True
    foreign = re.search(r"austria|österreich|schweiz|switzerland|\.at/?$|\.ch/?$|-at$",
                        f"{r['company']} {r['slug']} {r['website'] or ''}", re.I)
    german = re.search(r"\.de/?$", r["website"] or "") or re.search(r"\b(gmbh|ag|kg|se)\b", r["company"] or "", re.I)
    return bool(german and not foreign and not places - {""} and
                r["platform"] in ("softgarden", "coveto", "personio", "dvinci"))


_NAME_NOISE = re.compile(r"^(dein einstieg bei( der| dem)?|karriere bei( der)?|jobs bei|jobs at|careers at|join)\s+|"
                         r"\s+(career|careers|karriere|jobs|en|de|recruiting)$", re.I)


def clean_name(name, slug):
    """Board titles carry extras ("MaibornWolff Career", "IONOS EN"); fall back to the account name if nothing is left."""
    n = name or ""
    for _ in range(2):
        n = _NAME_NOISE.sub("", n).strip(" -|–")
    if len(n) < 2 or re.fullmatch(r"(?i)(jobs?|careers?|karriere|stellenangebote|trabajos|vacatures)( bei| at| en)?|"
                                  r"home(page)?", n):
        n = slug.split(".")[0].replace("-", " ").title()
    return n


def wikidata_sizes(domains, cache_path):
    """domain → {"employees", "founded"} from Wikidata's official-website statements (only well-known companies)."""
    cache = json.loads(Path(cache_path).read_text()) if Path(cache_path).exists() else {}
    todo = [d for d in domains if d not in cache]
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        values = " ".join(f"<{s}://{w}{d}{e}>" for d in batch for s in ("http", "https") for w in ("", "www.")
                          for e in ("", "/"))
        query = ("SELECT ?site ?emp ?inc WHERE { VALUES ?site { %s } ?item wdt:P856 ?site . "
                 "OPTIONAL { ?item wdt:P1128 ?emp } OPTIONAL { ?item wdt:P571 ?inc } }" % values)
        body = urllib.parse.urlencode({"query": query, "format": "json"}).encode()
        for attempt in range(3):
            try:
                req = urllib.request.Request("https://query.wikidata.org/sparql", data=body, headers={
                    "User-Agent": USER_AGENT, "Accept": "application/sparql-results+json",
                    "Content-Type": "application/x-www-form-urlencoded"})
                data = json.loads(urllib.request.urlopen(req, timeout=120).read())
                break
            except Exception as e:
                print(f"wikidata batch {i // 40}: {e}")
                time.sleep(10 * (attempt + 1))
        else:
            continue
        for d in batch:
            cache.setdefault(d, {})
        for b in data["results"]["bindings"]:
            d = reg_domain(b["site"]["value"])
            rec = cache.setdefault(d, {})
            if "emp" in b:
                rec["employees"] = max(rec.get("employees", 0), num(b["emp"]["value"]))
            if "inc" in b and num(b["inc"]["value"][:4]):
                rec["founded"] = min(rec.get("founded") or 9999, num(b["inc"]["value"][:4]))
        time.sleep(2)
    Path(cache_path).write_text(json.dumps(cache, indent=0, sort_keys=True))
    return cache


def segment(r, facts):
    """(segment, why): startup / Mittelstand / large from Wikidata facts if known, else a marked guess from the board."""
    emp, founded = facts.get("employees", 0), facts.get("founded") or 0
    if emp >= 5000:
        return "large", f"{emp:,} staff (Wikidata)"
    if founded >= 2010:
        return "startup", f"founded {founded}" + (f", {emp:,} staff" if emp else "") + " (Wikidata)"
    if emp or founded:
        return "Mittelstand", ", ".join(x for x in (f"{emp:,} staff" if emp else "", f"founded {founded}" if founded
                                                     else "") if x) + " (Wikidata)"
    if num(r["jobs"]) >= 300:
        return "large?", f"{r['jobs']} open jobs"
    en, judged = num(r["english_ads"]), num(r["highlight"]) + num(r["check"]) + num(r["exclude"])
    if r["platform"] in STARTUP_BOARDS:
        return "startup?", f"{r['platform']} board"
    if judged and en * 2 > judged:
        return "startup?", "English-first ads"
    if judged:
        return "Mittelstand?", "German-first ads"
    return "SME?", f"{r['platform']} board"


def size_of(emp):
    return "" if not emp else "small" if emp < 250 else "medium" if emp < 5000 else "large"


def area_of(r):
    names = Counter(area(t) for t in (r["sample_titles"] or "").split(" | ") if t)
    if not names:
        return ""
    group = Counter(AREA_GROUP.get(a, "software") for a in names.elements()).most_common(1)[0][0]
    return group


def site_id(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
                  .replace("ß", "ss")).strip("-")[:30].strip("-") or "company"
    sid, i = base, 2
    while sid in taken:
        sid = f"{base[:27]}-{i}"
        i += 1
    taken.add(sid)
    return sid


def select(rows, attempt1=ATTEMPT1, wikidata_cache=None):
    refs, domains, names, taken = covered(attempt1)
    by_name, by_domain = exploration_ids(attempt1)
    stats = Counter()
    kept = []
    for r in rows:
        if r["status"] != "ok" or not num(r["it_in_scope"]):
            continue
        stats["accounts with IT jobs in Europe"] += 1
        if not in_target_countries(r):
            stats["dropped: no IT job in DE/GB/NL"] += 1
            continue
        if not num(r["on_profile"]):
            stats["dropped: only IT support/admin, sales or network jobs"] += 1
            continue
        r["company"] = clean_name(r["company"], r["slug"])
        if NOT_A_COMPANY.search(r["company"]) or NOT_A_COMPANY.search(r["slug"].replace("-", " ")):
            stats["dropped: public body, non-profit, school, care or agency"] += 1
            continue
        dom, key = reg_domain(r["website"]), norm_name(r["company"]) or norm_name(r["slug"])
        if (r["platform"], r["slug"].split(".")[0].lower()) in refs or (dom and dom in domains) or (key and key in names):
            stats["dropped: already an attempt-1 target"] += 1
            continue
        kept.append(r)
    # One row per company: the board with the most IT jobs wins; boards share a company by website or name.
    kept.sort(key=lambda r: (-(num(r["it_de"]) + num(r["it_gb"]) + num(r["it_nl"]) + num(r["it_unknown"])),
                             -num(r["jobs"]), r["platform"] not in FEED_PLATFORMS))
    chosen, seen = [], set()
    for r in kept:
        dom = reg_domain(r["website"])
        keys = {f"{r['platform']}:{r['slug'].split('.')[0]}", norm_name(r["company"]) or norm_name(r["slug"]),
                dom if dom not in ATS_DOMAINS else ""} - {""}
        if keys & seen:
            stats["duplicate boards merged"] += 1
            continue
        seen |= keys
        chosen.append(r)
    facts = wikidata_sizes(sorted({reg_domain(r["website"]) for r in chosen if r["website"]}), wikidata_cache) \
        if wikidata_cache else {}
    out = []
    for r in sorted(chosen, key=lambda r: (-num(r["it_de"]), -num(r["it_in_scope"]), r["company"].lower())):
        f = facts.get(reg_domain(r["website"]), {}) if r["website"] else {}
        seg, why = segment(r, f)
        if seg.startswith("large") and num(r["it_de"]) == 0:
            stats["dropped: large board without German IT jobs"] += 1
            continue
        dom = reg_domain(r["website"])
        cid = (dom and by_domain.get(dom)) or (norm_name(r["company"]) and by_name.get(norm_name(r["company"]))) or ""
        judged = num(r["highlight"]) + num(r["check"]) + num(r["exclude"])
        where = ", ".join(f"{c} {num(r['it_' + c.lower()])}" for c in ("DE", "GB", "NL") if num(r["it_" + c.lower()]))
        de = num(r["it_de"])
        tier = 1 if de >= 3 or (de and (num(r["highlight"]) or seg.startswith("startup"))) else 2  # Germany first
        notes = (f"new tier {tier} ({seg} – {why}): {num(r['it_in_scope'])} IT jobs" + (f" ({where})" if where else "") +
                 (f", {r['highlight']} Highlight / {r['check']} Check / {r['exclude']} Exclude" if judged else "") +
                 f" on {r['platform']}")
        out.append({"site_id": site_id(r["company"] or r["slug"], taken), "name": r["company"] or r["slug"],
                    "company_ids": cid or "", "careers_url": r["careers_url"], "website": r["website"],
                    "platform": r["platform"], "slug": r["slug"], "search_url": "",
                    "countries": "DE;GB;NL" if seg.startswith("large") else "", "keywords": "", "match": "",
                    "size": size_of(f.get("employees")), "area": area_of(r), "notes": notes, "_segment": seg,
                    "_tier": tier})
    for i, row in enumerate(out, 1):
        row["company_ids"] = row["company_ids"] or f"N{i:04d}"  # not in attempt-1's data yet
    stats["new targets"] = len(out)
    return out, stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("accounts", nargs="+", help="research CSVs written by check_boards.py")
    ap.add_argument("--out", default="new_targets.csv")
    ap.add_argument("--wikidata", default="research/wikidata.json", help="cache of sizes looked up on Wikidata ('' = off)")
    args = ap.parse_args(argv)
    rows = [r for path in args.accounts for r in read_rows(path)]
    out, stats = select(rows, wikidata_cache=args.wikidata or None)
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TARGET_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)
    for k, v in stats.items():
        print(f"{v:6}  {k}")
    print(Counter(r["_segment"] for r in out), Counter(r["_tier"] for r in out), Counter(r["platform"] for r in out))


if __name__ == "__main__":
    main()
