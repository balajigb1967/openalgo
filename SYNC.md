# Syncing this fork with upstream (marketcalls/openalgo)

## Branch model

| Branch | Contains | Pushed to |
|---|---|---|
| `main` | Pristine upstream OpenAlgo. **Never commit anything here.** | `origin` (balajigb1967/openalgo) |
| `fno-custom` | `main` + all FNO customizations, kept as clean commits on top | `origin` |

`main` must always be a **fast-forward** of upstream/main. All real work happens
on `fno-custom`. If a change only makes sense on `main` (e.g. a CI tweak), it
belongs in an upstream PR, not a direct commit.

## One-click sync (recommended)

Desktop → **FNO-Server-Manager** → **[19] Sync OpenAlgo Fork**.
It runs `sync-openalgo-fork` in `C:\Terminal\openalgo` and shows the output.

## Manual sync

```bat
cd C:\Terminal\openalgo
git switch main
git fetch upstream
git merge --ff-only upstream/main
git push origin main
git switch fno-custom
git rebase main
git push --force-with-lease origin fno-custom
```

## Conflict policy

`rebase` pauses on conflicts. Resolve in the listed files, `git add` them, then
`git rebase --continue`. Abort any time with `git rebase --abort` (your branch
snaps back to how it was). Conflicts should be rare: customizations touch only
a handful of files (see `git log --oneline main..fno-custom`).

## Why not GitHub Actions auto-sync?

Scheduled workflows are disabled after 60 days of repo inactivity, and a merge
conflict would silently stall the auto-branch anyway. The menu item is the
dependable path; Actions can be added later as a convenience layer.

## Where the customizations live

- Commits on `fno-custom` (list with `git log --oneline main..fno-custom`).
- The Oracle VM's `~/openalgo` currently carries these as **uncommitted local
  edits** applied directly on its clone of upstream. To move the VM onto this
  fork: `git remote set-url origin https://github.com/balajigb1967/openalgo.git`,
  `git fetch origin`, then `git switch fno-custom` (see the runbook in the
  fno-trader-pro manager notes before switching a live install).
