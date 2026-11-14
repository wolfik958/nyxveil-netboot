#!/bin/bash
# Installs/refreshes the Nyxveil theme into /config/menus on every container start and writes the
# Nyxveil block into local-vars.ipxe. Anything you add to local-vars.ipxe outside the block is kept.
set -e
NGINX_PORT=${NGINX_PORT:-80}
NYX_HOST=${NYX_HOST:-'${next-server}'}          # iPXE fills in the TFTP server = this container
NYX_THEME=${NYX_THEME:-1}
NYX_OUT_DIR=${NYX_OUT_DIR:-/assets/nyxveil}
NYX_PORT=${NYX_PORT:-$NGINX_PORT}                # port clients reach nginx on (differs if you map e.g. 8080:80)
PORTSUFFIX=$([ "$NYX_PORT" = 80 ] && echo "" || echo ":$NYX_PORT")
OUT_URL=${NYX_OUT_URL:-http://${NYX_HOST}${PORTSUFFIX}/${NYX_OUT_DIR#/assets/}}

mkdir -p /config/menus/nyxveil /config/menus/local "$NYX_OUT_DIR"
cp -f /opt/nyxveil/menus/nyxveil/* /config/menus/nyxveil/

block() {
  echo '# ---- Nyxveil (managed by the nyxveil-netboot image; edits inside this block are overwritten) ----'
  echo "set custom_url ${OUT_URL}"
  if [ "$NYX_THEME" = 1 ]; then cat <<'IPXE'
iseq ${platform} efi && iseq ${buildarch} x86_64 && goto nyx_efi ||
goto nyx_end
:nyx_efi
isset ${nyx_front} && goto nyx_theme ||
set nyx_front 1
chain --autofree tftp://${tftp-server}/nyxveil/nyxveil.efi ||
iseq ${efi/NyxveilPick:string} isos && set nyx_isos 1 ||
:nyx_theme
chain tftp://${tftp-server}/nyxveil/theme.ipxe ||
isset ${nyx_isos} && chain ${custom_url}/custom.ipxe ||
clear nyx_isos
:nyx_end
IPXE
  fi
  echo '# ---- end Nyxveil ----'
}

# the web UI edits menus/local/ and copies it over menus/, so local/ is the source when it exists
src=/config/menus/local/local-vars.ipxe
[ -f "$src" ] || src=/config/menus/local-vars.ipxe
tmp=$(mktemp)
if [ -f "$src" ]; then
  # drop an older Nyxveil block (managed or hand-pasted) and stray custom_url lines, keep the rest
  awk '/^# ---- Nyxveil/{skip=1} !skip && !/^set custom_url /{print} /^# ---- end Nyxveil ----/{skip=0}' "$src" \
    | grep -v '^#!ipxe' > "$tmp.rest" || true
else
  : > "$tmp.rest"
fi
{ echo '#!ipxe'; block; cat "$tmp.rest"; } > "$tmp"
cp "$tmp" /config/menus/local/local-vars.ipxe
mv "$tmp" /config/menus/local-vars.ipxe
rm -f "$tmp.rest"
chown -R nbxyz:nbxyz /config/menus/nyxveil /config/menus/local-vars.ipxe /config/menus/local
echo "[nyxveil] theme $( [ "$NYX_THEME" = 1 ] && echo on || echo off ), ISO library at ${OUT_URL}"
