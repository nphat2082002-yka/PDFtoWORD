@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "%~dp0app.py" %*
if errorlevel 1 (
    echo.
    echo [ERROR] Ung dung ket thuc voi ma loi %ERRORLEVEL%.
    pause
)
