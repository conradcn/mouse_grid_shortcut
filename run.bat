@echo off
rem Kill any prior mouse_grid.py instance so hotkeys are released cleanly.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -like '*mouse_grid.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
start "" "C:\Users\conra\miniconda3\pythonw.exe" "%~dp0mouse_grid.py"
