@echo off
setlocal
cd /d "%~dp0"
set "NBRB_PY=%~dp0.venv_nbrb\Scripts\python.exe"

if not exist "%~dp0parser.py" (
    echo ERROR: parser.py must be next to this BAT file.
    goto fail
)

if not exist "%NBRB_PY%" (
    echo Creating a separate Python environment...
    py -3.13 -m venv "%~dp0.venv_nbrb"
    if errorlevel 1 goto fail
)

"%NBRB_PY%" -I -c "import numpy, pandas, pytz, pdfplumber, pypdf" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies. Internet connection is required...
    "%NBRB_PY%" -I -m pip install --upgrade pip
    if errorlevel 1 goto fail
    "%NBRB_PY%" -I -m pip install --upgrade --only-binary=:all: numpy pandas pytz pdfplumber pypdf
    if errorlevel 1 goto fail
    "%NBRB_PY%" -I -c "import numpy, pandas, pytz, pdfplumber, pypdf"
    if errorlevel 1 goto fail
)

echo Starting parser...
"%NBRB_PY%" -I -u "%~dp0parser.py" %*
if errorlevel 1 goto fail
echo Finished.
pause
exit /b 0

:fail
echo.
echo ERROR: see the message above. Parser did not finish successfully.
pause
exit /b 1
