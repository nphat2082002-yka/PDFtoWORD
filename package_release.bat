@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  VN-Office PDF to Word Pro v1.0.0 - Build Release Package
echo  Copyright (C) 2026 Huynh Ngoc Phat
echo ============================================================
python "%~dp0build_release.py" %*
if errorlevel 1 (
    echo.
    echo [ERROR] Dong goi that bai voi ma loi %ERRORLEVEL%.
    pause
    exit /b %ERRORLEVEL%
)
echo.
echo [OK] Da dong goi xong trong thu muc dist\VN-Office-PDF-to-Word-Pro
