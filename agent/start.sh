#!/data/data/com.termux/files/usr/bin/sh
# Запуск агента з автоперезапуском. Тримає Android від засинання.
termux-wake-lock 2>/dev/null
cd "$(dirname "$(readlink -f "$0")")"
pkill -f "[p]ython agent.py" 2>/dev/null
while true; do
  python agent.py > /dev/null 2>> agent.err
  sleep 15
done
