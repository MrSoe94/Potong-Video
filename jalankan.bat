@echo off
title Aplikasi Potong Video
cd /d "%~dp0"
python potong_video.py
if errorlevel 1 pause
