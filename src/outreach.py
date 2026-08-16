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

def render_screenshots() -> int:
    """Render one 'AI answer' PNG per audited lead, from the worst-fail prompt.

    Replaces the old manual gh-issue TODO. The image mimics an AI chat panel
    (neutral branding — not fake ChatGPT). Attached to the follow-up email.
    """
    from PIL import Image, ImageDraw, ImageFont
    W, PAD, LH = 900, 40, 26

    def _font(size, bold=False):
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        for base in ("/usr/share/fonts/truetype/dejavu/",
                     "/System/Library/Fonts/Supplemental/", "/Library/Fonts/"):
            try:
                return ImageFont.truetype(base + name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    def wrap(text, font, w):
        out, cur = [], ""
        for word in text.split():
            trial = (cur + " " + word).strip()
            if font.getlength(trial) <= w:
                cur = trial
            else:
                if cur:
                    out.append(cur)
                cur = word
        if cur:
            out.append(cur)
        return out

    leads = load_leads()
    out_dir = ROOT / "screenshots"
    out_dir.mkdir(exist_ok=True)
    fb, fp, fh = _font(18), _font(16), _font(13, bold=True)
    done = 0
    for l in leads.values():
        path = out_dir / f"{l['id']}.png"
        if path.exists() or not l.get("audit_json"):
            continue
        per = json.loads(l["audit_json"]).get("per_prompt") or []
        # worst = first prompt where visible=0 with an answer sample, else first with sample
        pick = next((x for x in per if x.get("visible") == 0 and x.get("sample")), None)
        if not pick:
            pick = next((x for x in per if x.get("sample")), None)
        if not pick:
            continue
        try:
            pl = wrap(pick["prompt"], fb, W - 2 * PAD)
            al = wrap(pick["sample"], fp, W - 2 * PAD)
            header_h = 46
            H = header_h + PAD + 22 + len(pl) * LH + 20 + 22 + len(al) * LH + PAD
            img = Image.new("RGB", (W, H), (255, 255, 255))
            d = ImageDraw.Draw(img)
            d.rectangle([0, 0, W, header_h], fill=(52, 53, 65))
            d.text((PAD, 14), "AI search — what a buyer sees", fill="white", font=fh)
            y = header_h + PAD
            d.text((PAD, y), "BUYER ASKED", fill=(108, 111, 122), font=fh); y += 22
            for ln in pl:
                d.text((PAD, y), ln, fill=(32, 33, 35), font=fb); y += LH
            y += 20
            d.text((PAD, y), "AI ANSWERED", fill=(108, 111, 122), font=fh); y += 22
            for ln in al:
                d.text((PAD, y), ln, fill=(32, 33, 35), font=fp); y += LH
            img.save(path)
            done += 1
            log("screenshot_rendered", company=l["company"], file=path.name)
        except Exception as e:  # noqa: BLE001
            log("screenshot_error", company=l["company"], err=str(e))
    return done


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
    if stage in ("screenshots", "all"):
        render_screenshots()
    if stage in ("replies", "all"):
        check_replies()
    if stage in ("send", "all"):
        send(c["limits"]["emails_per_run"])
