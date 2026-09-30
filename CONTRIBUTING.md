# Contributing to hunchfall

Team **dead Wallets** — Hefty Hacks, Finance x Trading track.

## Who owns what

| Area | Owners |
|---|---|
| `backend/` (FastAPI, Jev, policy gate, paper execution) | Aska, Ikio |
| `frontend/` (React dashboard) | Tapabroto |
| `docs/` | everyone |

Stay in your lane, but anyone may open a PR anywhere.

## Workflow

1. Never commit directly to `main`. Create a feature branch:
   `git checkout -b feat/short-name` (or `fix/...`).
2. One teammate reviews every PR before merge. Keep PRs small.
3. CI must pass: backend `pytest`, frontend `npm run build`.
4. Merge with squash to keep `main` readable.

## Rules (non-negotiable)

- **Never commit `.env` or any API key.** Copy `.env.example` to `.env`
  locally. If a key ever lands in git, tell the team immediately so it can
  be revoked and purged.
- **Paper trading only.** No order-placement endpoints, no wallet signing
  code, no private keys — in code, env, or docs. Any PR introducing them
  fails review automatically.
- **Honesty rules** (`docs/HONESTY.md`) apply to every surface: no invented
  win rates, MOCK/SAMPLE/REAL labels never stripped, vetoes/losses/fees
  always visible.

## Local dev

```bash
make install   # backend deps + frontend deps
make dev       # runs backend API and frontend together
make test      # backend pytest
```

Or manually:

```bash
cd backend && pip install -r requirements.txt && python -m app.loop --once
cd frontend && npm install && npm run dev
```
