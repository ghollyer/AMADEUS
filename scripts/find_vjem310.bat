@echo off
rem Sets AMADEUS_PY to the vjem310 python.exe. Used by the AMADEUS *.bat files.
rem Order: amadeus_config.json (microscope_pc.vjem310_python) -> common conda
rem locations -> "python" on PATH.
set "AMADEUS_PY="
set "AMADEUS_ROOT=%~dp0.."
if exist "%AMADEUS_ROOT%\amadeus_config.json" (
  for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "try { (Get-Content -Raw '%AMADEUS_ROOT%\amadeus_config.json' | ConvertFrom-Json).microscope_pc.vjem310_python } catch { '' }"`) do set "AMADEUS_PY=%%P"
)
if defined AMADEUS_PY if exist "%AMADEUS_PY%" goto :eof
set "AMADEUS_PY="
for %%C in (
  "%USERPROFILE%\.conda\envs\vjem310\python.exe"
  "%USERPROFILE%\anaconda3\envs\vjem310\python.exe"
  "%USERPROFILE%\miniconda3\envs\vjem310\python.exe"
  "%ProgramData%\Anaconda3\envs\vjem310\python.exe"
  "C:\vjem310\python.exe"
) do if not defined AMADEUS_PY if exist %%C set "AMADEUS_PY=%%~C"
if not defined AMADEUS_PY set "AMADEUS_PY=python"
