@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Stok ve Satis Takip - Yerel Web Uygulama Modu (Firebase Senkronizasyonlu)
echo ============================================================================
echo   STOK VE SATIS TAKIP - YEREL UYGULAMA SUNUCUSU BASLATILIYOR...
echo   (Render web sitesine gerek kalmadan bilgisayarda calisir ve Firebase ile esitlenir)
echo ============================================================================

:: Eger 5000 portu dinlenmiyorsa yerel sunucuyu arka planda baslat
netstat -ano | findstr ":5000 " | findstr "LISTENING" >nul
if %errorlevel% neq 0 (
    if exist ".venv\Scripts\pythonw.exe" (
        start "" ".venv\Scripts\pythonw.exe" wsgi.py
    ) else (
        start /min "" python wsgi.py
    )
    timeout /t 2 /nobreak >nul
)

:: Uygulama penceresi modunda (adres cubugu olmadan) ac
start msedge --app=http://localhost:5000 2>nul || start chrome --app=http://localhost:5000 2>nul || start http://localhost:5000
