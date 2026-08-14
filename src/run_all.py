"""Orchestrator: one full pipeline pass. Rotates niches round-robin per run."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from common import cfg, log, ROOT
import discover, audit, outreach

def main():
    c = cfg()
    niches = c["run"]["active_niches"]
    counter_f = ROOT / "data" / "run_counter.txt"
    n = int(counter_f.read_text().strip() or 0) if counter_f.exists() else 0
    niche = niches[n % len(niches)]
    counter_f.write_text(str(n + 1))
    log("run_start", run=n, niche=niche)

    discover.discover(niche, c["limits"]["discover_per_run"])
    discover.enrich_new()
    audit.run(c["limits"]["audits_per_run"])
    outreach.compose()
    outreach.open_issues()
    outreach.check_replies()
    outreach.send(c["limits"]["emails_per_run"])
    log("run_done", run=n)

if __name__ == "__main__":
    main()
