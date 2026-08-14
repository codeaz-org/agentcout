"""Stage 1+2: discover candidate companies per niche and enrich with a contact email.

Discovery: DuckDuckGo (ddgs lib, free, keyless) over the niche's queries.
Enrichment: fetch homepage + likely contact pages, extract emails (prefer generic
inboxes), guess country from TLD / address / phone prefix. No paid APIs anywhere.
"""
import random
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests

from common import cfg, lead_id, load_leads, save_leads, log, NOW

UA = {"User-Agent": "Mozilla/5.0 (compatible; AgentScout/1.0; +https://codeaz.org)"}
CONTACT_HINTS = ["contact", "kontakt", "impressum", "about", "despre", "kapcsolat", "contacto", "contactos", "contatti"]
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
BAD_EMAIL = re.compile(r"(example\.|@sentry|@w3|\.png|\.jpg|\.webp|no-?reply|wixpress|godaddy|@schema)")
GENERIC = ("info@", "hello@", "office@", "contact@", "hi@", "clinic@", "enquiries@", "sales@", "reception@")

TLD_COUNTRY = {".ro": "RO", ".pl": "PL", ".hu": "HU", ".es": "ES", ".pt": "PT", ".fr": "FR",
               ".ie": "IE", ".it": "IT", ".bg": "BG", ".gr": "GR", ".cz": "CZ", ".sk": "SK",
               ".hr": "HR", ".si": "SI", ".se": "SE", ".fi": "FI", ".ee": "EE", ".lt": "LT",
               ".lv": "LV", ".de": "DE", ".at": "AT", ".nl": "NL", ".dk": "DK", ".ch": "CH",
               ".co.uk": "GB", ".uk": "GB", ".be": "BE", ".no": "NO", ".cy": "CY", ".mt": "MT"}
PHONE_COUNTRY = {"+40": "RO", "+48": "PL", "+36": "HU", "+34": "ES", "+351": "PT", "+33": "FR",
                 "+353": "IE", "+39": "IT", "+359": "BG", "+30": "GR", "+420": "CZ", "+44": "GB",
                 "+49": "DE", "+43": "AT", "+31": "NL", "+45": "DK", "+41": "CH"}


def ddg(query: str, n: int = 8) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # older package name
    try:
        with DDGS() as d:
            return list(d.text(query, max_results=n, region="eu-en"))
    except Exception as e:  # noqa: BLE001
        log("ddg_error", query=query, err=str(e))
        return []


def guess_country(url: str, html: str) -> str:
    host = urlparse(url).netloc.lower()
    for tld, cc in sorted(TLD_COUNTRY.items(), key=lambda x: -len(x[0])):
        if host.endswith(tld):
            return cc
    for prefix, cc in PHONE_COUNTRY.items():
        if prefix in html or prefix.replace("+", "00") in html:
            return cc
    return ""


def extract_email(html: str) -> str:
    cands = [e.lower().strip(".") for e in EMAIL_RE.findall(html) if not BAD_EMAIL.search(e.lower())]
    if not cands:
        return ""
    generics = [e for e in cands if e.startswith(GENERIC)]
    pool = generics or cands
    return min(pool, key=len)  # shortest tends to be the real inbox


def fetch(url: str) -> str:
    try:
        r = requests.get(url, headers=UA, timeout=25, allow_redirects=True)
        if r.ok and "text/html" in r.headers.get("content-type", "html"):
            return r.text[:400_000]
    except Exception:  # noqa: BLE001
        pass
    return ""


def enrich(url: str) -> tuple[str, str]:
    """Return (email, country) by checking homepage then contact-ish pages."""
    home = fetch(url)
    if not home:
        return "", ""
    country = guess_country(url, home)
    email = extract_email(home)
    if not email:
        links = re.findall(r'href=["\']([^"\']+)["\']', home, flags=re.I)
        contactish = [urljoin(url, h) for h in links if any(k in h.lower() for k in CONTACT_HINTS)]
        for page in list(dict.fromkeys(contactish))[:3]:
            html = fetch(page)
            email = extract_email(html)
            country = country or guess_country(url, html)
            if email:
                break
            time.sleep(1)
    return email, country


SKIP_DOMAINS = ("facebook.", "instagram.", "linkedin.", "youtube.", "wikipedia.", "reddit.",
                "tripadvisor.", "trustpilot.", "yelp.", "google.", "whatclinic.", "clinicadvisor",
                "medium.", "quora.", "amazon.", "apps.shopify.com", "g2.com", "capterra")


def discover(niche_key: str, budget: int) -> int:
    c = cfg()
    niche = c["niches"][niche_key]
    leads = load_leads()
    known_domains = {urlparse(l["url"]).netloc.replace("www.", "") for l in leads.values()}
    added = 0
    queries = random.sample(niche["discovery_queries"], k=min(3, len(niche["discovery_queries"])))
    for q in queries:
        for hit in ddg(q):
            url = hit.get("href") or hit.get("url") or ""
            dom = urlparse(url).netloc.replace("www.", "")
            if not dom or any(s in dom for s in SKIP_DOMAINS) or dom in known_domains:
                continue
            lid = lead_id(url)
            if lid in leads:
                continue
            leads[lid] = {
                "id": lid, "niche": niche_key,
                "company": (hit.get("title") or dom).split(" - ")[0].split(" | ")[0].strip()[:80],
                "url": f"https://{dom}", "country": "", "email": "",
                "category": niche["label"], "status": "new",
                "first_seen": NOW(), "notes": f"via: {q}",
            }
            known_domains.add(dom)
            added += 1
            if added >= budget:
                break
        if added >= budget:
            break
        time.sleep(2)
    save_leads(leads)
    log("discover_done", niche=niche_key, added=added)
    return added


def enrich_new(limit: int = 15) -> int:
    c = cfg()
    pol = c["policy"]
    leads = load_leads()
    done = 0
    for l in leads.values():
        if l["status"] != "new" or done >= limit:
            continue
        email, country = enrich(l["url"])
        l["email"], l["country"] = email, country
        if not email:
            l["status"] = "no_email"
        elif country in pol["blocked_countries"]:
            l["status"] = "blocked_country"
        else:
            l["status"] = "enriched"
        done += 1
        log("enriched", company=l["company"], email=bool(email), country=country, status=l["status"])
        time.sleep(1.5)
    save_leads(leads)
    return done


if __name__ == "__main__":
    niche = sys.argv[1] if len(sys.argv) > 1 else cfg()["run"]["active_niches"][0]
    discover(niche, cfg()["limits"]["discover_per_run"])
    enrich_new()
