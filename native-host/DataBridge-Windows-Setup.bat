@echo off
REM DataBridge one-file Windows setup — double-click, nothing else needed.
REM This tiny bootstrapper ensures Python (downloads portable Python if none),
REM then fetches the real installer + print host from GitHub and runs them.
REM No sticker is printed during setup. Needs internet once.
REM NOTE: RAW points at the PUBLIC mirror repo (databridge-releases) — the
REM source repo is private, so raw.githubusercontent.com 404s from it.
setlocal
set RAW=https://raw.githubusercontent.com/alif13579/databridge-releases/main/native-host
set WORK=%TEMP%\DataBridgeSetup
mkdir "%WORK%" 2>nul

set PYCMD=
REM Never trust `where python` alone: Windows 10/11 ships a Microsoft Store
REM "app alias" stub that pretends python exists. Prove each candidate by
REM actually running it and checking the output: the stub cannot print "123".
for /f "delims=" %%v in ('python -c "print(123)" 2^>nul') do if "%%v"=="123" set PYCMD=python
if defined PYCMD goto py_ready
for /f "delims=" %%v in ('py -3 -c "print(123)" 2^>nul') do if "%%v"=="123" set PYCMD=py
if defined PYCMD goto py_ready
if not exist "%LOCALAPPDATA%\DataBridge\python\python.exe" goto bootstrap
set PYCMD=%LOCALAPPDATA%\DataBridge\python\python.exe
:py_ready
goto fetch

:bootstrap
echo No working Python found - downloading portable Python (one-time, ~20MB)...
set PYDIR=%LOCALAPPDATA%\DataBridge\python
mkdir "%PYDIR%" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip' -OutFile '%WORK%\py.zip'"
if not %errorlevel%==0 goto dl_fail
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%WORK%\py.zip' -DestinationPath '%PYDIR%' -Force"
if not %errorlevel%==0 goto dl_fail
powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-Content '%PYDIR%\python312._pth') -replace '#import site','import site' | Set-Content '%PYDIR%\python312._pth'"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%WORK%\get-pip.py'"
if not %errorlevel%==0 goto dl_fail
"%PYDIR%\python.exe" "%WORK%\get-pip.py" --no-warn-script-location
if not %errorlevel%==0 goto dl_fail
echo Portable Python ready.
set PYCMD=%PYDIR%\python.exe
goto fetch

:dl_fail
echo ERROR: download failed. Check internet and double-click again.
pause
exit /b 1

:fetch
echo Downloading installer...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%RAW%/install-windows.py' -OutFile '%WORK%\install-windows.py'"
if not %errorlevel%==0 goto dl_fail
"%PYCMD%" "%WORK%\install-windows.py" %*
echo.
pause
