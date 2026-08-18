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

echo [1/5] git pull...
git pull --rebase
if errorlevel 1 goto :error

echo [2/5] CPBL schedule...
python scripts\fetch_cpbl.py
if errorlevel 1 goto :error

echo [3/5] CPBL player stats...
python scripts\fetch_cpbl_players.py
if errorlevel 1 goto :error

echo [4/5] CPBL odds (best-effort)...
python scripts\fetch_cpbl_odds.py

echo [5/5] commit and push...
git add data\cpbl_data.json data\cpbl_pitchers.json data\cpbl_batters.json data\cpbl_odds.json
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
