@echo off
cd /d "%~dp0"
python -m streamlit run peds_clerkship_tracker.py --server.address 127.0.0.1
pause
