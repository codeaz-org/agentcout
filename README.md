# AgentScout — automated "sell the screenshot" outreach for Agent Readiness

Fully automatic cold-outreach machine on GitHub Actions, $0/month:
**discovers** EU businesses in your niche → **audits** how AI assistants talk about
them (buyer-intent prompts across a panel of free models) → **drafts** a cold email
led by the concrete finding ("you appeared in 0 of 8 AI answers; X was recommended
instead") → **opens a GitHub issue** telling you exactly which screenshot to take →
**sends** via Gmail with EU country gates, caps and suppression → **detects replies**
over IMAP and opens a 🔥 issue when a prospect answers.

Successor to gtm-lite, but on autopilot: no human approval step (switchable).

## Setup (15 minutes)

1. Create a **private** GitHub repo, push this folder.
2. Gmail: use a fresh/secondary Google account (protect your main domain's
   reputation). Enable 2FA → create an **App Password** (Google Account →
   Security → App passwords).
3. Repo → Settings → Secrets and variables → Actions, add:
   - `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`
   - `OPENROUTER_API_KEY` (free at openrouter.ai — optional but recommended: it's
     the 2nd audit-panel model) and/or `NVIDIA_API_KEY` (build.nvidia.com, free)
   - `GITHUB_TOKEN` is automatic — it powers GitHub Models (free LLM) and issues.
4. Edit `config/config.yaml`: your from_email, postal_line (legally required),
   and set `auto_send: false` **for the first 2 runs** — check the drafts in
   `data/leads.csv`, then flip to `true`.
5. Actions tab → run "AgentScout pipeline" manually once. Done — it now runs
   twice every weekday.

## Your only manual job

Watch for issues labeled `screenshot-todo`: each gives you 3-4 exact prompts to
paste into ChatGPT/Gemini. Screenshot the worst fail, save it as
`screenshots/<lead-id>.png` (filename is in the issue), commit. The follow-up
email (day 4) attaches it automatically. First emails go out text-only with the
statistics — the screenshot arms the follow-up punch.

And issues labeled `hot-reply` — that's a prospect answering. Close them yourself;
that part will never be automatic.

## Niche strategy

`config/niches.yaml` ships with 4 EU-tuned niches; `run.active_niches` round-robins
between them. Give each niche ~60-80 sends (2-3 weeks) before judging. Kill anything
with zero replies; double down where you get 2+. Dental tourism and relocation law
are the highest-value-per-client starts; B2B SaaS is the biggest pond.

## The legal line (read once, seriously)

B2B cold email with accurate sender info + opt-out is workable in most of the EU,
but DE/AT/NL/DK/CH effectively require consent — they're blocked in `policy` and
should stay blocked. The system prefers generic inboxes (info@) over named people,
keeps volume at 16/day, includes your postal address and an opt-out line, and
auto-suppresses anyone who replies "no thanks". Fill in `postal_line` before
sending anything. This is engineering hygiene, not legal advice.

## Costs

| Piece | Free tier | Our usage |
|---|---|---|
| GitHub Actions | 2000 min/mo | ~10 min/day |
| GitHub Models | free w/ GITHUB_TOKEN | audits + drafts |
| OpenRouter :free | 50 req/day | 2nd audit model |
| DuckDuckGo (ddgs) | keyless | discovery |
| Gmail SMTP/IMAP | free | 16 sends/day, reply scan |

## Files
```
src/discover.py   find companies (DDG) + extract emails/country from sites
src/audit.py      buyer-intent prompt panel + visibility scoring
src/outreach.py   compose / screenshot issues / send / reply detection
src/run_all.py    orchestrator (niche round-robin)
config/           sender identity, caps, country policy, niches
templates/playbook.md  the rules every draft must obey
data/             state, committed back by the workflow
```
