@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Stok ve Satis Takip - Masaustu Uygulamasi (Firebase Bulut Senkronizasyonlu)
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" main.py
) else (
    python main.py
)
