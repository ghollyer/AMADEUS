@echo off
rem Double-click to run AMADEUS on the microscope PC.
rem First run: opens the setup pages. Afterwards: the launcher (AMADEUS server,
rem DM status, network doctor, Settings).
cd /d "%~dp0"
call "%~dp0scripts\find_vjem310.bat"
echo Using %AMADEUS_PY%
"%AMADEUS_PY%" -m setup_wizard.launcher %*
if errorlevel 1 pause
