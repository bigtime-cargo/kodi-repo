#!/bin/bash
# Vygeneruje Kodi repozitár zo zdrojov addonu.
set -e
SRC="$HOME/plugin.video.o2tv"
OUT="$HOME/kodi-repo/docs"
ID="plugin.video.o2tv"
VER=$(sed -n "s/.*<addon id=\"$ID\".*version=\"\([^\"]*\)\".*/\1/p" "$SRC/addon.xml")

mkdir -p "$OUT/$ID"
rm -rf /tmp/kodibuild && mkdir -p /tmp/kodibuild
cp -r "$SRC" /tmp/kodibuild/$ID
rm -rf /tmp/kodibuild/$ID/.git /tmp/kodibuild/$ID/.gitignore
find /tmp/kodibuild -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
(cd /tmp/kodibuild && zip -qr "$OUT/$ID/$ID-$VER.zip" $ID)
cp "$SRC/addon.xml" "$OUT/$ID/addon.xml"

# index pre Kodi
{
  echo '<?xml version="1.0" encoding="UTF-8"?>'
  echo '<addons>'
  sed '1d' "$SRC/addon.xml"
  echo '</addons>'
} > "$OUT/addons.xml"
(cd "$OUT" && md5sum addons.xml | cut -d' ' -f1 > addons.xml.md5)

echo "hotovo: $ID $VER"
