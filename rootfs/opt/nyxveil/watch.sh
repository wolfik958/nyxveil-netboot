#!/bin/bash
# supervisord program: the Nyxveil ISO watcher, configured from the container environment
NGINX_PORT=${NGINX_PORT:-80}
NYX_HOST=${NYX_HOST:-'${next-server}'}
NYX_PORT=${NYX_PORT:-$NGINX_PORT}                # port clients reach nginx on (differs if you map e.g. 8080:80)
PORTSUFFIX=$([ "$NYX_PORT" = 80 ] && echo "" || echo ":$NYX_PORT")
export NYX_ISO_DIR=${NYX_ISO_DIR:-/assets/iso}
export NYX_OUT_DIR=${NYX_OUT_DIR:-/assets/nyxveil}
export NYX_HTTP=${NYX_HTTP:-http://${NYX_HOST}${PORTSUFFIX}/${NYX_ISO_DIR#/assets/}}
export NYX_OUT_URL=${NYX_OUT_URL:-http://${NYX_HOST}${PORTSUFFIX}/${NYX_OUT_DIR#/assets/}}
export NYX_CHECK_URL=${NYX_CHECK_URL:-http://127.0.0.1:${NGINX_PORT}/${NYX_OUT_DIR#/assets/}}
mkdir -p "$NYX_ISO_DIR" "$NYX_OUT_DIR"
exec python3 -u /opt/nyxveil/isolib/nyxveil-watch.py
