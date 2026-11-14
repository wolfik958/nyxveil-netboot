# Nyxveil netboot: netboot.xyz + the Nyxveil theme (GRUB front menu, themed iPXE menus) + an ISO
# library that turns ISOs dropped into a folder into network-bootable menu entries.
ARG NETBOOTXYZ_IMAGE=docker.io/netbootxyz/netbootxyz:latest
FROM ${NETBOOTXYZ_IMAGE}

# 7z unpacks Windows install discs (their file tree is UDF); python3 already ships with netboot.xyz
RUN apk add --no-cache 7zip

COPY rootfs/ /
RUN cat /opt/nyxveil/supervisor-nyxveil.conf >> /etc/supervisor.conf \
 && chmod +x /opt/nyxveil/*.sh /opt/nyxveil/isolib/*.py

ENV NYX_ISO_DIR=/assets/iso \
    NYX_OUT_DIR=/assets/nyxveil \
    NYX_THEME=1 \
    NYX_SETTLE=30

LABEL org.opencontainers.image.title="nyxveil-netboot" \
      org.opencontainers.image.description="netboot.xyz with the Nyxveil theme and a drag-and-drop ISO library" \
      org.opencontainers.image.source="https://github.com/wolfik958/nyxveil-netboot"

CMD ["/opt/nyxveil/start.sh"]
