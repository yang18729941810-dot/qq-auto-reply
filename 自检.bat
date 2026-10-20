@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  自检：QQ 版本 / 窗口 / 控件识别 / 配置
echo ============================================
python auto_reply.py doctor
echo.
pause
