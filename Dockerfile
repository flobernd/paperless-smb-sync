FROM debian:trixie-slim

RUN apt-get update \
    && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends \
        gettext-base \
        python3 \
        python3-requests \
        samba \
        smbclient \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY src/smb_sync /opt/smb-sync/smb_sync
COPY docker/smb-sync /usr/local/bin/smb-sync
COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/smb.conf.template /etc/samba/smb.conf.template
RUN chmod +x /usr/local/bin/smb-sync /entrypoint.sh

EXPOSE 445
VOLUME /var/lib/samba

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD ["smb-sync", "health"]

ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
