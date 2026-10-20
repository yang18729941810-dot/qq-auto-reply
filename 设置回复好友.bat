@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  选择「可以自动回复」的 QQ 好友
echo  按编号勾选，直接回车表示不改
echo ============================================
python auto_reply.py pick
echo.
pause
