@echo off
title Clipper
cd /d "%~dp0"
set PY=
python --version >nul 2>nul && set PY=python
if not defined PY py --version >nul 2>nul && set PY=py
if not defined PY goto nopython
ffmpeg -version >nul 2>nul
if errorlevel 1 goto noffmpeg
echo Installing packages (first run only)...
%PY% -m pip install -q -r requirements.txt
if errorlevel 1 goto pipfail
echo.
echo Starting Clipper. Keep this window open while you use it. Close it to stop.
%PY% app.py
pause
exit /b 0

:nopython
echo Python was not found.
echo Open a new terminal, run:  winget install Python.Python.3.12
echo Then close it and double-click start.bat again.
pause
exit /b 1

:noffmpeg
echo ffmpeg was not found.
echo Open a new terminal, run:  winget install Gyan.FFmpeg
echo Then close it and double-click start.bat again.
pause
exit /b 1

:pipfail
echo Installing the Python packages failed. Check your internet connection and try again.
pause
exit /b 1
