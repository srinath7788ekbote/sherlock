@echo off
setlocal

echo ============================================
echo   Sherlock - Automated Setup (Windows)
echo ============================================
echo.

:: --------------------------------------------------
:: Check for Administrator privileges
:: --------------------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] This script must be run as Administrator.
    echo         Right-click the file and select "Run as administrator".
    pause
    exit /b 1
)

:: --------------------------------------------------
:: Step 1 - Install Chocolatey (if not already installed)
:: --------------------------------------------------
where choco >nul 2>&1
if %errorlevel% neq 0 (
    echo [1/5] Installing Chocolatey...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))"
    if %errorlevel% neq 0 (
        echo [ERROR] Chocolatey installation failed.
        pause
        exit /b 1
    )
    :: Refresh PATH so choco is available in this session
    set "PATH=%ALLUSERSPROFILE%\chocolatey\bin;%PATH%"
    echo [1/5] Chocolatey installed.
) else (
    echo [1/5] Chocolatey already installed, skipping.
)
echo.

:: --------------------------------------------------
:: Step 2 - Install Make (if not already installed)
:: --------------------------------------------------
where make >nul 2>&1
if %errorlevel% neq 0 (
    echo [2/5] Installing Make...
    choco install make -y
    if %errorlevel% neq 0 (
        echo [ERROR] Make installation failed.
        pause
        exit /b 1
    )
    echo [2/5] Make installed.
) else (
    echo [2/5] Make already installed, skipping.
)
echo.

:: --------------------------------------------------
:: Step 3 - Install Python (if not already installed)
:: --------------------------------------------------
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [3/5] Installing Python (latest)...
    choco install python -y
    if %errorlevel% neq 0 (
        echo [ERROR] Python installation failed.
        pause
        exit /b 1
    )
    :: Refresh PATH for python
    for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "PATH=%%B;%PATH%"
    echo [3/5] Python installed.
) else (
    echo [3/5] Python already installed, skipping.
)
echo.

:: --------------------------------------------------
:: Step 4 - Create virtual environment
:: --------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [4/5] Creating virtual environment...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [4/5] Virtual environment created.
) else (
    echo [4/5] Virtual environment already exists, skipping.
)
echo.

:: --------------------------------------------------
:: Step 5 - Install dependencies
:: --------------------------------------------------
echo [5/5] Installing dependencies...
call .venv\Scripts\activate.bat
pip install -e ".[dev]"
if %errorlevel% neq 0 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)
echo [5/5] Dependencies installed.
echo.

:: --------------------------------------------------
:: Done
:: --------------------------------------------------
echo ============================================
echo   Setup complete!
echo ============================================
echo.
echo Next steps:
echo   1. Open a NEW terminal (regular, not admin)
echo   2. cd into this folder
echo   3. Activate the venv:  .venv\Scripts\activate.bat
echo   4. Run:  make connect
echo      (saves your New Relic credentials securely)
echo   5. Configure your AI client's MCP settings
echo      (see README.md, Step 7)
echo.
pause
