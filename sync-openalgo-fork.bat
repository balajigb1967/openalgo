@echo off
rem Sync the OpenAlgo fork with upstream (marketcalls/openalgo).
rem Runs inside C:\Terminal\openalgo. Exit code 0 = clean sync, 1 = needs attention.
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo  [1/5] Fetching upstream (marketcalls/openalgo)...
git fetch upstream --quiet
if errorlevel 1 (
    echo  [ERROR] Could not fetch upstream. Check your internet connection.
    exit /b 1
)

echo  [2/5] Updating main from upstream (fast-forward only)...
git switch main --quiet
if errorlevel 1 (
    echo  [ERROR] Could not switch to main. Commit or stash local changes first.
    exit /b 1
)
git merge --ff-only upstream/main --quiet
if errorlevel 1 (
    echo  [ERROR] main has diverged from upstream - fast-forward refused.
    echo         Fix manually: see SYNC.md in this folder.
    exit /b 1
)
git push origin main --quiet
if errorlevel 1 (
    echo  [ERROR] Could not push main to origin.
    exit /b 1
)

echo  [3/5] Rebasing fno-custom onto the new main...
git switch fno-custom --quiet
git rebase main --quiet
if errorlevel 1 (
    echo  [CONFLICT] Rebase paused on a conflict. Resolve the files git lists, then:
    echo             git add <files>  ^&^&  git rebase --continue
    echo             Or abort with:   git rebase --abort
    exit /b 1
)

echo  [4/5] Pushing fno-custom...
git push --force-with-lease origin fno-custom --quiet
if errorlevel 1 (
    echo  [ERROR] Could not push fno-custom.
    exit /b 1
)

echo  [5/5] Done. Current state:
echo.
git log --oneline -5
echo.
echo  main        -^> upstream/main  (pristine)
echo  fno-custom  -^> main + custom commits above
exit /b 0
