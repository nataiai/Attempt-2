"""Shared pieces of the discovery scripts: access to attempt-1's package, the result columns and small helpers."""

import csv
import io
import os
import re
import sys
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
# The scripts reuse attempt-1's readers, IT filter and language verdict, so the companies found here are checked
# exactly the way the collector will read them. Point ATTEMPT1 at a checkout of attempt-1 (default: next to this repo).
ATTEMPT1 = Path(os.environ.get("ATTEMPT1", HERE.parents[1] / "attempt-1"))
if (ATTEMPT1 / "jobcollector").is_dir() and str(ATTEMPT1) not in sys.path:
    sys.path.insert(0, str(ATTEMPT1))

USER_AGENT = "Mozilla/5.0 (compatible; JobCollector/0.1; personal job search; respects robots.txt)"

# One row per job-board account checked (research/accounts.csv).
FIELDS = ["platform", "slug", "company", "website", "careers_url", "jobs", "it_jobs", "it_in_scope", "it_de", "it_gb",
          "it_nl", "it_eu_other", "it_unknown", "it_non_eu", "highlight", "check", "exclude", "english_ads",
          "on_profile", "status", "notes", "sample_titles", "locations"]

BOARD_URLS = {
    "greenhouse": "https://job-boards.greenhouse.io/{s}", "lever": "https://jobs.lever.co/{s}",
    "ashby": "https://jobs.ashbyhq.com/{s}", "recruitee": "https://{s}.recruitee.com/",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{s}", "dvinci": "https://{s}.dvinci-hr.com/de/jobs",
    "softgarden": "https://{s}.softgarden.io/de/vacancies", "teamtailor": "https://{s}.teamtailor.com/jobs",
    "coveto": "https://{s}.coveto.de/public/jobs/",
}


def board_url(platform, slug):
    """Public job list of an account; Personio slugs carry their domain ("acme.de" → acme.jobs.personio.de)."""
    if platform == "personio":
        name, _, tld = slug.partition(".")
        return f"https://{name}.jobs.personio.{tld or 'de'}/"
    return BOARD_URLS.get(platform, "").format(s=slug)


def read_rows(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def append_rows(path, rows, fields=FIELDS):
    new = not Path(path).exists()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def num(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


# ---- names and domains ---------------------------------------------------------------------------

_LEGAL = re.compile(r"\b(gmbh|mbh|ag|se|kg|kgaa|co|ug|haftungsbeschr\w*|inc|ltd|llc|limited|plc|bv|b\.v|sa|sas|srl|oy|"
                    r"ab|as|aps|nv|gruppe|group|holding|deutschland|germany|international|europe|company|corporation|"
                    r"corp|und|and|the|partg|mbb|technologies|technology|software|solutions|systems|services)\b")


def norm_name(name):
    """Company name without legal form and filler words, for matching ("Comma Soft AG" → "commasoft")."""
    n = (name or "").lower().replace("&", " ").replace("+", " ")
    n = re.sub(r"\(.*?\)", " ", n)
    n = _LEGAL.sub(" ", n)
    return re.sub(r"[^a-z0-9äöüß]+", "", n)


def reg_domain(url):
    """Registrable domain of a URL ("https://jobs.acme.co.uk/x" → "acme.co.uk")."""
    url = url or ""
    host = urlsplit(url if "//" in url else "https://" + url).netloc.lower().split(":")[0]
    host = re.sub(r"^www\d?\.", "", host)
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "ac", "gv") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


# ---- where a job is ------------------------------------------------------------------------------

DE_REGIONS = re.compile(r"bayern|baden-w(ü|ue)rttemberg|brandenburg|bremen|hessen|mecklenburg|niedersachsen|"
                        r"nordrhein-westfalen|\bnrw\b|rheinland-pfalz|saarland|sachsen|th(ü|ue)ringen|schleswig-holstein|"
                        r"deutschlandweit|bundesweit|ruhrgebiet", re.I)
GEONAMES_URL = "https://download.geonames.org/export/dump/cities1000.zip"
_PLACES = {}


def load_places(cache=HERE / ".cache" / "cities1000.zip"):
    """Place name → (country, population) of its largest namesake, from GeoNames (CC BY 4.0, places > 1,000 people)."""
    if _PLACES:
        return _PLACES
    cache = Path(cache)
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(GEONAMES_URL, headers={"User-Agent": USER_AGENT})
        cache.write_bytes(urllib.request.urlopen(req, timeout=300).read())
    with zipfile.ZipFile(cache) as z:
        text = z.read("cities1000.txt").decode("utf-8")
    for line in io.StringIO(text):
        cols = line.rstrip("\n").split("\t")
        if len(cols) < 15:
            continue
        country, pop = cols[8], num(cols[14])
        for name in {cols[1].lower(), cols[2].lower()}:
            if name and pop > _PLACES.get(name, ("", -1))[1]:
                _PLACES[name] = (country, pop)
    return _PLACES


def place_country(location, places=None):
    """Country of the first location part that is a known place name ("Oberkochen, Hybrid" → "DE"), or ""."""
    places = places if places is not None else load_places()
    for part in re.split(r"[,;/|()\n]| - | – ", location or ""):
        part = re.sub(r"\b(remote|hybrid|vor ort|onsite|on-site|home ?office)\b", " ", part.strip().lower())
        part = re.sub(r"^\d{4,5}\s+", "", part).strip()  # postcode
        hit = places.get(part)
        if hit:
            return hit[0]
    return ""
