@echo off
REM Mesh Showcase - Place all StaticMesh assets in a grid layout
REM Usage: double-click or run from terminal
REM Options: --dry-run  --spacing 200  --clear-existing  --base-path /Game/Path

cd /d "%~dp0"
python mesh_showcase.py %*
pause
