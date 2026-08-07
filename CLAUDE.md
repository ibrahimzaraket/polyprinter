# PolyPrinter
Polymarket trader scouting and copy-trading system.
Read docs/PRD.md, docs/SCHEMA.md, docs/AUDIT.md before writing any code.
("Hermes Oracle v2" in those docs = the previous system being replaced.)

## Non-negotiable
- Build ONLY the phase named. Do not skip ahead.
- Dependencies in the Dockerfile. NEVER a venv in /tmp.
- Every external API call goes through polyprinter/sources/ and persists
  its raw response before anything parses it.
- Every observed trade produces a decisions row — TAKE or SKIP with a reason.
- Do not touch the traefik container or open firewall ports.
  Dashboard binds to 127.0.0.1:8765 only.
- No credentials on this machine. If you need one, stop and ask.
- Commit after every working unit.

## Verify, don't assume
PRD §9 lists Polymarket endpoint assumptions that are UNVERIFIED.
Confirm against live responses and correct the docs before building.
