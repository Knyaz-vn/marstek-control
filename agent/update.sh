#!/data/data/com.termux/files/usr/bin/sh
# Оновлення коду з GitHub і перезапуск
cd "$(dirname "$(readlink -f "$0")")"
git pull --ff-only && sh stop.sh && (nohup sh start.sh > /dev/null 2>&1 &) && echo "Агент оновлено і запущено"
