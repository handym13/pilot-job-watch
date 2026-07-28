#!/usr/bin/env python3
"""
Pilot job digest: simulator seat-support plus line flying jobs.

Runs once daily. Sweeps a fixed watchlist, verifies every posting is still
live before including it, applies hard filters, ranks by geography, and writes
docs/index.html, which GitHub Pages serves as the search page.

The sidebar tip and history fact rotate daily.
No secrets required. Run locally with: python digest.py
"""

import json, os, re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path(__file__).parent / "seen.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; job-digest/1.0)"}
TIMEOUT = 25

# ---------------------------------------------------------------- filters

# Only four things get a posting dropped. Everything else is a ranking signal,
# so the page shows all US openings and sorts them rather than hiding them.

# "Qualified now" has to mean he actually meets it today, not that it is close.
# Update CURRENT_HOURS as he flies and the bands move with him.
CURRENT_HOURS = 650
BAND_NOW = CURRENT_HOURS    # a stated minimum at or under this, he meets it
BAND_SOON = 1250            # stated, above his time, but reachable this year
MAX_MINIMUM = 1250          # above this he would already be at Horizon, so drop it

# A URL that still resolves is not the same as a job that is still open. Old
# social posts and archived listings live forever. Anything with a readable date
# older than this is dropped outright.
MAX_POSTING_AGE_DAYS = 90

# He already holds a CFI job in California. Instructing is only worth a look if it
# comes with the move he actually wants, which is Hawaii. Everywhere else it is a
# sideways step, so instructor titles are dropped outside tier 3.
INSTRUCTOR_TITLES = [r"flight instructor", r"\bCFI\b", r"\bCFII\b", r"\bMEI\b",
                     r"instructor pilot", r"ground instructor", r"\binstructor\b"]
INSTRUCTING_OK_IN_TIERS = (3,)     # Hawaii only

EXCLUDE = {
    "rotorcraft": ["rotorcraft", "helicopter", "anti-torque", "autorotation",
                   "as350", "bell 407", "ec135", "s-76", "r44", "r66"],
    "multi-year commitment": ["two years", "2 years", "two-year", "24 month", "24-month",
                              "program spans", "training contract", "training bond",
                              "pre-determined number of support sessions",
                              "predetermined number of support sessions"],
    "outside US": ["united kingdom", "farnborough", "le bourget", "dubai", "singapore",
                   ", gb,", ", fr,", ", ae,", ", sg,", ", au,", ", nz,", ", de,"],
}

# A school selling training and an operator hiring a pilot use much of the same
# vocabulary. These phrases only appear on the selling side. Any hit drops the row.
# Aviation employers hire far more non-pilots than pilots. A careers page that
# says "now hiring" is usually not hiring a pilot.
NOT_A_PILOT_ROLE = [
    "mechanic", "a&p ", "a & p", "airframe and powerplant", "avionics",
    "maintenance technician", "maintenance controller", "aircraft technician",
    "dispatcher", "flight dispatcher", "load planner", "ramp agent", "ramp service",
    "line service", "lineman", "fueler", "customer service agent", "gate agent",
    "flight attendant", "cabin crew", "reservations", "ticket agent",
    "accountant", "bookkeeper", "receptionist", "marketing manager",
    "sales representative", "office manager", "parts clerk", "detailer",
    "cleaner", "janitorial", "security officer", "warehouse",
]

TRAINING_PROGRAM = [
    # Commercial and financial language. A school selling seats says these; an
    # operator hiring a pilot never does.
    "tuition", "enroll", "enrollment", "admissions", "financing available",
    "financing options", "loan options", "program cost", "course fee",
    "training package", "zero to hero", "zero-to-hero", "request info",
    "schedule a tour", "prospective student",
    # Product names for training sold as a package.
    "become a pilot", "become a commercial pilot", "become a cfi", "become an airline",
    "start your aviation career", "start your career in aviation",
    "cadet program", "pathway program", "professional pilot program",
    "career pilot program", "career track program",
    # Deliberately NOT here, because real CFI job descriptions use them:
    #   "our students"      instruct our students
    #   "student pilot"     train student pilots
    #   "discovery flight"  conduct discovery flights
    #   "flight training program"  our Part 141 flight training program
    # Dropping a genuine Hawaii or Alaska instructor job costs more than showing
    # one extra program, so those stay out of this list.
]

# Pay-to-fly is not hidden, it is routed to its own section and sorted by price.
# Buying multi time in a light twin is ordinary. Buying jet SIC time is viewed
# skeptically by some recruiters. The page shows both; the label says which.
PAYFLY_MARKERS = ["pay-to-fly", "pay to fly", "time building", "time-building",
                  "hour building", "hour-building", "sic program", "block time",
                  "purchase", "tuition", "cost to participant", "program fee",
                  "self-funded", "self funded"]

PAYFLY_GOOD_HOURLY = 250   # at or under this per hour, flag as worth a look
PAYFLY_GOOD_TOTAL = 15000  # or a total package at or under this

WANTED_TITLES = [
    r"second[- ]in[- ]command", r"\bSIC\b", r"support crew member", r"seat support",
    r"simulator support pilot", r"pilot monitoring", r"simulator pilot",
    r"first officer", r"\bF/?O\b", r"co-?pilot", r"relief pilot", r"line pilot",
    r"pilot (?:position|opening|job|wanted|needed)", r"hiring pilots?",
    r"(?:^|\W)(?:staff|line|company|contract|seasonal|charter) pilot",
    r"flight instructor", r"\bCFI\b", r"\bMEI\b", r"\bCFII\b",
    r"charter pilot", r"cargo pilot", r"survey pilot", r"aerial survey",
    r"pipeline patrol", r"power ?line patrol", r"utility patrol", r"patrol pilot",
    r"banner tow", r"aerial advertis", r"skydive", r"skydiving", r"jump pilot",
    r"parachute", r"glider tow", r"tow pilot", r"traffic watch", r"fish spot",
    r"air ambulance", r"medevac", r"tour pilot", r"scenic (?:tour )?pilot",
    r"air tour pilot",
    r"ferry pilot", r"contract pilot", r"aerial (?:imag|map|photo|data)",
    r"survey pilot", r"mapping pilot", r"sensor operator", r"\bISR\b",
    r"cargo feeder", r"feeder pilot", r"check ?hauling", r"night freight",
    r"crop dust", r"\bag pilot\b", r"aerial applicat",
]

# Geography, best first. He does not like cold weather, so California leads,
# then Hawaii, then Alaska. Everything else in the five-state region ranks last.
TIER1 = ["bay area", "san francisco", "sfo", "oakland", "hayward", "concord",
         "san jose", "palo alto", "san carlos", "livermore", "napa", "santa rosa",
         "sacramento", "stockton", "modesto", "oakdale", "monterey", "salinas",
         "chico", "redding", "ukiah", "petaluma", "novato", "watsonville",
         "vacaville", "fairfield", "marin", "sonoma", "half moon bay",
         "northern california"]
TIER2 = ["ca", "california", "los angeles", "long beach", "van nuys", "san diego",
         "fresno", "bakersfield", "santa barbara", "burbank", "ontario", "riverside",
         "torrance", "hawthorne", "chino", "oxnard", "san marcos", "camarillo"]
TIER3 = ["hi", "hawaii", "honolulu", "kailua", "kona", "hilo", "lihue", "kahului",
         "maui", "oahu", "kauai", "big island"]
TIER4 = ["ak", "alaska", "anchorage", "fairbanks", "juneau", "bethel", "kenai",
         "kodiak", "nome", "sitka", "ketchikan", "dillingham", "valdez", "homer",
         "talkeetna", "palmer", "wasilla", "lake hood", "merrill field"]
TIER5 = ["az", "arizona", "marana", "pinal", "pinal airpark", "tucson", "phoenix",
         "mesa", "chandler", "scottsdale", "glendale", "peoria", "gilbert",
         "tempe", "prescott", "goodyear", "casa grande", "deer valley",
         "falcon field", "coolidge", "buckeye", "yuma", "flagstaff", "sedona",
         "kingman", "bullhead", "lake havasu", "sierra vista", "safford",
         "show low", "page az", "winslow", "payson"]
TIER6 = ["or", "oregon", "wa", "washington", "nv", "nevada", "id", "idaho",
         "seattle", "portland", "spokane", "boise", "reno", "las vegas",
         "bend", "redmond", "hillsboro", "renton", "everett", "bellingham",
         "kenmore", "medford", "eugene", "salem", "yakima", "wenatchee"]

TIER_LABEL = {1: "Northern California", 2: "Rest of California", 3: "Hawaii",
              4: "Alaska", 5: "Arizona", 6: "Pacific Northwest and inland West",
              7: "Outside his geography"}

# Anything landing in tier 4 is dropped. His geography is Alaska, Washington,
# Oregon, California and Hawaii, plus the immediately adjacent inland West.
DROP_OUTSIDE_GEOGRAPHY = True

# ---- freshness ----
# A posting that has been up a week is a different thing from one posted Tuesday.
FRESH_BANDS = [(2, "fresh", 0), (7, "this week", 1), (30, "aging", 2)]


def freshness_band(dt):
    """Return (label, rank). Undated postings sit between 'this week' and 'aging'."""
    if dt is None:
        return "undated", 2
    days = (datetime.now(timezone.utc) - dt).days
    for limit, label, rank in FRESH_BANDS:
        if days <= limit:
            return label, rank
    return "stale", 3

# --- fit scoring ---
# What he actually brings. Each group scores when the posting asks for it.
# Reasons are shown on the page so a highlight explains itself.

PROFILE = [
    ("Rotational schedule, so he keeps a home base", 4,
     ["week on week off", "week on, week off", "7 on 7 off", "7/7", "14/14", "15/15",
      "8 on 6 off", "two weeks on", "2 weeks on", "rotational", "rotation schedule",
      "on a rotation", "commutable", "travel to base provided",
      "airline ticket provided", "seasonal rotation"]),
    ("Housing provided", 4,
     ["housing provided", "crew housing", "free housing", "housing included",
      "housing available", "company housing", "lodging provided", "room provided",
      "accommodation provided", "furnished apartment", "bunkhouse", "per diem and housing"]),
    ("Commercial ASES and float time", 4,
     ["seaplane", "float", "amphib", "ases", "on floats"]),
    ("Northern California, where he wants to be", 4,
     ["bay area", "san francisco", "oakland", "hayward", "concord", "san jose",
      "livermore", "napa", "santa rosa", "sacramento", "stockton", "oakdale",
      "monterey", "chico", "redding", "petaluma", "novato", "northern california"]),
    ("California, warm and close to home", 3,
     ["california", "los angeles", "van nuys", "long beach", "san diego", "fresno",
      "bakersfield", "torrance", "hawthorne", "burbank", "chino", "camarillo"]),
    ("Hawaii, warm weather and home", 5,
     ["hawaii", "honolulu", "kona", "hilo", "lihue", "kahului", "maui", "oahu"]),
    ("Arizona, where the fast turbine hour-building seats are", 3,
     ["arizona", "marana", "pinal", "tucson", "phoenix", "mesa", "chandler",
      "prescott", "goodyear", "casa grande", "coolidge", "deer valley"]),
    ("Alaska, where the hours pile up fastest but the weather does not suit him", 1,
     ["alaska", "anchorage", "fairbanks", "juneau", "ketchikan", "sitka", "bethel",
      "kodiak", "nome", "dillingham", "kenai", "talkeetna", "palmer"]),
    ("Active CFI, CFII and MEI in progress", 3,
     ["cfi", "flight instructor", "mei", "cfii", "instructor certificate"]),
    ("Tailwheel time", 2,
     ["tailwheel", "conventional gear", "taildragger"]),
    ("Turbine exposure: DHC-3 and C-208", 2,
     ["caravan", "c208", "c-208", "208b", "grand caravan", "dhc", "otter", "beaver",
      "turbine", "pt6", "king air", "be20", "1900"]),
    ("Two-crew seat, logs time", 2,
     ["second in command", "first officer", "sic", "co-pilot", "two-pilot", "two crew"]),
    ("Trains low-time pilots", 2,
     ["will train", "no experience necessary", "entry level", "low time",
      "training provided", "type rating provided", "paid training"]),
    ("High performance and complex time", 1,
     ["high performance", "complex", "constant speed", "retractable"]),
    ("Alaska Air Group connection", 3,
     ["alaska airlines", "horizon air", "alaska air group"]),
    ("Instrument current, 127 hours", 1,
     ["instrument rating", "ifr", "instrument current"]),
]

TOP_MATCH_SCORE = 8   # at or above this, pin it to the highlight card
MAX_ROWS = 25         # the table shows this many; the rest are counted, not listed


def score_fit(text, job, d):
    """Return (score, reasons). Qualifying on hours is weighted heavily."""
    low = (text + " " + job.get("title", "") + " " + job.get("location", "")).lower()
    score, reasons = 0, []
    for label, weight, words in PROFILE:
        if any(w in low for w in words):
            score += weight
            reasons.append(label)
    # A stated minimum he clears is the strongest single signal on the page. An
    # unstated minimum is an unknown, not a match, and ranks below it.
    if d.get("band_rank") == 0:
        score += 6
        reasons.insert(0, f"states {d['min_hours']} hours minimum, he clears it")
    elif d.get("band_rank") == 1:
        score += 2
        reasons.insert(0, f"states {d['min_hours']} hours, {d['min_hours'] - CURRENT_HOURS} short")
    elif d.get("band_rank") == 3:
        score -= 5
    score += {1: 4, 2: 3, 3: 3, 4: 1, 5: 2, 6: 1}.get(d.get("tier"), 0)

    fresh_points = {0: 3, 1: 2, 2: 0, 3: -3}.get(d.get("fresh_rank"), 0)
    if fresh_points:
        score += fresh_points
        if fresh_points > 0:
            reasons.append(f"posted {d.get('posted', 'recently')}")
    return score, reasons


# ---------------------------------------------------------------- sources

def _rows_from_links(html, name, base=""):
    """Pass one: anchors whose text looks like a job title.

    Works on operators with a real listings page.
    """
    out, soup = [], BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        if not title or len(title) > 140:
            continue
        if not any(re.search(p, title, re.I) for p in WANTED_TITLES):
            continue
        href = a["href"]
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        if not href.startswith("http"):
            href = base.rstrip("/") + "/" + href.lstrip("/")
        row = a.find_parent(["tr", "li", "div", "article"])
        loc = ""
        if row:
            m = re.search(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*(AK|HI|CA|OR|WA|NV|AZ|ID|[A-Z]{2})\b",
                          row.get_text(" ", strip=True))
            if m:
                loc = m.group(0)
        out.append({"source": name, "title": title, "url": href, "location": loc})
    return out


def _rows_from_text(html, name, page_url):
    """Pass two: the small-operator case.

    Plenty of Part 135, 141 and 91 shops never build a listings page. The careers
    page is a paragraph: "We're hiring Caravan first officers, email the chief
    pilot." There is no anchor to find, so scan the visible text for a job title
    and keep the surrounding sentence. The link is the page itself.
    """
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "nav", "footer", "header"]):
        junk.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    # Must read like employment, not enrollment. "Apply now" alone is what every
    # flight academy puts on its admissions page, so it is deliberately not here.
    # Explicit hiring language only. Pay words like "hourly rate" and "per hour"
    # are deliberately absent: every school's aircraft rental page has them.
    HIRE = (r"now hiring|we are hiring|we're hiring|currently hiring|"
            r"join our team|join the team|now accepting applications|"
            r"accepting (?:pilot )?(?:resumes|applications)|send (?:your )?resume|"
            r"email (?:your )?resume|open position|position available|job opening|"
            r"employment opportunit|career opportunit|we have an opening|"
            r"apply (?:for|to) (?:this|the|our) (?:position|role|opening|job)|"
            r"(?:full|part)[- ]time (?:position|employment)")
    hiring = re.search(HIRE, text, re.I)
    out, seen_titles, claimed = [], set(), []
    for pat in WANTED_TITLES:
        for m in re.finditer(pat, text, re.I):
            # keep a readable window around the hit for context and location
            lo, hi = max(0, m.start() - 400), min(len(text), m.end() + 400)
            snippet = text[lo:hi].strip()
            # The hiring phrase has to be near this title, not merely somewhere
            # on the page, or a footer careers link turns marketing into a job.
            if not re.search(HIRE, snippet, re.I):
                continue
            title = m.group(0).strip()
            key = title.lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            loc = ""
            lm = re.search(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*"
                           r"(AK|HI|CA|OR|WA|NV|AZ|ID)\b", snippet)
            if lm:
                loc = lm.group(0)
            # Collapse overlapping matches: "hiring flight instructors, CFI/CFII
            # required" is one job, not three. Keep the first hit per region.
            if any(abs(m.start() - s) < 300 for s in claimed):
                continue
            claimed.append(m.start())
            out.append({"source": name, "title": title.title(), "url": page_url,
                        "location": loc, "context": snippet,
                        "page_text": text[:20000],
                        "text_only": True, "hiring_signal": True})
    # Without a hiring phrase anywhere on the page, a bare mention of "pilot" is
    # almost certainly navigation or marketing copy, not an opening.
    return [r for r in out if r["hiring_signal"]][:3]


def scrape(name, url, base=""):
    """Try the links pass, fall back to the text pass."""
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        rows = _rows_from_links(r.text, name, base or url)
        if not rows:
            rows = _rows_from_text(r.text, name, url)
        return rows
    except Exception as e:
        return [{"source": name, "error": str(e), "url": url}]


def fetch_cae():
    """CAE runs Workday, which exposes a JSON search endpoint."""
    out = []
    for term in ["support crew member", "second in command", "first officer"]:
        try:
            r = requests.post(
                "https://cae.wd3.myworkdayjobs.com/wday/cxs/cae/career/jobs",
                json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term},
                headers={**UA, "Content-Type": "application/json"}, timeout=TIMEOUT)
            for p in r.json().get("jobPostings", []):
                if any(re.search(pat, p.get("title", ""), re.I) for pat in WANTED_TITLES):
                    out.append({"source": "CAE", "title": p["title"],
                                "location": p.get("locationsText", ""),
                                "url": "https://cae.wd3.myworkdayjobs.com/en-US/career"
                                       + p.get("externalPath", "")})
        except Exception as e:
            out.append({"source": "CAE", "error": f"{term}: {e}"})
    return out


# Aggregator boards are the spine. Direct careers pages catch what boards miss.
# Verify each URL once after the first run; operators change ATS vendors often.
# URLs marked (unverified) are best guesses that need one check after the first run.
# Anything that fails shows up in the "did not respond" list on the page.
# Operators that post to their own pages. Grouped by operating rule and region.
# Two-line format so adding one is a copy, paste and edit.
#
# STATUS
#   verified   fetched and confirmed it returns readable listings
#   broken     fetched and confirmed it returns nothing useful
#   unverified never fetched; the first run's "did not respond" list will tell you
#
# Small operators often run their careers page through an ATS embed (Workable,
# BambooHR, Paylocity, JazzHR) or an Indeed widget. Those render client side and
# will come back empty the same way JSfirm does. When that happens, the fix is to
# point at the ATS URL directly rather than the operator's own page.

def op(name, url, base=None):
    return lambda: scrape(name, url, base or "/".join(url.split("/")[:3]))


WATCHLIST = [
    # ============ the destination ============
    op("Horizon Air", "https://careers.alaskaair.com/company/horizon-air/job-category/pilots/jobs/"),   # verified
    op("Alaska Airlines", "https://careers.alaskaair.com/company/alaska-airlines/jobs/"),               # verified

    # ============ aggregator boards ============
    op("JSfirm", "https://www.jsfirm.com/jobs/pilot"),                       # broken, JS rendered
    op("Climbto350", "https://www.climbto350.com/pilot_jobs.asp"),           # unverified
    op("Pilot Career Center", "https://pilotcareercenter.com/Pilot-Jobs"),   # unverified
    op("FindAPilot", "https://www.findapilot.com/jobs"),                     # unverified
    op("Aviation Job Search", "https://www.aviationjobsearch.com/jobs/pilot"),  # unverified
    op("AVjobs", "https://www.avjobs.com/jobs/"),                            # unverified
    op("NAFI board", "https://nafimentor.org/job-board/"),                   # unverified
    op("AOPA board", "https://www.aopa.org/about/hr/aviation-job-postings"), # unverified
    op("EAA board", "https://www.eaa.org/eaa/eaa-membership/aviation-job-search"),  # unverified
    op("Alaska Airmen", "https://www.alaskaairmen.org/building/career/job-work-development"),  # unverified

    # ============ low-time specific, the layer I was missing ============
    # These track minimums in the 250 to 500 range, which is the band he cleared
    # 150 hours ago. This is where the volume actually is.
    op("Road to 1500 database", "https://www.roadto1500.com/p/community-database"),
    op("Low Time Pilot", "https://www.lowtimepilot.com/"),
    op("Low Time Pilot, pipeline", "https://www.lowtimepilot.com/pipeline-patrol"),
    op("American Sky Aviation list", "https://americanskyaviation.com/low-hours-commercial-pilot-jobs/"),

    # ============ Part 142 simulator training centers ============
    op("FlightSafety", "https://careers.flightsafety.com/go/Instructor-Jobs/3675500/"),  # verified
    fetch_cae,
    op("SIMCOM", "https://simcom.com/careers/"),                             # unverified

    # ============ Part 135, Northern California and the West ============
    op("West Air", "https://westairinc.com/careers/"),                       # unverified, Fresno CA
    op("Ameriflight", "https://ameriflight.com/careers/"),                   # unverified
    op("Bridgeford Flying Service", "https://bridgefordflying.com/"),        # unverified, Napa CA
    op("Surf Air Mobility", "https://www.surfair.com/careers"),              # unverified
    op("Boutique Air", "https://www.boutiqueair.com/careers"),               # unverified
    op("Contour Airlines", "https://www.contourairlines.com/careers"),       # unverified
    op("Kenmore Air", "https://www.kenmoreair.com/careers/"),                # unverified, WA floats
    op("SkyShare", "https://skyshare.com/careers/"),                         # unverified, UT

    # ============ Part 135, Alaska ============
    op("Ravn Alaska", "https://www.flyravn.com/careers/"),                   # unverified
    op("Everts Air", "https://www.evertsair.com/careers/"),                  # unverified
    op("Grant Aviation", "https://www.flygrant.com/careers"),                # unverified
    op("Bering Air", "https://beringair.com/careers/"),                      # unverified
    op("Alaska Seaplanes", "https://www.flyalaskaseaplanes.com/careers"),    # unverified
    op("Northern Air Cargo", "https://www.nac.aero/careers/"),               # unverified
    op("Lynden Air Cargo", "https://www.lynden.com/lac/careers.html"),       # unverified
    op("Wright Air Service", "https://www.wrightairservice.com/careers"),    # unverified
    op("Warbelow's Air Ventures", "https://www.warbelows.com/careers"),      # unverified
    op("Yute Commuter Service", "https://www.flyyute.com/careers"),          # unverified
    op("Alaska Central Express", "https://www.aceaircargo.com/careers"),     # unverified
    op("Taquan Air", "https://www.taquanair.com/employment"),                # unverified, floats
    op("Ward Air", "https://www.wardair.com/"),                              # unverified, Juneau floats
    op("Harris Air", "https://www.harrisair.com/"),                          # unverified, floats

    # ============ Part 135, Hawaii ============
    op("Kamaka Air", "https://www.kamakaair.com/careers"),                   # unverified
    # Mokulele is a Southern Airways Corporation brand. Their own careers page is a
    # generic corporate one and does not list pilot openings; the live listings sit
    # on the aggregators. Verified by fetch.
    op("Southern / Mokulele openings",
       "https://www.glassdoor.com/Jobs/Southern-Airways-Express-Jobs-E1907708.htm"),
    op("Southern / Mokulele on Indeed",
       "https://www.indeed.com/cmp/Southern-Airways-Express"),
    op("FlightHired", "https://flighthired.com/job/"),                        # verified
    op("BizJetJobs low time", "https://bizjetjobs.com/low-time-pilots"),      # verified
    op("Road to 1500, Southern deep dive",
       "https://www.roadto1500.com/p/southern-airways-express-deep-dive"),

    # ============ Flight schools he would actually move for ============
    # He already instructs in the Bay Area, so NorCal schools are
    # noise. Only two places would be a step, not a sideways move.
    #
    # Hawaii needs the CFII. Anchorage does not, per Birch Creek's own posting.
    op("Birch Creek Aviation", "https://www.birchcreekaviation.com/careers"),   # Merrill Field, Anchorage
    op("Birch Creek Aviation (site)", "https://www.birchcreekaviation.com/"),   # text-pass fallback
    op("Blue River Aviation", "https://www.blueriveraviation.com/careers/"),    # Palmer, near Anchorage
    op("George's Aviation", "https://www.georgesaviation.com/careers"),         # unverified, Honolulu, needs CFII
    op("Fly Around Alaska", "https://flyaroundalaska.com/flight-school/hiring-flight-instructor/"),

    # ============ Part 91 utility and time building ============
    op("Lodi Parachute Center", "https://www.parachutecenter.com/"),         # unverified, jump pilots
    op("Bay Area Skydiving", "https://www.bayareaskydiving.com/"),           # unverified
]


# ---------------------------------------------------------------- verify

DEAD = ["position has been filled", "no longer accepting", "no longer available",
        "posting has expired", "job not found", "this position is closed"]


def verify_live(job):
    """Open the detail page. Filled and expired postings get dropped, not listed.

    This is not optional: FlightSafety leaves filled roles in its search listing
    while the detail page reads 'this position has been filled.'
    """
    url = job.get("url")
    if not url:
        return "no link", ""
    if job.get("text_only"):
        # Hand back the whole page, not just the snippet, so the training-program
        # filter can see language that sits elsewhere on the page.
        return "live", job.get("page_text") or job.get("context", "")
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
    except Exception as e:
        return "unreachable", str(e)
    if r.status_code == 404:
        return "gone", ""
    body = r.text.lower()
    if any(p in body for p in DEAD):
        return "filled", ""
    job["raw"] = r.text[:250000]
    return "live", BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)[:8000]



def extract_posted(html_or_text):
    """Find when a posting went up. Tries the reliable sources first.

    1. schema.org JobPosting datePosted, which most ATS platforms emit
    2. a <time datetime="..."> element
    3. prose: "Date: Jul 1, 2026", "Posted on ...", "Posted 3 days ago"

    Returns (datetime or None, display string).
    """
    t = html_or_text

    # 1. JSON-LD
    m = re.search(r'"datePosted"\s*:\s*"([0-9]{4}-[0-9]{2}-[0-9]{2})', t)
    if not m:
        # 2. <time datetime="...">
        m = re.search(r'<time[^>]+datetime="([0-9]{4}-[0-9]{2}-[0-9]{2})', t, re.I)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt, _posted_label(dt)
        except ValueError:
            pass

    # 3. prose dates
    m = re.search(r"(?:date|posted(?:\s+on)?)\s*[:\-]?\s*"
                  r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})",
                  t, re.I)
    if m:
        for fmt in ("%b %d, %Y", "%b %d %Y", "%B %d, %Y", "%B %d %Y"):
            try:
                dt = datetime.strptime(m.group(1).replace(".", ""), fmt).replace(tzinfo=timezone.utc)
                return dt, _posted_label(dt)
            except ValueError:
                continue

    # 4. relative, spelled out
    m = re.search(r"(?:posted\s+)?(\d{1,3})\+?\s*(day|week|month|year|yr)s?\s+ago", t, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = n * {"day": 1, "week": 7, "month": 30, "year": 365, "yr": 365}[unit]
        dt = datetime.now(timezone.utc) - timedelta(days=days)
        return dt, _posted_label(dt)

    # 5. compact social stamps: "4y", "3yr", "8mo", "2w", "5d", often "• 4y •"
    m = re.search(r"(?:^|[\s•·|])(\d{1,3})\s?(y|yr|yrs|mo|mos|w|wk|d)(?:[\s•·|]|$)", t, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = n * {"y": 365, "yr": 365, "yrs": 365, "mo": 30, "mos": 30,
                    "w": 7, "wk": 7, "d": 1}[unit]
        dt = datetime.now(timezone.utc) - timedelta(days=days)
        return dt, _posted_label(dt)

    # 6. bare "May 2022"
    m = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
                  r"(19\d{2}|20\d{2})\b", t, re.I)
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)[:3]} {m.group(2)}", "%b %Y").replace(tzinfo=timezone.utc)
            return dt, _posted_label(dt)
        except ValueError:
            pass

    return None, "not stated"


def _posted_label(dt):
    days = (datetime.now(timezone.utc) - dt).days
    local = dt.astimezone(ZoneInfo("America/Los_Angeles"))
    stamp = local.strftime("%b %-d")
    if local.year != datetime.now(ZoneInfo("America/Los_Angeles")).year:
        stamp = local.strftime("%b %-d, %Y")
    if days <= 0:
        return f"{stamp} · today"
    if days == 1:
        return f"{stamp} · yesterday"
    if days < 30:
        return f"{stamp} · {days}d ago"
    if days < 365:
        return f"{stamp} · {days // 30}mo ago"
    return stamp


def assess(text, job):
    """Parse, then decide keep or drop. Returns (details, reject_reason)."""
    low = (text + " " + job.get("location", "")).lower()
    d = {}

    # Raw HTML first: JSON-LD datePosted and <time> tags live in markup that text
    # extraction throws away, and those are the reliable dates.
    d["posted_dt"], d["posted"] = extract_posted(job.get("raw") or text)

    # Real postings phrase this a dozen ways. Catch the common ones, then take
    # the smallest number found, since that is the entry requirement.
    # A preferred figure is an aspiration, not a gate. Pull those out before
    # looking for the real minimum, and remember them separately.
    soft = re.findall(r"(\d{1,2},\d{3}|\d{3,4})\s*\+?\s*(?:total\s*)?(?:flight\s*)?"
                      r"(?:hours|hrs|tt)[^.;]{0,30}?"
                      r"(?:preferred|desired|desirable|a plus|nice to have|ideally)", low)
    d["preferred_hours"] = min(int(x.replace(",", "")) for x in soft) if soft else None
    low = re.sub(r"(\d{1,2},\d{3}|\d{3,4})\s*\+?\s*(?:total\s*)?(?:flight\s*)?"
                 r"(?:hours|hrs|tt)[^.;]{0,30}?"
                 r"(?:preferred|desired|desirable|a plus|nice to have|ideally)", " ", low)

    # Strip qualified hour figures first. "4,000 TT, 600 hours time in type" must
    # not read as a 600 hour job. Same for PIC, multi, turbine and night sub-minima.
    # Allow "time", "=", "of", "is" and similar between the qualifier and the number:
    # "Total turbine time = 500 hours" must not read as a 500 hour entry requirement.
    low = re.sub(r"(?:time in type|in type|\bpic\b|multi[- ]engine|\bme\b|turbine|"
                 r"night|cross[- ]country|\bxc\b|instrument|tail ?wheel|float|"
                 r"seaplane|make and model|in type)"
                 r"(?:\s+time)?\s*(?:=|:|-|of|is|at)?\s*(?:min\.?|minimum\s*(?:of)?)?\s*"
                 r"\d{1,2},?\d{0,3}\s*(?:\+)?\s*(?:hours|hrs)?", " ", low)

    # Real postings phrase this a dozen ways, and half of them use a comma.
    N = r"(\d{1,2},\d{3}|\d{3,4})"
    pats = [
        rf"(?:minimum|min\.?|at least|apply at|requires?|required)[^.]{{0,45}}?{N}\s*"
        r"(?:total\s*)?(?:flight\s*|flying\s*)?(?:hours|hrs|tt)",
        rf"{N}\s*\+?\s*(?:total\s*)?(?:flight\s*|flying\s*)?(?:hours|hrs)"
        r"(?:\s*total\s*(?:flight\s*)?time)?\s*(?:minimum|required|or more)?",
        rf"{N}\s*\+?\s*(?:tt|total time)\b",
        rf"total\s*(?:flight\s*)?time\s*[:\-]?\s*(?:min\.?\s*)?{N}",
    ]
    hits = [h.replace(",", "") for p in pats for h in re.findall(p, low)]
    nums = [int(h) for h in hits if h and int(h) >= 100]
    d["min_hours"] = min(nums) if nums else None

    d["certs"] = ", ".join(c for c in ["ATP", "Commercial", "Multi-Engine", "Instrument", "CFI"]
                           if c.lower() in low) or "not stated"

    for reason, words in EXCLUDE.items():
        if any(w in low for w in words):
            return d, reason

    title_l = job.get("title", "").lower()
    hit = next((r for r in NOT_A_PILOT_ROLE if r in title_l), None)
    if hit:
        return d, f"not a pilot role ({hit.strip()})"

    if re.search(r"\b(program|academy|course|curriculum|pathway|cadet|"
                 r"training|school|admissions|scholarship|internship|"
                 r"apprentice|bootcamp|camp)\b", title_l):
        return d, "training product, not a job (title)"

    hits = [w for w in TRAINING_PROGRAM if w in low]
    if hits:
        return d, f"training program, not a job ({hits[0]})"

    rot = re.search(
        r"(week on[,\s]+week off|\b\d{1,2}\s*(?:on|/)\s*\d{1,2}\s*(?:off)?\b|"
        r"rotational|rotation schedule|two weeks on|2 weeks on|commutable|crew housing)", low)
    d["rotation"] = rot.group(0).strip() if rot else None

    d["housing"] = bool(re.search(
        r"(housing (?:provided|included|available)|crew housing|free housing|"
        r"company housing|lodging provided|furnished apartment|bunkhouse)", low))

    # pay-to-fly detection and pricing
    d["payfly"] = any(m in low for m in PAYFLY_MARKERS) and "$" in text
    if d["payfly"]:
        hourly = [int(x.replace(",", "")) for x in
                  re.findall(r"\$\s?([\d,]{3,7})\s*(?:/|per\s*)(?:hr|hour|flight hour)", low)]
        totals = [int(x.replace(",", "")) for x in
                  re.findall(r"\$\s?([\d,]{4,7})", low)]
        d["price_hourly"] = min(hourly) if hourly else None
        d["price_total"] = min(totals) if totals else None
        d["price_sort"] = d["price_hourly"] or d["price_total"] or 10**9
        d["good_deal"] = bool(
            (d["price_hourly"] and d["price_hourly"] <= PAYFLY_GOOD_HOURLY) or
            (not d["price_hourly"] and d["price_total"] and d["price_total"] <= PAYFLY_GOOD_TOTAL))

    h = d["min_hours"]
    if h is None and d.get("preferred_hours"):
        d["band"] = f"{d['preferred_hours']} preferred, none required"
        d["band_rank"] = 1
    elif h is None:
        d["band"], d["band_rank"] = "unstated", 2
    elif h <= BAND_NOW:
        d["band"], d["band_rank"] = "meets it", 0
    elif h <= BAND_SOON:
        d["band"], d["band_rank"] = "short by %d" % (h - CURRENT_HOURS), 1
    else:
        d["band"], d["band_rank"] = "future", 3
        if h > MAX_MINIMUM:
            return d, f"needs {h} hours, he is at Horizon before that"

    loc = (job.get("location", "") + " " + text[:500]).lower()
    d["tier"] = 7
    for n, group in ((1, TIER1), (2, TIER2), (3, TIER3), (4, TIER4),
                     (5, TIER5), (6, TIER6)):
        if any(re.search(rf"\b{re.escape(t)}\b", loc) for t in group):
            d["tier"] = n
            break

    if DROP_OUTSIDE_GEOGRAPHY and d["tier"] == 7:
        return d, "outside his geography"

    # He already instructs in the Bay Area, so a California CFI job is sideways.
    if (d["tier"] not in INSTRUCTING_OK_IN_TIERS
            and any(re.search(p, job.get("title", ""), re.I) for p in INSTRUCTOR_TITLES)):
        return d, "instructor role outside Hawaii, already instructing"

    # A URL that still resolves is not a job that is still open. Old social posts
    # and archived listings stay reachable forever, so age is checked explicitly.
    pdt = d.get("posted_dt")
    if pdt is not None:
        age = (datetime.now(timezone.utc) - pdt).days
        if age > MAX_POSTING_AGE_DAYS:
            return d, f"posted {d.get('posted', 'long ago')}, {age} days old"
    d["fresh"], d["fresh_rank"] = freshness_band(pdt)
    d["reach"] = bool(d["min_hours"] and d["min_hours"] > BAND_NOW)

    ok, why = looks_like_a_posting(job, text)
    if not ok:
        return d, why

    # Text-mode finds with nothing concrete attached are almost always marketing
    # copy that happened to sit near a hiring phrase.
    if job.get("text_only") and not any([job.get("location"), d.get("min_hours"),
                                         d.get("posted_dt"), d.get("rotation"),
                                         d.get("housing")]):
        return d, "no location, hours or date given"

    d["kind"] = "sim" if re.search(r"simulator|second[- ]in[- ]command|support crew|seat support",
                                   job["title"], re.I) else "line"
    return d, None


# ---------------------------------------------------------------- tips

# One tip per run, rotating. Tactical and specific to the 650 -> 1400 gap.
# Edit freely; the digest just cycles through in order.
TIPS = [
    ("Not all hours weigh the same.",
     "Multi time is the scarcest thing on a low-time resume. The MEI turns every "
     "multi lesson into logged dual given, which is the cheapest multi time in "
     "existence. Having it in hand before the winter application window is worth "
     "more than having it at all."),
    ("Funding that has not landed is a plan, not a schedule.",
     "A rating you are waiting on money for slips when the money slips, and a "
     "rating that arrives after the hiring window costs a whole season. Worth "
     "pricing the self-funded version early, so the decision is already made if "
     "the funding does not come through."),
    ("The rate matters as much as the hours.",
     "A school CFI billing 45 hours a month at instructor wages and an "
     "independent CFI billing 45 hours at their own rate log identical time "
     "for very different money. Same runway, different funding."),
    ("The seaplane rating is a door, not a line item.",
     "Float operators are a small world that hires on conversation, not "
     "applications. A direct email to a chief pilot in Juneau or Ketchikan "
     "beats twenty online submissions."),
    ("Ask the airline before you ask the vendor.",
     "Airlines staff their own sims with seat fillers. Right type, right SOPs, "
     "inside the building. Worth a call to flight training before applying to "
     "a third-party training center."),
    ("Time the sim prep late.",
     "Flows and memory items learned a year before initial mostly evaporate. "
     "The last few hundred hours before a class date is when that work pays."),
    ("Every non-logging month is a delay.",
     "When the gate is an hour count, a job that pays well but logs nothing "
     "pushes the start date out. Worth it sometimes. Never worth it by accident."),
]


def tip_for_run():
    """One tip per calendar day."""
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    return TIPS[now.timetuple().tm_yday % len(TIPS)]



# ---------------------------------------------------------------- history

# Rotating facts. Verified as written; edit and extend freely.
HISTORY = [
    ("Bessie Coleman, 1921",
     "American flight schools refused to teach her, so she learned French, sailed "
     "to Paris, and earned her license from the Federation Aeronautique "
     "Internationale. First African American woman, and first Native American "
     "woman, to hold a pilot license anywhere in the world."),
    ("Eugene Bullard, 1917",
     "Flew combat for France in the Lafayette Flying Corps while the US Army Air "
     "Service rejected him. First African American military pilot. He never flew "
     "for his own country."),
    ("Willa Brown, 1938",
     "First African American woman to earn a US commercial pilot certificate, and "
     "the first to hold both pilot and mechanic certificates. Her Coffey School of "
     "Aeronautics in Chicago trained men who went on to Tuskegee."),
    ("Banning and Allen, 1932",
     "Crossed the country from Los Angeles to New York in a used Eagle Rock "
     "biplane, funding the trip by asking people along the way to sign the wing "
     "for a donation. First transcontinental flight by African American pilots."),
    ("Marlon Green, 1963",
     "Denied a job by Continental despite being qualified, he took it to the "
     "Supreme Court and won unanimously. That ruling is why airline cockpits "
     "opened. He did not fly a line trip until 1965."),
    ("David Harris, 1964",
     "Hired by American Airlines, becoming the first African American pilot at a "
     "major US passenger carrier. He kept it quiet for months, unsure how "
     "crews would react."),
    ("Three-axis control, 1903",
     "The Wrights' real invention was not the engine or the airfoil. It was "
     "coordinated roll, pitch and yaw together. Wing warping for roll is the "
     "direct ancestor of the aileron and of every turn coordination he teaches."),
    ("The DHC-2 and DHC-3, 1947 and 1951",
     "De Havilland Canada designed the Beaver and the Otter by asking bush pilots "
     "what they actually needed. The answer was full-span flaps, doors that fit a "
     "fuel drum, and short field performance over speed. Seventy years on, they "
     "still fly the Alaska routes they were built for."),
    ("The 1500 hour rule, 2010",
     "Written into the Airline Safety and FAA Extension Act after Colgan 3407. "
     "The restricted ATP pathways that let some pilots qualify at 1000, 1250 or "
     "1500 hours all descend from that single law."),
    ("The Cessna 208, 1982",
     "Built as a Beaver replacement for operators who wanted turbine reliability "
     "on unimproved strips. It is now the most common way a low-time pilot in "
     "Alaska or Hawaii first touches turbine equipment."),
]


def history_for_run():
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    return HISTORY[now.timetuple().tm_yday % len(HISTORY)]





# ---------------------------------------------------------------- ferry and owner work

# Owner-flown work rarely reaches a job board. It lives in classifieds and forums.
# Craigslist and Reddit both expose RSS with no API key, so these are cheap to poll.
#
# Honest caveat, surfaced on the page too: most ferry work is gated by the owner's
# insurance, which typically wants time in make and model he does not have yet.
# Short repositioning hops in light singles are the realistic slice.

FERRY_TERMS = ["ferry pilot", "ferry flight", "reposition", "repositioning",
               "pilot needed", "pilot wanted", "need a pilot", "looking for a pilot",
               "safety pilot", "aircraft delivery", "deliver my", "fly my plane",
               "owner needs pilot", "part 91 pilot"]

CRAIGSLIST_SITES = ["sfbay", "sacramento", "monterey", "chico", "redding",
                    "honolulu", "anchorage", "losangeles", "sandiego", "fresno",
                    "phoenix", "tucson"]

REDDIT_QUERIES = [
    "https://www.reddit.com/r/flying/search.rss?q=ferry+pilot&restrict_sr=1&sort=new&t=month",
    "https://www.reddit.com/r/flying/search.rss?q=%22pilot+needed%22&restrict_sr=1&sort=new&t=month",
    "https://www.reddit.com/r/pilots/search.rss?q=ferry&restrict_sr=1&sort=new&t=month",
    "https://www.reddit.com/r/Alaska/search.rss?q=pilot+needed&restrict_sr=1&sort=new&t=month",
    "https://www.reddit.com/r/Hawaii/search.rss?q=pilot&restrict_sr=1&sort=new&t=month",
]


def _rss_items(url, source, limit=8):
    out = []
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "xml")
        for item in (soup.find_all("item") or soup.find_all("entry"))[:limit]:
            title = (item.title.get_text(strip=True) if item.title else "")
            link = item.link.get_text(strip=True) if item.link and item.link.get_text(strip=True) \
                else (item.link.get("href") if item.link else "")
            if not title or not any(t in title.lower() for t in FERRY_TERMS):
                continue
            out.append({"source": source, "title": title[:120], "url": link,
                        "location": "", "ferry": True, "text_only": True,
                        "context": title})
    except Exception as e:
        out.append({"source": source, "error": str(e)})
    return out


def fetch_ferry():
    """Classifieds and forums, filtered to posts that read like owner-flown work."""
    out = []
    for site in CRAIGSLIST_SITES:
        for cat, q in (("jjj", "pilot"), ("gigs", "pilot")):
            out += _rss_items(
                f"https://{site}.craigslist.org/search/{cat}?query={q}&format=rss",
                f"Craigslist {site}")
    for url in REDDIT_QUERIES:
        out += _rss_items(url, "Reddit")
    return out


# ---------------------------------------------------------------- companies

# Names, not URLs. This is the easy list to maintain: you add a company by typing
# its name, and the research pass finds its careers page itself. No guessing at
# URL paths, and nothing breaks when a site is redesigned.
#
# Add names freely. The rotation keeps any one day's search cheap.

COMPANIES_PRIORITY = [
    # Checked every single run.
    "Horizon Air",
    "Birch Creek Aviation Anchorage",
    "George's Aviation Honolulu",
    "Alaska Seaplanes",
    "Kamaka Air",
    "Trail Ridge Air Anchorage",      # directory: commercial SES expected
    "Land and Sea Aviation Anchorage",
]

COMPANIES_ROTATION = [
    # Checked a slice at a time, cycling through over about a week.
    # Alaska
    "Ravn Alaska", "Everts Air Cargo", "Grant Aviation", "Bering Air",
    "Northern Air Cargo", "Lynden Air Cargo", "Ryan Air Alaska",
    "Wright Air Service", "Warbelow's Air Ventures", "Yute Commuter Service",
    "Alaska Central Express ACE Air Cargo", "Taquan Air", "Ward Air Juneau",
    "Harris Air Alaska", "Blue River Aviation Palmer", "Fly Around Alaska",
    "Alaska Air Transit", "Island Air Express Alaska",
    # Hawaii
    "Mokulele Airlines Hawaii first officer", "Southern Airways Corporation pilot",
    "Paragon Air Hawaii",
    # Arizona, where the low-time turbine seats are
    "Rampart Aviation Marana Arizona", "Skydive Arizona Eloy pilot",
    "Ping Aviation Arizona", "Westwind Air Service Phoenix",
    "Grand Canyon Airlines pilot", "Scenic Airlines Arizona",
    "Air Methods fixed wing Arizona", "Native Air Arizona fixed wing",
    "Silver State Helicopters fixed wing Arizona", "Chandler Air Service",
    "Skydive Phoenix pilot", "Marana Aerospace pilot",
    # California and the West
    "West Air Fresno", "Ameriflight", "Bridgeford Flying Service Napa",
    "Surf Air Mobility", "Boutique Air", "Contour Airlines", "Kenmore Air",
    "SkyShare Utah", "Advanced Air", "Redding Air Service",
    # Simulator and training
    "FlightSafety International", "CAE", "SIMCOM Aviation Training",
    "Avenger Flight Group", "Pan Am International Flight Academy",
    # Utility and time building, California first
    "Lodi Parachute Center", "Bay Area Skydiving", "Skydive Monterey Bay",
    "FlyBayArea San Carlos", "Fly Sky Ads Long Beach",
    # Aerial survey and pipeline patrol inside his geography
    "EagleView Bothell Washington", "Quantum Spatial NV5 aerial west coast",
    "aerial survey pilot California", "aerial survey pilot Alaska",
    "pipeline patrol pilot California", "pipeline patrol pilot Washington",
    "EagleView aerial",
        "Quantum Spatial NV5 aerial",
        # Pipeline and powerline patrol, minimums 250 to 500
        "Aviation Specialties pipeline patrol",
    # Banner tow and aerial advertising
        # Skydive operations
    "Skydive Perris", "Skydive Elsinore",
    "Skydive Snohomish", "Skydive Kapowsin",
    # Cargo feeders, minimums 300 to 900
            # Air tour
    "Blue Hawaiian fixed wing",
    "Kenai Fjords Tours flightseeing", "Rust\'s Flying Service",
    # Part 135 that takes lower time
    
    # ---- from the Pacific operator directory, July 2026 ----
    # Lake Hood float cluster, Anchorage. Busiest seaplane base in the world and
    # the single best concentration of work matching his ASES.
    "Regal Air Anchorage Lake Hood", "Rust's Flying Service Lake Hood",
    # Alaska glacier and mountain tour operators
    "K2 Aviation Talkeetna", "Talkeetna Air Taxi",
    "University of Alaska Anchorage aviation instructor",
    # Washington
    "San Juan Airlines Bellingham", "Galvin Flying Seattle",
    "Rainier Flight Service Renton", "Regal Air Everett",
    "Clay Lacy Aviation Seattle", "Airpac Airlines Seattle cargo",
    # Oregon
    "Leading Edge Flight Academy Bend", "Hillsboro Aero Academy",
    "Willamette Aviation Aurora", "Precision Aviation Training Newberg",
    "Leading Edge Aviation Redmond",
    # California, NorCal first
    "Sierra West Airlines Oakdale", "Solairus Aviation Petaluma",
    "Threshold Aviation Group Chino",             # Hawaii
    "Pacific Air Charters Honolulu", "Royal Pacific Air Honolulu",
    "Pacific Flight Academy Honolulu",
]

# Deliberately not tracked from that directory, all rotorcraft only:
# Temsco Helicopters, Mauna Loa Helicopters, Blue Hawaiian Helicopters,
# Safari Helicopters, Paradise Helicopters.

COMPANIES_PER_RUN = 16


def companies_for_run():
    reg = registry()
    extra = reg.get("companies_rotation_add", [])
    prio = reg.get("companies_priority", [])
    return _companies_for_run(prio, extra)


def _companies_for_run(extra_priority=(), extra_rotation=()):
    """Priority names every run, plus a rotating slice of the rest."""
    rotation = list(COMPANIES_ROTATION) + [c for c in extra_rotation
                                            if c not in COMPANIES_ROTATION]
    priority = list(COMPANIES_PRIORITY) + [c for c in extra_priority
                                           if c not in COMPANIES_PRIORITY]
    n = len(rotation)
    if not n:
        return priority
    day = datetime.now(timezone.utc).astimezone().timetuple().tm_yday
    start = (day * COMPANIES_PER_RUN) % n
    slice_ = [rotation[(start + i) % n] for i in range(COMPANIES_PER_RUN)]
    return priority + slice_




# ---------------------------------------------------------------- source registry

# Sources live in sources.json, not in this file. Add one by editing that file;
# no code change and no risk of breaking the build. The daily research pass also
# appends what it discovers to the "candidates" list there.
#
# source_health.json records what each source has actually produced, so a source
# that has never returned a single posting is visible rather than silently dead.

REGISTRY = Path(__file__).parent / "sources.json"
HEALTH = Path(__file__).parent / "source_health.json"


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        print(f"{path.name} unreadable, using defaults")
        return default


def registry():
    return _load_json(REGISTRY, {})


def health():
    return _load_json(HEALTH, {})


def registry_sources(poll=None):
    """Flatten the registry into (name, url) pairs, optionally by poll cadence."""
    reg, out = registry(), []
    for key in ("boards", "forums", "operators_hot", "candidates"):
        for s in reg.get(key, []):
            if not s.get("url"):
                continue
            cadence = s.get("poll", "hot" if key == "operators_hot" else "daily")
            if poll is None or cadence == poll:
                out.append((s["name"], s["url"]))
    return out


def record_health(h, name, url, found, errored):
    """One row per source: how often it has been checked, and what it produced."""
    row = h.setdefault(url, {"name": name, "checks": 0, "hits": 0,
                             "errors": 0, "last_hit": None})
    row["name"] = name
    row["checks"] += 1
    if errored:
        row["errors"] += 1
    if found:
        row["hits"] += found
        row["last_hit"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return h


def dead_sources(h, min_checks=20):
    """Sources checked plenty and never once produced a posting."""
    return sorted(
        [(r["name"], u, r["checks"]) for u, r in h.items()
         if r["checks"] >= min_checks and r["hits"] == 0],
        key=lambda x: -x[2])


# ---------------------------------------------------------------- detection speed

# The market produces two to five genuinely new postings a week. Latency, not
# volume, is the thing worth optimising. Three mechanisms do the work:
#
#   1. HOT sources are polled every run (about every 20 minutes). The full
#      watchlist sweeps once a day, because most of it almost never changes.
#   2. Page fingerprints. A careers page whose content hash is unchanged since
#      the last poll is skipped without parsing, so frequent polling stays cheap
#      and fast even across dozens of sources.
#   3. A new posting pushes to the phone immediately. A page you have to visit
#      cannot beat a market where good jobs close in days.

POLL_STATE = Path(__file__).parent / "page_hashes.json"

# Highest-yield, highest-churn sources. Keep this list short; it runs every poll.
HOT_SOURCES = [
    ("Horizon Air", "https://careers.alaskaair.com/company/horizon-air/job-category/pilots/jobs/"),
    ("Southern / Mokulele", "https://www.glassdoor.com/Jobs/Southern-Airways-Express-Jobs-E1907708.htm"),
    ("Birch Creek Aviation", "https://www.birchcreekaviation.com/"),
    ("Fly Around Alaska", "https://flyaroundalaska.com/flight-school/hiring-flight-instructor/"),
    ("Alaska Seaplanes", "https://www.flyalaskaseaplanes.com/"),
    ("Kamaka Air", "https://www.kamakaair.com/careers"),
    ("Ameriflight", "https://ameriflight.com/careers/"),
    ("Kenmore Air", "https://www.kenmoreair.com/careers/"),
    ("Grant Aviation", "https://www.flygrant.com/careers"),
    ("Ravn Alaska", "https://www.flyravn.com/careers/"),
    ("FlightHired", "https://flighthired.com/job/"),
    ("BizJetJobs low time", "https://bizjetjobs.com/low-time-pilots"),
    ("Road to 1500", "https://www.roadto1500.com/p/community-database"),
    ("Low Time Pilot", "https://www.lowtimepilot.com/"),
]


def hot_sources():
    """Registry first, falling back to the built-in list if the file is missing."""
    return registry_sources(poll="hot") or HOT_SOURCES


def _hashes():
    if not POLL_STATE.exists():
        return {}
    try:
        return json.loads(POLL_STATE.read_text())
    except Exception:
        print("page_hashes.json unreadable, starting fresh")
        return {}


def _save_hashes(h):
    POLL_STATE.write_text(json.dumps(h, indent=1, sort_keys=True))


def poll(sources, hashes, h=None):
    """Fetch each source; parse only the ones whose content actually changed.

    Records per-source health too, so a source checked a hundred times that has
    never produced a posting shows up as a problem rather than a silent zero.
    """
    import hashlib
    h = health() if h is None else h
    rows, checked, skipped = [], 0, 0
    for name, url in sources:
        got, errored = 0, False
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            checked += 1
            body = re.sub(rb"\s+", b" ", r.content)
            digest = hashlib.sha256(body).hexdigest()[:16]
            if hashes.get(url) == digest:
                skipped += 1
                record_health(h, name, url, 0, False)
                continue
            hashes[url] = digest
            found = _rows_from_links(r.text, name, "/".join(url.split("/")[:3]))
            found = found or _rows_from_text(r.text, name, url)
            got = len(found)
            rows += found
        except Exception as e:
            errored = True
            rows.append({"source": name, "error": str(e), "url": url})
        record_health(h, name, url, got, errored)
    return rows, checked, skipped


def notify(new_jobs):
    """Push new postings to the phone. Free, no account: ntfy.sh topic.

    Set NTFY_TOPIC to any hard-to-guess string, install the ntfy app, subscribe
    to that topic. Silently does nothing if the variable is unset.
    """
    topic = os.environ.get("NTFY_TOPIC")
    if not topic or not new_jobs:
        return
    for j in new_jobs[:5]:
        d = j.get("details", {})
        bits = [x for x in [j.get("location"), d.get("band"),
                            d.get("rotation"), "housing" if d.get("housing") else ""] if x]
        try:
            requests.post(
                f"https://ntfy.sh/{topic}",
                data=f"{j['title']}\n{j.get('source','')}\n{' · '.join(bits)}".encode(),
                headers={"Title": "New pilot posting",
                         "Priority": "high" if d.get("score", 0) >= TOP_MATCH_SCORE else "default",
                         "Tags": "airplane",
                         "Click": j.get("url", "")},
                timeout=15)
        except Exception:
            pass


# ---------------------------------------------------------------- research pass

# The scraper handles the known list. This handles everything the list doesn't
# know about: operators nobody told it about, careers pages that moved, seasonal
# hiring that never reaches a job board. One Claude call a day with web search on.
#
# Needs ANTHROPIC_API_KEY. Without it the section is skipped and the page still
# builds, so the scraper never depends on this working.

RESEARCH_MODEL = "claude-sonnet-5"

WHO_HE_IS = """
Commercial pilot, roughly 650 hours total, building toward 1,400 for a Horizon Air
class date. Holds Commercial ASEL and AMEL, CFI, Commercial ASES with about 20 float
hours, tailwheel, first class medical, 127 hours instrument, roughly 97 hours high
performance. CFII and MEI in progress. Small amount
of turbine exposure in the DHC-3 and C-208. Currently instructing in the Bay Area.

What he wants, in order: Northern California, then Alaska or Hawaii, then the rest of
the West. Rotational schedules (week on week off, 7/7, 14/14) rank very high because
they let him keep a Bay Area home while flying elsewhere. Fixed wing only, US only,
no multi-year commitment programs. Hour minimums at or under 750 are ideal, up to
1,250 is worth listing.
"""


def research_pass(unreachable):
    """Search the web by company name. This is the half that adapts.

    The scraper needs a working URL for every source. This needs only a name, so
    the list is easy to maintain and nothing breaks when a site is redesigned.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None

    names = companies_for_run()
    name_list = "\n".join(f"- {c}" for c in names)
    broken = "\n".join(
        f"- {u[0]} ({u[2]})" if isinstance(u, tuple) else f"- {u}"
        for u in unreachable[:10]) or "- none this run"

    prompt = f"""You are the research half of a daily pilot job search. A scraper already
checked a fixed list of careers page URLs. You do what it cannot: search the open web.

The pilot:
{WHO_HE_IS}

Search each of these companies by name for current pilot or flight instructor
openings. Their careers pages move, so find where each one posts today rather than
assuming a URL:
{name_list}

Scraped sources that returned nothing today, which may mean a moved page rather than
no jobs:
{broken}

Then do these, in priority order:

1. For each company above, report any current opening that fits his profile. Include
   the hour minimum and the schedule if the posting states them. Only report postings
   you actually found, with a URL that resolves to the posting or its careers page.
2. Search beyond the list for openings it would miss: Part 135 operators, Part 141 and
   61 schools hiring instructors, Part 91 utility work such as jump pilot, aerial
   survey, pipeline patrol, banner tow, and seasonal hiring in his geography.
3. For each unreachable source above, find its current careers URL.
4. Name operators worth adding to the permanent list, with a careers URL.
5. SOURCE DISCOVERY, the important one. Find job boards, aggregators, forums,
   association boards, regional listings and operator directories that a search
   like this should be watching and probably is not. Think beyond the obvious
   national boards: state aviation association boards, seasonal and bush flying
   listings, skydive and survey operator directories, university and college
   flight department boards, and regional classifieds. For each, give a URL that
   lists jobs rather than a homepage.

Return ONLY a JSON object, no preamble, no markdown fences:
{{"findings":[{{"title":"","employer":"","location":"","min_hours":"","schedule":"",
"posted":"the date the posting went up, or empty if the page does not say",
"url":"","why":"one sentence on why it fits him"}}],
"url_fixes":[{{"source":"","new_url":""}}],
"new_sources":[{{"name":"","url":"","note":""}}],
"discovered_sources":[{{"name":"","url":"","kind":"board|forum|directory|classified",
"why":"what this covers that the current list misses"}}],
"summary":"two or three sentences on what today's search actually turned up, including
which companies you checked and found nothing at"}}

Report nothing you did not find. Empty arrays are a correct answer. Never invent a
posting, an hour minimum, or a URL."""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": RESEARCH_MODEL, "max_tokens": 8000,
                  "messages": [{"role": "user", "content": prompt}],
                  "tools": [{"type": "web_search_20250305", "name": "web_search",
                             "max_uses": 20}]},
            timeout=300)
        blocks = r.json().get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        start, end = text.find("{"), text.rfind("}")
        out = json.loads(text[start:end + 1]) if start >= 0 else None
        if out is not None:
            out["checked"] = names
            _absorb_sources(out.get("discovered_sources", []))
        return out
    except Exception as e:
        return {"error": str(e), "findings": [], "url_fixes": [],
                "new_sources": [], "summary": "", "checked": names}



# ---------------------------------------------------------------- vetting

# A forum thread about jobs is not a job. Neither is a listicle, a guide, or an
# aggregator's index page. These run before anything is published.

ARTICLE_URL = [
    "/blog/", "/p/", "/article", "/news/", "/guide", "/threads/", "/thread/",
    "/community/", "/forum", "/forums/", "/showthread", "/topic/", "/t/",
    "/resources", "/advice", "/tips", "/how-to", "/wiki", "reddit.com/r/",
    "/posts/", "/story/", "medium.com", "substack.com",
]

ARTICLE_TITLE = [
    "where to find", "how to find", "how to get", "best sites", "best places",
    "top ", " guide", "guide to", "everything you need", "what you need to know",
    "opportunities under", "jobs for low", "low time pilot jobs", "list of",
    "ultimate", "beginner", "explained", "vs ", " tips", "faq", "q&a",
    "database", "directory", "roundup", "career paths", "salary",
]

# Pages whose job is to list other people's jobs. Their index is not a posting.
INDEX_HINTS = ["job board", "search jobs", "browse jobs", "all jobs",
               "latest jobs", "job listings", "find jobs"]

# A board's own category page is not a posting, and it usually says so plainly.
INDEX_TITLES = {
    "jobs", "pilot jobs", "aviation jobs", "flying jobs", "careers", "career",
    "career opportunities", "employment", "employment opportunities",
    "job openings", "open positions", "current openings", "openings",
    "find jobs", "search jobs", "job search", "vacancies", "pilot careers",
    "join our team", "work with us", "apply now", "positions",
}


def looks_like_a_posting(job, text=""):
    """Return (ok, reason). Conservative: unsure means no."""
    url = (job.get("url") or "").lower()
    title = (job.get("title") or "").strip()
    tl = title.lower()

    if not title or len(title) < 4:
        return False, "no title"

    if any(p in url for p in ARTICLE_URL):
        return False, "article or forum thread, not a posting"

    if tl.strip(" -|:") in INDEX_TITLES:
        return False, "index page, not a posting"

    if any(p in tl for p in ARTICLE_TITLE):
        return False, "reads like an article, not a posting"

    # Real postings carry an identifier. A bare category path is an index.
    path = url.split("?")[0].rstrip("/").split("/")[-1] if url else ""
    if path and not re.search(r"\d", path) and len(path.split("-")) < 3 \
            and any(url.rstrip("/").endswith(seg) for seg in
                    ("/jobs", "/jobs/pilot", "/careers", "/pilot-jobs",
                     "/employment", "/openings", "/job")):
        return False, "index page, not a posting"

    # A title that is a whole sentence is prose, not a job title.
    if len(title.split()) > 12 or title.rstrip().endswith((".", "?", "!")):
        return False, "prose, not a job title"

    # Text-mode finds on an index page are the source describing itself.
    if job.get("text_only") and any(h in (text[:4000] or "").lower() for h in INDEX_HINTS):
        return False, "index page describing itself"

    return True, ""


VET_MODEL = "claude-sonnet-5"


def vet_with_llm(candidates):
    """Ask Claude to confirm each candidate is a real, specific, open posting.

    Returns a dict of index -> (keep, reason, fields). Without an API key this
    is skipped and the heuristics above stand on their own.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not candidates:
        return {}

    listing = "\n".join(
        f'{i}. title="{c.get("title","")}" source="{c.get("source","")}" '
        f'url="{c.get("url","")}" context="{(c.get("context") or "")[:300]}"'
        for i, c in enumerate(candidates))

    prompt = f"""Each line below was scraped by a job search and may or may not be a real
job posting. Plenty will be forum threads, blog articles, job-board index pages, or
marketing copy that merely mentions hiring.

Keep an entry ONLY if it is a specific, currently open job opening at a named employer.
Drop it if it is any of: an article or guide about jobs, a forum discussion, a job
board's own index or category page, a training program being sold, a non-pilot role,
or something too vague to identify the employer and the role.

{listing}

Return ONLY a JSON array, no prose and no markdown fences:
[{{"i":0,"keep":true,"employer":"","role":"","reason":"short"}}]

Be strict. When you cannot tell, keep=false. It is much better to drop a real posting
than to publish a forum thread as if it were a job."""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": VET_MODEL, "max_tokens": 4000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120)
        blocks = r.json().get("content", [])
        txt = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
        s, e = txt.find("["), txt.rfind("]")
        rows = json.loads(txt[s:e + 1]) if s >= 0 else []
        return {int(x["i"]): x for x in rows if "i" in x}
    except Exception as e:
        print(f"vetting pass unavailable ({e}), heuristics only")
        return {}


# ---------------------------------------------------------------- output

def dedupe(rows):
    seen, out = set(), []
    for r in rows:
        k = r.get("url") or r.get("error")
        if k and k not in seen:
            seen.add(k); out.append(r)
    return out


def load_seen():
    """Corrupt state is recoverable; a crash is not. A merge conflict can leave
    markers in these files, so parse failures start fresh instead of aborting."""
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        print("seen.json unreadable, starting fresh")
        return set()


def save_seen(u):
    STATE_FILE.write_text(json.dumps(sorted(u), indent=1))


TH = ('padding:8px 12px;border-bottom:1px solid #1b1f24;font-size:11px;'
      'letter-spacing:.08em;text-transform:uppercase;color:#5b6570;')
TD = 'padding:12px;border-bottom:1px solid #dfe3e8;font-size:13px;'


def build_html(jobs, top, payfly, rejected, errors, run_time, held_back=None,
               total=None, research=None, ferry=None):
    changed_n = sum(1 for j in jobs if j.get("is_new"))
    changed_line = (f"{changed_n} new since the last check"
                    if changed_n else "Nothing new since the last check")
    tip_head, tip_body = tip_for_run()
    hist_head, hist_body = history_for_run()
    _y = re.search(r"\b(1[89]\d{2}|20\d{2})\b", hist_head)
    hist_year = _y.group(1) if _y else ""
    hist_title = re.sub(r",?\s*\b(1[89]\d{2}|20\d{2})\b.*$", "", hist_head).strip() or hist_head

    rows = ""
    for j in jobs:
        d = j["details"]
        tier = TIER_LABEL[d["tier"]]
        badge = ('<span style="background:#1f6feb;color:#fff;font-size:10px;padding:1px 5px;'
                 'border-radius:2px;">NEW</span>') if j["is_new"] else ""
        star = ('<span style="background:#b8860b;color:#fff;font-size:10px;padding:1px 5px;'
                'border-radius:2px;">TOP FIT</span>'
                if d.get("score", 0) >= TOP_MATCH_SCORE else "")
        house = ('<span style="background:#7a4f9e;color:#fff;font-size:10px;padding:1px 5px;'
                 'border-radius:2px;">HOUSING</span>' if d.get("housing") else "")
        isle = ('<span style="background:#0f8a8a;color:#fff;font-size:10px;padding:1px 5px;'
                'border-radius:2px;">HAWAII</span>' if d.get("tier") == 3 else "")
        kind = ('<span style="border:1px solid #5b6570;color:#5b6570;font-size:10px;'
                'padding:1px 5px;">SIM</span>') if d["kind"] == "sim" else ""
        rows += f"""
        <tr>
          <td style="{TD}font-size:14px;"><strong>{j['title']}</strong> {star} {isle} {house} {badge} {kind}
            <br><span style="color:#5b6570;font-size:12px;">{j['source']}</span></td>
          <td style="{TD}">{j.get('location') or 'not stated'}
            <br><span style="color:#5b6570;font-size:11px;">{tier}</span>
            {'<br><span style="background:#1a5f7a;color:#fff;font-size:10px;padding:1px 5px;border-radius:2px;">' + d['rotation'].upper() + '</span>' if d.get('rotation') else ''}</td>
          <td style="{TD}white-space:nowrap;">{d.get('posted','not stated')}
            <br><span style="font-size:11px;color:{'#1a7f4b' if d.get('fresh_rank',2)==0 else '#8a939c'};">{d.get('fresh','')}</span></td>
          <td style="{TD}">{d['min_hours'] or 'n/s'}
            <br><span style="color:#8a5a00;font-size:11px;">{d['band']}</span></td>
          <td style="{TD}"><a href="{j['url']}" style="color:#1f6feb;">Open</a></td>
        </tr>"""

    _h = health()
    _live = sum(1 for r in _h.values() if r.get("hits", 0) > 0)
    _dead = dead_sources(_h)
    cov = ""
    if _h:
        dead_items = "".join(
            f"<li style='margin-bottom:3px;'><a href='{u}' style='color:#1f6feb;"
            f"text-decoration:none;'>{n}</a> <span style='color:#8a939c;'>"
            f"{c} checks, nothing found</span></li>" for n, u, c in _dead[:20])
        cov = (f'<details style="margin-top:12px;">'
               f'<summary style="font-size:12px;color:#5b6570;cursor:pointer;list-style:revert;">'
               f'<strong>{len(_h)} sources tracked</strong> &middot; {_live} have produced '
               f'postings, {len(_dead)} never have</summary>'
               f'<p style="font-size:12px;color:#8a939c;margin:10px 0 6px;">'
               f'Checked many times and never returned a posting. Either the URL is '
               f'wrong, the page renders in JavaScript, or the source genuinely has '
               f'nothing in his band. Worth a look before trusting a quiet week.</p>'
               f'<ul style="font-size:12px;color:#5b6570;line-height:1.6;'
               f'padding-left:18px;">{dead_items}</ul></details>')

    cut_note = ""
    if held_back:
        lo = min(j["details"].get("score", 0) for j in jobs) if jobs else 0
        by_band = {}
        for j in held_back:
            by_band[j["details"]["band"]] = by_band.get(j["details"]["band"], 0) + 1
        detail = ", ".join(f"{v} {k}" for k, v in sorted(by_band.items(), key=lambda x: -x[1]))
        cut_note = (f'<p style="font-size:12px;color:#5b6570;margin-top:16px;padding-top:12px;'
                    f'border-top:1px solid #eef0f2;">Showing the {len(jobs)} strongest of '
                    f'<strong>{total}</strong> open postings. The {len(held_back)} below the cut '
                    f'scored under {lo} on fit: {detail}. Raise MAX_ROWS to see them.</p>')

    rej = ""
    if rejected:
        def _drop_row(j, w):
            label = j.get("title") or "untitled posting"
            url = j.get("url")
            link = (f'<a href="{url}" style="color:#1f6feb;text-decoration:none;">{label}</a>'
                    if url else label)
            return (f"<li style='margin-bottom:4px;'>{j.get('source','')}: {link} "
                    f"<span style='color:#8a5a00;'>({w})</span></li>")
        items = "".join(_drop_row(j, w) for j, w in rejected[:40])
        # Grouped by reason so the summary line is useful while collapsed.
        # Labels are written to read correctly at any count, so nothing is
        # pluralised on the fly. "2 CFI outside Hawaiis" is how that goes wrong.
        SHORT = [
            (r"^training (program|product)", "training programs"),
            (r"^posted ",                    "too old"),
            (r"^needs \d+ hours",            "hours too high"),
            (r"^instructor role",            "CFI outside Hawaii"),
            (r"^outside his geography",      "wrong location"),
            (r"^rotorcraft",                 "rotorcraft"),
            (r"^multi-year",                 "commitment contracts"),
            (r"^filled",                     "already filled"),
            (r"^(gone|unreachable|no link)", "dead links"),
        ]
        by_reason = {}
        for j, w in rejected:
            label = next((s for pat, s in SHORT if re.search(pat, w, re.I)),
                         re.sub(r"\s*\(.*\)$", "", w))
            by_reason[label] = by_reason.get(label, 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in
                            sorted(by_reason.items(), key=lambda x: -x[1])[:4])
        rej = (f'<details style="margin-top:20px;border-top:1px solid #eef0f2;padding-top:12px;">'
               f'<summary style="font-size:12px;color:#5b6570;cursor:pointer;list-style:revert;">'
               f'<strong>{len(rejected)} checked and dropped</strong>'
               f'{" &middot; " + summary if summary else ""}</summary>'
               f'<p style="font-size:12px;color:#8a939c;margin:10px 0 6px;">'
               f'Listed so an empty table reads as the filter working rather than the '
               f'scraper breaking.</p>'
               f'<ul style="font-size:12px;color:#5b6570;line-height:1.6;padding-left:18px;">'
               f'{items}</ul></details>')

    err = ""
    if errors:
        def _err_row(e):
            if isinstance(e, tuple):
                name, msg, url = e
                return (f"<li style='margin-bottom:4px;'>"
                        f"<a href='{url}' style='color:#1f6feb;text-decoration:none;'>{name}</a>"
                        f" <span style='color:#8a939c;'>{msg}</span></li>")
            return f"<li style='margin-bottom:4px;'>{e}</li>"
        items = "".join(_err_row(e) for e in errors)
        err = (f'<details style="margin-top:12px;">'
               f'<summary style="font-size:12px;color:#8a5a00;cursor:pointer;list-style:revert;">'
               f'<strong>{len(errors)} sources did not respond</strong></summary>'
               f'<p style="font-size:12px;color:#8a939c;margin:10px 0 6px;">'
               f'Nothing found there is not the same as nothing being there. '
               f'Each line is one URL worth fixing.</p>'
               f'<ul style="font-size:12px;color:#5b6570;line-height:1.6;padding-left:18px;">'
               f'{items}</ul></details>')

    empty = ('<tr><td colspan="5" style="padding:22px 12px;font-size:14px;color:#5b6570;">'
             'Nothing cleared the filter this run. The watchlist was checked in full.</td></tr>')

    card = ('background:#fff;border:1px solid #dfe3e8;padding:16px 18px;margin-bottom:14px;')
    eyebrow = ('font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#5b6570;')



    checked = ", ".join((research or {}).get("checked", [])) or "nothing"
    res_card = ""
    if research and (research.get("findings") or research.get("summary")
                     or research.get("url_fixes") or research.get("new_sources")):
        if research.get("error"):
            body = (f'<div style="font-size:13px;color:#8a5a00;">Research pass did not run: '
                    f'{research["error"]}</div>')
        else:
            body = ""
            if research.get("summary"):
                body += (f'<div style="font-size:13px;color:#3a4149;line-height:1.6;'
                         f'margin-bottom:14px;">{research["summary"]}</div>')
            for f in research.get("findings", [])[:8]:
                meta = " &middot; ".join(x for x in [
                    f.get("employer"), f.get("location"),
                    (f"posted {f['posted']}" if f.get("posted") else ""),
                    (f"min {f['min_hours']} hrs" if f.get("min_hours") else ""),
                    f.get("schedule")] if x)
                body += f"""
                <div style="border-left:3px solid #4a6f8a;padding:2px 0 2px 13px;margin-bottom:14px;">
                  <div style="font-size:15px;font-weight:600;line-height:1.3;">
                    <a href="{f.get('url','#')}" style="color:#1b1f24;text-decoration:none;">{f.get('title','')}</a></div>
                  <div style="font-size:12px;color:#5b6570;margin-top:3px;">{meta}</div>
                  <div style="font-size:13px;color:#3a4149;margin-top:5px;line-height:1.5;">{f.get('why','')}</div>
                </div>"""
            if research.get("url_fixes"):
                fixes = "".join(
                    f'<li style="margin-bottom:3px;">{x.get("source","")}: '
                    f'<a href="{x.get("new_url","#")}" style="color:#1f6feb;">{x.get("new_url","")}</a></li>'
                    for x in research["url_fixes"][:8])
                body += (f'<div style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;'
                         f'color:#5b6570;margin-top:16px;">Careers pages that moved</div>'
                         f'<ul style="font-size:12px;line-height:1.6;margin:5px 0 0;padding-left:17px;'
                         f'color:#5b6570;word-break:break-all;">{fixes}</ul>')
            if research.get("new_sources"):
                adds = "".join(
                    f'<li style="margin-bottom:3px;"><a href="{x.get("url","#")}" '
                    f'style="color:#1f6feb;">{x.get("name","")}</a> '
                    f'<span style="color:#8a939c;">{x.get("note","")}</span></li>'
                    for x in research["new_sources"][:8])
                body += (f'<div style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;'
                         f'color:#5b6570;margin-top:16px;">Worth adding to the watchlist</div>'
                         f'<ul style="font-size:12px;line-height:1.6;margin:5px 0 0;padding-left:17px;'
                         f'color:#5b6570;">{adds}</ul>')

        res_card = f"""
     <div style="background:#f7fafc;border:1px solid #d3dde5;border-top:3px solid #4a6f8a;
                 padding:18px 20px;margin-bottom:14px;">
      <div style="font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#4a6f8a;">
       Research notes</div>
      <div style="font-size:12px;color:#6b7d8a;margin:5px 0 14px;line-height:1.5;">
       What a live web search turned up beyond the fixed watchlist. Verify before
       acting; this half reasons rather than scrapes.<br>
       <span style="color:#8a9aa6;">Searched today: {checked}</span></div>
      {body}
     </div>"""

    top_card = ""
    if top:
        blocks = ""
        for j in top:
            d = j["details"]
            why = "".join(
                f'<li style="margin-bottom:3px;">{r}</li>' for r in d.get("reasons", [])[:5])
            blocks += f"""
            <div style="border-left:3px solid #b8860b;padding:2px 0 2px 14px;margin-bottom:16px;">
              <div style="font-size:16px;font-weight:600;line-height:1.3;">
                <a href="{j['url']}" style="color:#1b1f24;text-decoration:none;">{j['title']}</a></div>
              <div style="font-size:12px;color:#5b6570;margin-top:3px;">
                {j['source']} &middot; {j.get('location') or 'location not stated'}
                &middot; min {d.get('min_hours') or 'not stated'} hrs
                &middot; posted {d.get('posted','not stated')}
                {'&middot; <strong style="color:#1a5f7a;">' + d['rotation'] + '</strong>' if d.get('rotation') else ''}</div>
              <div style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;
                          color:#8a6d1f;margin-top:9px;">Why this one</div>
              <ul style="font-size:13px;color:#3a4149;line-height:1.5;margin:4px 0 8px;
                         padding-left:17px;">{why}</ul>
              <a href="{j['url']}" style="font-size:13px;color:#1f6feb;">Open the posting</a>
            </div>"""
        top_card = f"""
     <div style="background:#fffdf5;border:1px solid #e0d3a8;border-top:3px solid #b8860b;
                 padding:18px 20px;margin-bottom:14px;">
      <div style="font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#8a6d1f;">
       Worth stopping for</div>
      <div style="font-size:12px;color:#6b6350;margin:5px 0 14px;line-height:1.5;">
       {len(top)} posting{'s' if len(top)!=1 else ''} scored at or above {TOP_MATCH_SCORE} against
       his actual credentials, not just the hour minimum. These also appear in the table below.</div>
      {blocks}
     </div>"""


    ferry_card = ""
    if ferry:
        items = ""
        for j in ferry[:10]:
            items += f"""
            <div style="border-left:3px solid #8a6d1f;padding:2px 0 2px 12px;margin-bottom:11px;">
              <div style="font-size:14px;line-height:1.35;">
                <a href="{j.get('url','#')}" style="color:#1b1f24;text-decoration:none;">{j['title']}</a></div>
              <div style="font-size:11px;color:#8a939c;margin-top:2px;">{j['source']}</div>
            </div>"""
        ferry_card = f"""
     <div style="{card}">
      <div style="{eyebrow}">Owner and ferry work</div>
      <div style="font-size:12px;color:#5b6570;margin:6px 0 12px;line-height:1.5;">
       Pulled from classifieds and forums, not job boards, because this work rarely reaches one.
       Unverified by definition. Worth knowing: most ferry work is gated by the owner's insurance,
       which usually wants time in make and model. Short repositioning hops in light singles are
       the realistic slice.</div>
      {items}
     </div>"""

    pf_rows = ""
    for j in payfly:
        d = j["details"]
        price = ("$%s/hr" % f"{d['price_hourly']:,}") if d.get("price_hourly") else (
                ("$%s" % f"{d['price_total']:,}") if d.get("price_total") else "not stated")
        flag = ('<span style="background:#1a7f4b;color:#fff;font-size:10px;padding:1px 6px;'
                'border-radius:2px;">GOOD PRICE</span>') if d.get("good_deal") else ""
        pf_rows += f"""
        <tr>
          <td style="{TD}font-size:14px;"><strong>{j['title']}</strong> {flag}
            <br><span style="color:#5b6570;font-size:12px;">{j['source']}</span></td>
          <td style="{TD}">{j.get('location') or 'not stated'}</td>
          <td style="{TD}white-space:nowrap;">{d.get('posted','not stated')}</td>
          <td style="{TD}"><strong>{price}</strong></td>
          <td style="{TD}"><a href="{j['url']}" style="color:#1f6feb;">Open</a></td>
        </tr>"""

    payfly_card = ""
    if pf_rows:
        payfly_card = f"""
     <div style="{card}">
      <div style="{eyebrow}">Paid time building</div>
      <div style="font-size:12px;color:#5b6570;margin:6px 0 10px;line-height:1.5;">
       Cheapest first. Buying multi time in a light twin is ordinary and often the fastest
       way to a rating. Buying jet SIC time is viewed skeptically by some recruiters.
       Priced at or under ${PAYFLY_GOOD_HOURLY:,}/hr, or ${PAYFLY_GOOD_TOTAL:,} for a package,
       gets the green flag.</div>
      <table style="width:100%;border-collapse:collapse;">
       <thead><tr style="text-align:left;">
        <th style="{TH}">Program</th><th style="{TH}">Location</th>
        <th style="{TH}">Posted</th><th style="{TH}">Price</th>
        <th style="{TH}">Link</th></tr></thead>
       <tbody>{pf_rows}</tbody></table>
     </div>"""



    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  @media only screen and (max-width:640px) {{
    .col {{ display:block !important; width:100% !important; padding:0 !important; }}
    .side {{ padding-top:16px !important; }}
  }}
</style></head>
<body style="margin:0;padding:20px;background:#eef1f4;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1b1f24;">
 <div style="max-width:960px;margin:0 auto;">

  <div style="background:#1b1f24;color:#fff;padding:18px 22px;">
   <div style="font-size:10px;letter-spacing:.16em;text-transform:uppercase;opacity:.65;">Pilot job watch</div>
   <div style="font-size:24px;font-weight:600;margin-top:3px;">{len(jobs)} of {total or len(jobs)} matches</div>
   <div style="font-size:13px;margin-top:6px;color:{'#8fd6a8' if changed_n else 'rgba(255,255,255,.55)'};">
    {changed_line}</div>
   <div style="font-size:12px;opacity:.7;margin-top:5px;line-height:1.5;">
    Confirmed still open at {run_time}.
    California, Hawaii, Alaska and the West. Fixed wing. Nothing above {MAX_MINIMUM} hours.
    California, Hawaii, Arizona, Alaska and the West. Instructor roles count only in Hawaii. Training programs, commitment contracts and
    filled postings are dropped and listed at the bottom with the reason.
    Ranked by fit: a stated minimum he meets, then credentials, schedule, housing,
    geography and how fresh the posting is. Hawaii is never cut for space.</div>
  </div>

  <table role="presentation" style="width:100%;border-collapse:collapse;margin-top:14px;">
   <tr>
    <td class="col" valign="top" style="width:62%;padding-right:14px;">
     {top_card}
     <div style="{card}">
      <table style="width:100%;border-collapse:collapse;">
       <thead><tr style="text-align:left;">
        <th style="{TH}">Role</th><th style="{TH}">Location</th>
        <th style="{TH}">Posted</th><th style="{TH}">Min hrs</th>
        <th style="{TH}">Link</th></tr></thead>
       <tbody>{rows or empty}</tbody></table>
      {cut_note}{rej}{err}{cov}
     </div>
     {payfly_card}
     {ferry_card}
     {res_card}
    </td>

    <td class="col side" valign="top" style="width:38%;">
     <div style="{card}">
      <div style="{eyebrow}">Tip</div>
      <div style="font-size:15px;font-weight:600;margin-top:6px;line-height:1.3;">{tip_head}</div>
      <div style="font-size:13px;color:#3a4149;margin-top:6px;line-height:1.55;">{tip_body}</div>
     </div>
     <div style="background:#16232e;border:1px solid #16232e;padding:0;margin-bottom:14px;
                 overflow:hidden;">
      <div style="padding:16px 18px 4px;">
       <div style="font-size:10px;letter-spacing:.18em;text-transform:uppercase;
                   color:#7d9bb0;">On this subject</div>
      </div>
      <div style="padding:0 18px;">
       <div style="font-family:Georgia,'Times New Roman',serif;font-size:44px;line-height:1;
                   color:#c9a227;letter-spacing:-.02em;">{hist_year}</div>
       <div style="font-family:Georgia,'Times New Roman',serif;font-size:19px;line-height:1.25;
                   color:#f2ece0;margin-top:6px;">{hist_title}</div>
       <div style="width:34px;height:2px;background:#c9a227;margin:12px 0 10px;"></div>
       <div style="font-size:13px;color:#c6d2db;line-height:1.6;padding-bottom:18px;">{hist_body}</div>
      </div>
     </div>
    </td>
   </tr>
  </table>

  <div style="font-size:11px;color:#7a838c;margin-top:6px;padding:0 4px;line-height:1.5;">
   Watchlist: FlightSafety, CAE, JSfirm, Climbto350, and Alaska, Hawaii and West Coast operators.
  </div>
 </div></body></html>"""




def _selftest():
    """Fail loudly if assess() stops producing the fields the ranking depends on.

    Three separate edits in this file silently matched nothing and disabled a
    whole subsystem without any error. This catches that class of bug at startup.
    """
    probe = ("now hiring first officer caravan posted 3 days ago minimum 500 hours "
             "housing provided week on week off kailua kona hawaii")
    d, reject = assess(probe, {"title": "First Officer", "location": "Kailua-Kona, HI",
                               "url": "http://x", "raw": ""})
    problems = []
    if reject:
        problems.append(f"probe posting was rejected: {reject}")
    for key in ("posted_dt", "posted", "fresh", "fresh_rank", "tier",
                "band", "band_rank", "min_hours", "rotation", "housing", "kind"):
        if key not in d:
            problems.append(f"assess() did not set {key!r}")
    if d.get("min_hours") != 500:
        problems.append(f"hour parsing: expected 500, got {d.get('min_hours')}")
    if d.get("fresh_rank") != 1:
        problems.append(f"freshness: expected rank 1, got {d.get('fresh_rank')}")
    if d.get("tier") != 3:
        problems.append(f"geography: expected Hawaii (3), got {d.get('tier')}")
    if not d.get("housing"):
        problems.append("housing not detected")

    old, _ = assess("now hiring survey pilot posted 2 years ago minimum 250 hours fresno california",
                    {"title": "Survey Pilot", "location": "Fresno, CA", "url": "http://x", "raw": ""})
    if not _:
        problems.append("a two-year-old posting was not dropped")

    if problems:
        raise SystemExit("SELF TEST FAILED\n  " + "\n  ".join(problems))


def main():
    _selftest()
    # FULL_SWEEP=1 does the whole watchlist plus the research pass. Otherwise this
    # is a fast poll: hot sources and classifieds only, typically under a minute.
    full = bool(os.environ.get("FULL_SWEEP"))
    hashes = _hashes()

    raw, errors = [], []
    h = health()
    hot_rows, checked, skipped = poll(hot_sources(), hashes, h)
    for row in hot_rows:
        (errors.append((row["source"], row["error"][:90], row.get("url", "")))
         if "error" in row else raw.append(row))

    if full:
        for name, url in registry_sources(poll="daily"):
            for row in scrape(name, url):
                if "error" in row:
                    errors.append((row["source"], row["error"][:90], row.get("url", "")))
                else:
                    raw.append(row)
        for fn in WATCHLIST:
            for row in fn():
                if "error" in row:
                    errors.append((row["source"], row["error"][:90], row.get("url", "")))
                else:
                    raw.append(row)
    ferry_raw = dedupe([r for r in fetch_ferry() if "error" not in r])
    raw = dedupe(raw)

    seen = load_seen()
    live, rejected = [], []
    for job in raw:
        status, text = verify_live(job)
        if status != "live":
            rejected.append((job, status)); continue
        details, reason = assess(text, job)
        job["details"] = details
        if reason:
            rejected.append((job, reason)); continue
        job["details"]["score"], job["details"]["reasons"] = score_fit(text, job, job["details"])
        job["is_new"] = job["url"] not in seen
        live.append(job)

    ferry = []
    for job in ferry_raw:
        d, reason = assess(job.get("context", ""), job)
        if reason:
            continue
        d["posted_dt"], d["posted"] = None, "classified"
        d["fresh"], d["fresh_rank"] = "listed recently", 1
        d["score"], d["reasons"] = score_fit(job.get("context", ""), job, d)
        job["details"] = d
        ferry.append(job)
    ferry.sort(key=lambda j: -j["details"]["score"])

    payfly = [j for j in live if j["details"].get("payfly")]
    live = [j for j in live if not j["details"].get("payfly")]
    # A high score on a job he cannot apply for is not a top fit. Band gates it.
    top = [j for j in live if j["details"].get("score", 0) >= TOP_MATCH_SCORE
           and j["details"].get("band_rank", 9) <= 2]
    top.sort(key=lambda j: -j["details"]["score"])
    def recency(j):
        dt = j["details"].get("posted_dt")
        return -dt.timestamp() if dt else 0

    # One relevance number does the ordering. It already folds in whether he can
    # apply, his credentials, the schedule, geography and how fresh the posting is.
    # Ties break on band, then on date.
    live.sort(key=lambda j: (-j["details"].get("score", 0),
                             j["details"]["band_rank"],
                             recency(j)))
    # Hawaii is a thin market and it matters more than its volume suggests, so a
    # Hawaii posting is never cut for space even if it scores below the line.
    # Cheap filters have run. Anything still standing gets adjudicated by Claude,
    # which is far better than regex at telling a posting from a thread about one.
    verdicts = vet_with_llm(live)
    if verdicts:
        kept = []
        for i, j in enumerate(live):
            v = verdicts.get(i)
            if v and not v.get("keep", True):
                rejected.append((j, f"vetting: {v.get('reason', 'not a real posting')}"))
                continue
            if v and v.get("employer"):
                j["details"]["employer"] = v["employer"]
            kept.append(j)
        print(f"vetting: {len(live) - len(kept)} of {len(live)} rejected by review")
        live = kept

    brand_new = [j for j in live if j["is_new"]]
    notify(sorted(brand_new, key=lambda j: -j["details"].get("score", 0)))

    shown, held_back = live[:MAX_ROWS], live[MAX_ROWS:]
    rescued = [j for j in held_back if j["details"].get("tier") == 3]
    if rescued:
        shown = shown + rescued
        held_back = [j for j in held_back if j["details"].get("tier") != 3]
    payfly.sort(key=lambda j: (not j["details"].get("good_deal"), j["details"].get("price_sort", 10**9)))
    # GitHub runners are set to UTC, so .astimezone() alone renders UTC. Pin it to
    # the two zones that actually matter and show both.
    _now = datetime.now(timezone.utc)
    _pt = _now.astimezone(ZoneInfo("America/Los_Angeles"))
    _hi = _now.astimezone(ZoneInfo("Pacific/Honolulu"))
    run_time = (_pt.strftime("%b %-d, %Y at %-I:%M %p %Z")
                + " / " + _hi.strftime("%-I:%M %p HST"))
    research = research_pass(errors) if full else None
    html = build_html(live, top, payfly, rejected, errors, run_time, research)

    out = Path(__file__).parent / "docs"
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(html)
    save_seen(seen | {j["url"] for j in live})
    _save_hashes(hashes)
    HEALTH.write_text(json.dumps(h, indent=1, sort_keys=True))
    mode = "full sweep" if full else "fast poll"
    print(f"{mode}: {checked} hot sources checked, {skipped} unchanged and skipped. "
          f"{len(live)} match(es), {len(brand_new)} NEW, {len(top)} top fit.")


if __name__ == "__main__":
    main()


def _absorb_sources(found):
    """Append newly discovered sources to sources.json so the list grows itself.

    Written as candidates rather than into the main lists: they get polled, and
    source_health.json will show within a week whether they are worth keeping.
    """
    if not found:
        return
    reg = registry()
    if not reg:
        return
    known = {s.get("url") for k in ("boards", "forums", "operators_hot", "candidates")
             for s in reg.get(k, [])}
    added = 0
    for s in found:
        url = (s.get("url") or "").strip()
        if not url.startswith("http") or url in known:
            continue
        reg.setdefault("candidates", []).append({
            "name": s.get("name", "unnamed")[:60],
            "url": url,
            "poll": "daily",
            "kind": s.get("kind", "unknown"),
            "note": (s.get("why") or "")[:160],
            "added": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        })
        known.add(url)
        added += 1
    if added:
        REGISTRY.write_text(json.dumps(reg, indent=2))
        print(f"registry: added {added} discovered source(s)")
