"""Stage 4-7: compose emails, open screenshot TODOs, send via Gmail, detect replies.

compose: LLM writes the email grounded in the playbook + audit findings.
issues:  gh CLI opens a screenshot TODO issue per audited lead (your manual step).
send:    Gmail SMTP, country-policy gate, caps, suppression, staggering,
         follow-up after N days (attaching screenshots/<id>.png if you added one).
replies: IMAP scan of the Gmail inbox; a reply suppresses the lead and opens a HOT issue.
"""
import datetime as dt
import email as email_lib
import imaplib
import json
import mimetypes
import os
import pathlib
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage

from common import ROOT, cfg, load_leads, save_leads, suppressed, log, llm_json, NOW

PLAYBOOK = (ROOT / "templates" / "playbook.md").read_text()


# --------------------------- COMPOSE ---------------------------

def compose(limit: int = 10) -> int:
    c = cfg()
    leads = load_leads()
    done = 0
    for l in leads.values():
        if l["status"] != "audited" or done >= limit:
            continue
        f = json.loads(l["audit_json"] or "{}")
        niche = c["niches"][l["niche"]]
        stat = (f"Asked {f['asked']} buyer questions across {len(c['llm']['audit_panel'])} AI models "
                f"({f['answers']} answers total). {l['company']} appeared in {f['visible_in']} of "
                f"{f['answers']} answers ({f['visibility_pct']}%). "
                f"Competitors named instead: {', '.join(f['competitors_named'][:3]) or 'several others'}.")
        try:
            out = llm_json(
                f"{PLAYBOOK}\n\n"
                f"Write a cold email to the team at {l['company']} ({l['url']}), "
                f"a {niche['label']} business in {l['country'] or 'Europe'}.\n"
                f"Sender: {c['sender']['from_name']}, a software house ({c['sender']['reply_to']}).\n"
                f"Angle: {niche['email_angle']}\n"
                f"THE FINDING (use the concrete numbers, do not exaggerate): {stat}\n"
                f"Verdict: {f['verdict']}.\n"
                f"Offer: a full agent-readiness audit + fix (llms.txt, structured pricing/FAQ, "
                f"comparison pages) so AI assistants recommend them accurately.\n"
                'Return JSON: {"subject": "...", "body": "..."} - body plain text, '
                "under 130 words, no placeholders, no [brackets].",
                temperature=0.5)
            l["subject"], l["body"] = out["subject"].strip()[:120], out["body"].strip()
            l["status"] = "drafted"
            done += 1
            log("drafted", company=l["company"], subject=l["subject"])
        except Exception as e:  # noqa: BLE001
            log("compose_error", company=l["company"], err=str(e))
    save_leads(leads)
    return done


# --------------------------- SCREENSHOT ISSUES ---------------------------

def open_issues() -> int:
    """One TODO issue per newly drafted lead: exact prompts for you to screenshot."""
    if not cfg()["run"]["open_screenshot_issues"] or not os.environ.get("GITHUB_TOKEN"):
        return 0
    leads = load_leads()
    opened = 0
    for l in leads.values():
        if l["status"] != "drafted" or "issue_opened" in (l.get("notes") or ""):
            continue
        f = json.loads(l["audit_json"] or "{}")
        prompts = "\n".join(f"- [ ] `{p['prompt']}`" for p in f.get("per_prompt", []))
        body = (f"**Lead:** {l['company']} — {l['url']} ({l['country']})\n"
                f"**Verdict:** {f.get('verdict')} — visible in {f.get('visible_in')}/{f.get('answers')} AI answers\n"
                f"**Competitors AI names instead:** {', '.join(f.get('competitors_named', []))}\n\n"
                f"### Your 3-minute TODO\nRun these in ChatGPT/Gemini (web), screenshot the best fail:\n{prompts}\n\n"
                f"Save the screenshot as `screenshots/{l['id']}.png`, commit to main.\n"
                f"It will be attached automatically to the follow-up email.\n\n"
                f"First (text-only) email goes out automatically; this screenshot arms the follow-up.")
        try:
            subprocess.run(["gh", "issue", "create", "--title",
                            f"📸 Screenshot TODO: {l['company']} ({f.get('verdict')})",
                            "--body", body],
                           check=True, capture_output=True, text=True, cwd=ROOT)
            l["notes"] = (l.get("notes") or "") + " issue_opened"
            opened += 1
        except Exception as e:  # noqa: BLE001
            log("issue_error", company=l["company"], err=str(e)[:200])
    save_leads(leads)
    return opened


# --------------------------- SEND ---------------------------

def _smtp():
    s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60)
    s.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
    return s


def _build(l: dict, c: dict, followup: bool = False) -> EmailMessage:
    m = EmailMessage()
    m["From"] = f"{c['sender']['from_name']} <{os.environ['GMAIL_ADDRESS']}>"
    m["To"] = l["email"]
    m["Reply-To"] = c["sender"]["reply_to"]
    footer = (f"\n\n--\n{c['sender']['company_line']}\n{c['sender']['postal_line']}\n"
              f"{c['sender']['unsubscribe_line']}")
    if followup:
        m["Subject"] = "Re: " + l["subject"]
        body = (f"Quick follow-up — attaching what buyers actually see when they ask AI "
                f"about your space. Happy to walk you through the full results.\n"
                f"If the timing's wrong, a one-word reply is fine.{footer}")
        m.set_content(body)
        shot = ROOT / "screenshots" / f"{l['id']}.png"
        if shot.exists():
            ctype = mimetypes.guess_type(str(shot))[0] or "image/png"
            main, sub = ctype.split("/")
            m.add_attachment(shot.read_bytes(), maintype=main, subtype=sub, filename="ai-answer.png")
    else:
        m["Subject"] = l["subject"]
        m.set_content(l["body"] + footer)
    return m


def send(limit: int) -> int:
    c = cfg()
    if not c["run"]["auto_send"]:
        log("send_skipped", reason="auto_send=false (drafts remain in leads.csv)")
        return 0
    leads = load_leads()
    supp = suppressed()
    gate = c["policy"]
    today = dt.date.today()
    sent = 0
    smtp = None
    for l in leads.values():
        if sent >= limit:
            break
        is_first = l["status"] == "drafted"
        is_follow = (l["status"] == "sent" and l.get("sent_at")
                     and (today - dt.date.fromisoformat(l["sent_at"][:10])).days
                         >= c["limits"]["followup_after_days"])
        if not (is_first or is_follow):
            continue
        if l["email"].lower() in supp or l["email"].split("@")[-1] in supp:
            l["status"] = "suppressed"
            continue
        if l["country"] and l["country"] in gate["blocked_countries"]:
            l["status"] = "blocked_country"
            continue
        if l["country"] and gate["allowed_countries"] and l["country"] not in gate["allowed_countries"]:
            l["status"] = "blocked_country"
            continue
        try:
            smtp = smtp or _smtp()
            smtp.send_message(_build(l, c, followup=is_follow))
            if is_follow:
                l["status"], l["followup_at"] = "followed_up", NOW()
            else:
                l["status"], l["sent_at"] = "sent", NOW()
            sent += 1
            log("sent", company=l["company"], to=l["email"], followup=is_follow)
            time.sleep(c["limits"]["min_seconds_between_sends"])
        except Exception as e:  # noqa: BLE001
            log("send_error", company=l["company"], err=str(e)[:200])
    if smtp:
        smtp.quit()
    save_leads(leads)
    return sent


# --------------------------- REPLIES ---------------------------

def check_replies() -> int:
    leads = load_leads()
    by_email = {l["email"].lower(): l for l in leads.values() if l.get("email")}
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        M.select("INBOX")
        since = (dt.date.today() - dt.timedelta(days=7)).strftime("%d-%b-%Y")
        _, data = M.search(None, f'(SINCE "{since}")')
        hits = 0
        for num in (data[0].split() if data and data[0] else []):
            _, msg_data = M.fetch(num, "(BODY.PEEK[HEADER])")
            hdr = email_lib.message_from_bytes(msg_data[0][1])
            sender = email_lib.utils.parseaddr(hdr.get("From", ""))[1].lower()
            l = by_email.get(sender)
            if l and l["status"] in ("sent", "followed_up"):
                body_low = str(hdr.get("Subject", "")).lower()
                l["status"] = "replied"
                l["notes"] = (l.get("notes") or "") + " REPLIED"
                hits += 1
                log("REPLY", company=l["company"], from_=sender)
                if "no thanks" in body_low or "unsubscribe" in body_low:
                    with open(ROOT / "data" / "suppression.csv", "a") as f:
                        f.write(sender + "\n")
                elif os.environ.get("GITHUB_TOKEN"):
                    subprocess.run(["gh", "issue", "create", "--title",
                                    f"🔥 REPLY from {l['company']}",
                                    "--body", f"{sender} replied. Go close them.\n{l['url']}"],
                                   capture_output=True, text=True, cwd=ROOT)
        M.logout()
        save_leads(leads)
        return hits
    except Exception as e:  # noqa: BLE001
        log("imap_error", err=str(e)[:200])
        return 0


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    c = cfg()
    if stage in ("compose", "all"):
        compose()
    if stage in ("issues", "all"):
        open_issues()
    if stage in ("replies", "all"):
        check_replies()
    if stage in ("send", "all"):
        send(c["limits"]["emails_per_run"])
