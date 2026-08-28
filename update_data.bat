@echo off
chcp 65001 >nul
cd /d "%~dp0scripts"
echo ============================================
echo  CPBL / NPB Prediction Data Update
echo ============================================

echo.
echo [1/5] CPBL schedule and results...
python fetch_cpbl.py
if errorlevel 1 goto :error

echo.
echo [2/6] CPBL pitcher and batter stats...
python fetch_cpbl_players.py
if errorlevel 1 goto :error

echo.
echo [3/6] NPB schedule and results...
python fetch_npb.py
if errorlevel 1 goto :error

echo.
echo [4/6] NPB pitcher and batter stats...
python fetch_npb_players.py
if errorlevel 1 goto :error

echo.
echo [5/6] CPBL + NPB probable starters + market odds (best-effort, playsport.cc)...
python fetch_playsport_odds.py

echo.
echo [6/6] Generating website...
python generate_site.py
if errorlevel 1 goto :error

echo.
echo ============================================
echo  Done. Open site\index.html to view results.
echo ============================================
pause
exit /b 0

:error
echo.
echo ============================================
echo  Update failed - see error above.
echo ============================================
pause
exit /b 1
