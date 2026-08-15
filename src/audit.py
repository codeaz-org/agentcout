"""Stage 3: the audit engine - "selling the screenshot" as data.

For each enriched lead, run the niche's buyer-intent prompts through a panel of
free models (simulating "what does AI tell buyers"). Programmatically score:
  - visibility:   in how many answers does the company appear at all?
  - competitors:  which other names get recommended instead?
  - claims:       what does AI assert about them (pricing etc.) - flagged for
                  the human screenshot/verify step, since we can't know truth.
Findings are stored as JSON on the lead and drive both the email copy and the
GitHub screenshot TODO issue.
"""
import json
import re
import sys

from common import cfg, load_leads, save_leads, log, llm

SYSTEM = ("You are a helpful assistant advising a real buyer. Answer naturally and "
          "concretely, naming specific companies/products when you can.")


def run_panel(prompt: str) -> list[str]:
    answers = []
    for provider in cfg()["llm"]["audit_panel"]:
        try:
            answers.append(llm(prompt, system=SYSTEM, provider=provider,
                               temperature=0.6, max_tokens=500))
        except RuntimeError as e:
            log("audit_model_failed", provider=provider, err=str(e))
    return answers


def mentions(company: str, text: str) -> bool:
    # match on the distinctive token(s) of the name, not the full legal string
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", company) if len(t) > 3][:2]
    return bool(tokens) and all(t.lower() in text.lower() for t in tokens)


def extract_competitors(answers: list[str], company: str) -> list[str]:
    """Ask the LLM to list org names recommended across the answers."""
    joined = "\n---\n".join(answers)[:6000]
    try:
        raw = llm(
            "List the company/clinic/product names recommended in these AI answers, "
            f"excluding '{company}'. Return a comma-separated list only, max 6 names.\n\n{joined}",
            temperature=0, max_tokens=120)
        names = [n.strip() for n in raw.split(",") if 2 < len(n.strip()) < 60]
        return names[:6]
    except RuntimeError:
        return []


def audit_lead(lead: dict, niche: dict) -> dict:
    country = lead.get("country") or "your country"
    prompts = [p.format(company=lead["company"], country=country,
                        category=lead.get("category", niche["label"]))
               for p in niche["buyer_prompts"]][:4]

    per_prompt, total_answers, visible_in = [], 0, 0
    all_answers = []
    for p in prompts:
        answers = run_panel(p)
        total_answers += len(answers)
        vis = sum(1 for a in answers if mentions(lead["company"], a))
        visible_in += vis
        all_answers += answers
        per_prompt.append({"prompt": p, "answers": len(answers), "visible": vis,
                           "sample": (answers[0][:400] if answers else "")})

    competitors = extract_competitors(all_answers, lead["company"])
    findings = {
        "asked": len(prompts),
        "answers": total_answers,
        "visible_in": visible_in,
        "visibility_pct": round(100 * visible_in / total_answers) if total_answers else 0,
        "competitors_named": competitors,
        "verdict": ("invisible" if visible_in == 0 else
                    "weak" if visible_in <= total_answers // 3 else "visible"),
        "per_prompt": per_prompt,
    }
    return findings


def run(limit: int) -> int:
    c = cfg()
    leads = load_leads()
    done = 0
    for l in leads.values():
        if l["status"] != "enriched" or done >= limit:
            continue
        niche = c["niches"][l["niche"]]
        try:
            findings = audit_lead(l, niche)
        except Exception as e:  # noqa: BLE001
            log("audit_error", company=l["company"], err=str(e))
            continue
        l["audit_json"] = json.dumps(findings, ensure_ascii=False)
        l["status"] = "audited"
        done += 1
        log("audited", company=l["company"], verdict=findings["verdict"],
            visibility=f"{findings['visible_in']}/{findings['answers']}")
    save_leads(leads)
    return done


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else cfg()["limits"]["audits_per_run"])
