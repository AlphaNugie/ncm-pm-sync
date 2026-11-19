@echo off
rem 定时任务入口：把私信里的分享歌曲/专辑拉进目标歌单
rem 用法：双击运行，或挂到 Windows 任务计划程序（建议每 15 分钟一次）
cd /d "%~dp0"
python ncm_sync.py sync >> sync.log 2>&1
