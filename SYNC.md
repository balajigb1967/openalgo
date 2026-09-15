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
It calls `C:\Terminal\openalgo-sync.bat` and shows the output.

Note: the *runtime* copy of the script lives outside the repo on purpose —
the sync switches branches, and a script inside the repo would delete
itself mid-run when leaving `fno-custom` (where it is committed) for
`main`. If `C:\Terminal\openalgo-sync.bat` is ever lost, restore it from
`sync-openalgo-fork.bat` on the `fno-custom` branch.

## Manual sync

```bat
call C:\Terminal\openalgo-sync.bat
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
