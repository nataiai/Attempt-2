"""Tests without network. The parts that reuse attempt-1 are skipped when its package is not importable."""

import pytest

import common
from cc_accounts import account_of, blocks_for

common._PLACES.update({"oberkochen": ("DE", 8000), "wien": ("AT", 1900000), "berlin": ("DE", 3600000)})


# ---- Common Crawl index ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefix,key,account", [
    ("de,personio,jobs,", "de,personio,jobs,acme-gmbh)/job/2234627?language=de", "acme-gmbh"),
    ("com,recruitee,", "com,recruitee,televic)/o/embedded-developer", "televic"),
    ("io,greenhouse,job-boards)/", "io,greenhouse,job-boards)/celonis/jobs/7012345", "celonis"),
    ("com,ashbyhq,jobs)/", "com,ashbyhq,jobs)/elevenlabs", "elevenlabs"),
    ("io,greenhouse,job-boards)/", "io,greenhouse,job-boards)/robots.txt", ""),
    ("io,softgarden,", "io,softgarden,pcw-cdn)/static/app.js", ""),
    ("co,lever,jobs)/", "co,lever,jobs)/index.html", ""),
])
def test_account_of(prefix, key, account):
    assert account_of(prefix, key) == account


def test_blocks_for_includes_the_block_before_the_first_match():
    keys = ["de,otto)/", "de,personio,jobs,aerowest)/", "de,personio,jobs,gauly)/", "de,pfeiffer)/"]
    assert list(blocks_for(keys, "de,personio,jobs,")) == [0, 1, 2]
    assert list(blocks_for(keys, "de,zalando)/")) == [3]


# ---- places ---------------------------------------------------------------------------------------

def test_place_country_reads_the_first_known_place():
    assert common.place_country("Oberkochen, Hybrid") == "DE"
    assert common.place_country("73447 Oberkochen") == "DE"
    assert common.place_country("Wien / Remote") == "AT"
    assert common.place_country("Anywhere") == ""


def test_norm_name_and_domain():
    assert common.norm_name("Comma Soft AG") == common.norm_name("comma soft") == "commasoft"
    assert common.reg_domain("https://jobs.acme.co.uk/x") == "acme.co.uk"
    assert common.reg_domain("www.zollsoft.de") == "zollsoft.de"


# ---- reading a board (attempt-1's readers) --------------------------------------------------------

PERSONIO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
<position><id>1</id><office>Berlin</office><name>Backend Engineer (Python)</name>
<jobDescriptions><jobDescription><name>About you</name><value><![CDATA[<p>You build our data platform with Python
and Postgres. You work in an international team; our company language is English. You have 2+ years of experience with
backend services, you write tests, and you enjoy owning features from design to production. German is not required.
We offer hybrid work in Berlin, a learning budget and a relocation package for people moving to Germany.</p>]]></value>
</jobDescription></jobDescriptions></position>
<position><id>2</id><office>Oberkochen</office><name>Softwareentwickler C# (m/w/d)</name>
<jobDescriptions><jobDescription><name>Ihr Profil</name><value><![CDATA[<p>Sie entwickeln unsere Software für
Messmaschinen in C# und .NET weiter. Sie haben ein abgeschlossenes Studium der Informatik oder eine vergleichbare
Ausbildung und Erfahrung mit WPF. Sie arbeiten eng mit der Konstruktion und dem Service zusammen und unterstützen bei
Bedarf unsere Inbetriebnahmen. Gute Deutschkenntnisse setzen wir voraus. Wir bieten flexible Arbeitszeiten.</p>]]></value>
</jobDescription></jobDescriptions></position>
<position><id>3</id><office>Berlin</office><name>Office Manager (m/w/d)</name></position>
</workzag-jobs>"""


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, params=None, headers=None, json_body=None, retries=None):
        from jobcollector.fetch import FetchError, Response

        for key, text in self.pages.items():
            if url.endswith(key):
                return Response(url, 200, text)
        raise FetchError(f"HTTP 404 for {url}")


def test_check_counts_it_jobs_by_country_and_verdict():
    pytest.importorskip("jobcollector")
    from check_boards import check

    page = '<html><head><title>Jobs bei Acme Messtechnik GmbH</title></head><body>' \
           '<a href="https://www.acme-messtechnik.de/impressum">Impressum</a></body></html>'
    row = check("personio", "acme.de", FakeFetcher({"/xml": PERSONIO_XML, "acme.jobs.personio.de/": page}))
    assert (row["status"], row["jobs"], row["it_jobs"], row["it_de"], row["it_unknown"]) == ("ok", 3, 2, 2, 0)
    assert (row["highlight"], row["check"], row["english_ads"]) == (1, 1, 1)
    assert row["company"] == "Acme Messtechnik GmbH"
    assert row["website"] == "https://www.acme-messtechnik.de/"


def test_check_reports_a_missing_board():
    pytest.importorskip("jobcollector")
    from check_boards import check

    row = check("personio", "gone.com", FakeFetcher({}))
    assert row["status"] == "error" and "404" in row["notes"]


@pytest.mark.parametrize("title,slug,name", [
    ("Jobs bei zollsoft GmbH", "zollsoft.com", "zollsoft GmbH"),
    ("Karriere Board | Wavestone Germany AG", "wavestone", "Wavestone Germany AG"),
    ("ElevenLabs Jobs", "elevenlabs", "ElevenLabs"),
    ("", "data-solutions-gmbh", "Data Solutions Gmbh"),
])
def test_company_name(title, slug, name):
    pytest.importorskip("jobcollector")
    from check_boards import company_name

    assert company_name(title, slug) == name


# ---- choosing new targets -------------------------------------------------------------------------

def row(**kw):
    base = {f: "" for f in common.FIELDS}
    base.update(status="ok", platform="personio", slug="acme.de", company="Acme GmbH", jobs="12", it_in_scope="3",
                it_de="3", on_profile="3")
    base.update({k: str(v) for k, v in kw.items()})
    return base


@pytest.mark.parametrize("name,excluded", [
    ("Klinikum Neumarkt", True), ("Hays AG", True), ("Stadtwerke Bielefeld GmbH", True), ("DRK Kreisverband", True),
    ("Leibniz-Institut für Neue Materialien gGmbH", True), ("mi.ro Personalvermittlung", True),
    ("Gambit Consulting GmbH", False), ("Isar Aerospace SE", False), ("Comma Soft AG", False), ("STARK", False),
])
def test_not_a_company(name, excluded):
    pytest.importorskip("jobcollector")
    from select_new import NOT_A_COMPANY

    assert bool(NOT_A_COMPANY.search(name)) is excluded


@pytest.mark.parametrize("name,slug,clean", [
    ("MaibornWolff Career", "maibornwolff-career.de", "MaibornWolff"),
    ("IONOS EN", "ionos2", "IONOS"),
    ("Jobs bei", "consileon.de", "Consileon"),
    ("Dein Einstieg bei der ]init[ AG", "init.de", "]init[ AG"),
])
def test_clean_name(name, slug, clean):
    pytest.importorskip("jobcollector")
    from select_new import clean_name

    assert clean_name(name, slug) == clean


def test_segment_prefers_facts_and_marks_guesses():
    pytest.importorskip("jobcollector")
    from select_new import segment

    assert segment(row(), {"employees": 12000})[0] == "large"
    assert segment(row(), {"founded": 2019, "employees": 80})[0] == "startup"
    assert segment(row(platform="greenhouse"), {})[0] == "startup?"
    assert segment(row(jobs=420), {})[0] == "large?"
    assert segment(row(highlight=3, check=1, english_ads=4), {})[0] == "startup?"
    assert segment(row(check=3, english_ads=0), {})[0] == "Mittelstand?"
    assert segment(row(), {})[0] == "SME?"


def test_in_target_countries():
    pytest.importorskip("jobcollector")
    from select_new import in_target_countries

    assert in_target_countries(row())
    assert in_target_countries(row(it_de=0, it_unknown=2, locations="Oberkochen"))
    assert not in_target_countries(row(it_de=0, it_unknown=2, locations="Wien"))
    assert in_target_countries(row(it_de=0, it_unknown=1, platform="softgarden", company="Bartec GmbH"))
    assert not in_target_countries(row(it_de=0, it_eu_other=4))
