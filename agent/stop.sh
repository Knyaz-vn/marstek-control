#!/data/data/com.termux/files/usr/bin/sh
pkill -f "[s]tart.sh"
pkill -f "[p]ython agent.py"
termux-wake-unlock 2>/dev/null
echo "Агент зупинено"
