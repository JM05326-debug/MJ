@echo off
chcp 65001 >nul
rem Runs locally (via Windows Task Scheduler — see README) because
rem www.cpbl.com.tw blocks GitHub Actions' published IP ranges. Fetches
rem fresh CPBL data and pushes it straight to the repo; the cloud workflow
rem (.github/workflows/daily_predict.yml) is timed to run ~15 minutes after
rem this so it picks up the fresh push. Everything else (NPB, prediction
rem locking, results collection, training, dashboard) stays fully cloud-
rem automated and does not depend on this machine being on.
cd /d "%~dp0.."

rem No pull here before fetching on purpose: fetching only overwrites local
rem data/*.json files via a plain Python script, it doesn't touch git state,
rem so it can't conflict with anything upstream. This also makes a run
rem that got killed/timed out mid-fetch safe to just retry — an interrupted
rem previous run may leave data/*.json modified-but-uncommitted, and a pull
rem here would fail on that dirty tree before ever getting to fetch again.

echo [1/4] CPBL schedule...
python scripts\fetch_cpbl.py
if errorlevel 1 goto :error

echo [2/4] CPBL player stats...
python scripts\fetch_cpbl_players.py
if errorlevel 1 goto :error

echo [3/4] playsport odds + probable starters (best-effort)...
python scripts\fetch_playsport_odds.py

echo [4/4] commit and push...
git add data\cpbl_data.json data\cpbl_pitchers.json data\cpbl_batters.json data\cpbl_odds.json data\npb_odds.json
git diff --cached --quiet
if errorlevel 1 (
    git commit -m "local CPBL update: %date% %time%"
    git pull --rebase
    if errorlevel 1 goto :error
    git push
    if errorlevel 1 goto :error
) else (
    echo no changes to commit
)

echo Done.
exit /b 0

:error
echo Update failed - see error above.
exit /b 1
