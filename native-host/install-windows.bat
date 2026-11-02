@echo off
REM DataBridge print-host installer for Windows — just double-click it.
REM Fully automatic, Python NOT required: if no python exists, a portable
REM Python is downloaded once (~20MB) into %LOCALAPPDATA%\DataBridge\python.
REM COM port + extension ID are auto-detected. No sticker is printed.
REM Rare manual overrides: install-windows.bat [COM_PORT] [EXTRA_EXT_ID]
setlocal
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
goto runpy

:bootstrap
echo No working Python found - downloading portable Python (one-time, ~20MB)...
set PYDIR=%LOCALAPPDATA%\DataBridge\python
set PYZIP=%TEMP%\db-python-embed.zip
mkdir "%PYDIR%" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip' -OutFile '%PYZIP%'"
if not %errorlevel%==0 goto dl_fail
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%PYZIP%' -DestinationPath '%PYDIR%' -Force"
if not %errorlevel%==0 goto dl_fail
del "%PYZIP%" 2>nul
REM Enable site-packages in the embeddable distro (needed for pip).
powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-Content '%PYDIR%\python312._pth') -replace '#import site','import site' | Set-Content '%PYDIR%\python312._pth'"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%TEMP%\db-get-pip.py'"
if not %errorlevel%==0 goto dl_fail
"%PYDIR%\python.exe" "%TEMP%\db-get-pip.py" --no-warn-script-location
if not %errorlevel%==0 goto dl_fail
del "%TEMP%\db-get-pip.py" 2>nul
echo Portable Python ready.
set PYCMD=%PYDIR%\python.exe
goto runpy

:dl_fail
echo ERROR: download failed. Check internet and double-click again.
echo (Or install Python from https://www.python.org/downloads/ with "Add to PATH".)
pause
exit /b 1

:runpy
"%PYCMD%" "%~dp0install-windows.py" %*
echo.
pause
