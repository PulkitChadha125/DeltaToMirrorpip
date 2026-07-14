@echo off
setlocal

:: Open Windows Firewall for TCP port 5050 (requires Administrator)
net session >nul 2>&1
if errorlevel 1 (
  echo Requesting Administrator permission...
  powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo ========================================
echo  Open firewall port 5050
echo ========================================
echo.

netsh advfirewall firewall delete rule name="DeltaToMirrorPip 5050" >nul 2>&1

netsh advfirewall firewall add rule name="DeltaToMirrorPip 5050" dir=in action=allow protocol=TCP localport=5050 profile=any
if errorlevel 1 (
  echo Failed to add firewall rule.
  pause
  exit /b 1
)

echo Firewall rule added successfully.
echo.
echo Port 5050 is now open for inbound TCP.
echo Access the app at:
echo   http://THIS-SERVER-IP:5050
echo.
echo Tip: run run.bat to start the Flask app bound to 0.0.0.0:5050
echo.
pause
endlocal
