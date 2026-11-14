#!/bin/bash
# Nyxveil netboot - container entrypoint: stock netboot.xyz init, Nyxveil install, then supervisord
# (nginx + TFTP + web UI from netboot.xyz, plus the Nyxveil ISO watcher).
/init.sh
/opt/nyxveil/install.sh
echo "[nyxveil] starting supervisord (netboot.xyz services + ISO watcher)"
exec supervisord -c /etc/supervisor.conf
