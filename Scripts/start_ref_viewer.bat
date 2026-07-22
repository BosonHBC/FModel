@echo off
chcp 65001 >nul 2>&1
title UE Asset Reference Viewer
cd /d "%~dp0"

echo ========================================
echo   UE Asset Reference Viewer
echo ========================================
echo.

where python >nul 2>&1
if %errorlevel% neq 0 (
    where py >nul 2>&1
    if %errorlevel% neq 0 (
        echo [ERROR] Python not found. Please install Python 3.8+ from python.org
        echo         Make sure to check "Add Python to PATH" during installation.
        pause
        exit /b 1
    )
    py ref_viewer.py
) else (
    python ref_viewer.py
)

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Script exited with code %errorlevel%
    pause
)
