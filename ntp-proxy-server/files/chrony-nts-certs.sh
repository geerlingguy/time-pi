#!/bin/sh
# certbot deploy hook: chronyd reads its NTS certificate as _chrony, and only at startup.
set -e
install -d -o root -g _chrony -m 0750 /etc/chrony/nts
install -o root -g _chrony -m 0640 "$RENEWED_LINEAGE/fullchain.pem" "$RENEWED_LINEAGE/privkey.pem" /etc/chrony/nts/
systemctl restart chrony
