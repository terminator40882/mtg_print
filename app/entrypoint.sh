#!/bin/sh
set -e

mkdir -p /data

# CUPS nur lokal im Container betreiben (kein Sharing, kein Browsing)
cupsctl --no-share-printers --no-remote-any --no-remote-admin >/dev/null 2>&1 || true
if ! lpstat -r >/dev/null 2>&1; then
  cupsd
fi
i=0
until lpstat -r >/dev/null 2>&1 || [ $i -ge 40 ]; do
  i=$((i + 1))
  sleep 0.25
done

if [ -n "$PRINTER_URI" ]; then
  echo "Drucker: $PRINTER_URI"
elif [ -n "$PRINTER_IP" ]; then
  echo "Drucker: ipp://$PRINTER_IP:631/ipp/print"
else
  echo "WARNUNG: PRINTER_IP ist nicht gesetzt – PDF-Erzeugung funktioniert, Drucken nicht."
fi

exec uvicorn mtgprint.main:app --host 0.0.0.0 --port "${APP_INTERNAL_PORT:-8080}" --proxy-headers
