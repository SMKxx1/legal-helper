# Legal Helper

A Microsoft Word task-pane add-in plus a small FastAPI service that reviews a `.docx` against a
legal playbook using a team of LLM agents over OpenRouter (Zero-Data-Retention routes only), and
returns findings the add-in applies as tracked changes + comments. Built as a teaching demo for the
"Deployment 2" workshop.

This repo is mid-rebuild. The authoritative spec — architecture, decisions, data model, and the
phase-by-phase build plan — lives in [`LEGAL_HELPER_PLAN.md`](LEGAL_HELPER_PLAN.md). This README gets
a real quickstart + architecture write-up once the rebuild reaches Phase 6 (see the plan's §6).

```bash
make install    # create backend/.venv and install pinned deps
make run        # serve the API on http://localhost:8000
curl -s localhost:8000/healthz    # -> {"status":"ok"}
```
