"""Shared plumbing: config, CSV-in-repo state, and the free-LLM provider chain."""
import csv
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import time

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
NOW = lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

LEAD_FIELDS = [
    "id", "niche", "company", "url", "country", "email", "category",
    "status",            # new -> enriched -> audited -> drafted -> sent -> followed_up | replied | suppressed | no_email | blocked_country
    "audit_json", "subject", "body",
    "first_seen", "sent_at", "followup_at", "notes",
]


def cfg():
    c = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
    c["niches"] = yaml.safe_load((ROOT / "config" / "niches.yaml").read_text())
    return c


def lead_id(url: str) -> str:
    domain = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url.strip().lower())).split("/")[0]
    return hashlib.sha1(domain.encode()).hexdigest()[:10]


# ---------- CSV state (committed back to the repo by the workflow) ----------

def load_leads() -> dict:
    path = DATA / "leads.csv"
    if not path.exists():
        return {}
    with open(path, newline="") as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def save_leads(leads: dict) -> None:
    with open(DATA / "leads.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEAD_FIELDS)
        w.writeheader()
        for r in leads.values():
            w.writerow({k: r.get(k, "") for k in LEAD_FIELDS})


def suppressed() -> set:
    path = DATA / "suppression.csv"
    if not path.exists():
        return set()
    return {line.strip().lower() for line in path.read_text().splitlines() if line.strip()}


def log(event: str, **kw) -> None:
    path = DATA / "activity.log"
    with open(path, "a") as f:
        f.write(json.dumps({"t": NOW(), "event": event, **kw}) + "\n")
    print(f"[{event}] {kw}")


# ---------- Free LLM chain with fallback ----------

def llm(prompt: str, system: str = "", model_override: str | None = None,
        temperature: float = 0.4, max_tokens: int = 900) -> str:
    """Try each configured provider in order; return first success."""
    chain = cfg()["llm"]["chain"]
    errors = []
    for p in chain:
        key = os.environ.get(p["env_key"], "")
        if not key:
            errors.append(f"{p['provider']}: no {p['env_key']}")
            continue
        model = model_override or p["model"]
        # model_override may belong to another provider's catalog; only force it
        # on the provider whose catalog it matches, else use provider default.
        if model_override and model_override not in (p["model"],):
            if (":free" in model_override) != (p["provider"] == "openrouter"):
                model = p["model"]
        try:
            r = requests.post(
                f"{p['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "messages": ([{"role": "system", "content": system}] if system else [])
                             + [{"role": "user", "content": prompt}],
                },
                timeout=90,
            )
            if r.status_code == 429:
                errors.append(f"{p['provider']}: rate-limited")
                time.sleep(2)
                continue
            r.raise_for_status()
            out = r.json()["choices"][0]["message"]["content"].strip()
            if out:
                return out
        except Exception as e:  # noqa: BLE001 - fall through the chain
            errors.append(f"{p['provider']}: {e}")
    raise RuntimeError("All LLM providers failed: " + " | ".join(errors))


def llm_json(prompt: str, system: str = "", **kw) -> dict:
    raw = llm(prompt + "\n\nRespond with ONLY valid JSON. No prose, no markdown fences.",
              system=system, **kw)
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
    return json.loads(raw)
