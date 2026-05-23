# Pre-baked FRR + snmpd image. The upstream frrouting/frr image is
# alpine-based; we just `apk add net-snmp` once at build time so each
# cabinet boots without that step. wrapper.sh (rendered by tools/render)
# still launches snmpd in the background before exec'ing docker-start.
FROM quay.io/frrouting/frr:10.6.1

RUN apk add --no-cache --quiet net-snmp
