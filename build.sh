#!/bin/bash
# Zabalí addon aj repo addon a vygeneruje index pre Kodi.
set -e
OUT="$HOME/kodi-repo/docs"
SOURCES=("$HOME/plugin.video.o2tv" "$HOME/kodi-repo/repository.bigtime")

rm -rf /tmp/kodibuild && mkdir -p /tmp/kodibuild
for SRC in "${SOURCES[@]}"; do
  ID=$(basename "$SRC")
  VER=$(sed -n 's/.*<addon id="'"$ID"'".*version="\([^"]*\)".*/\1/p' "$SRC/addon.xml")
  mkdir -p "$OUT/$ID"
  cp -r "$SRC" /tmp/kodibuild/$ID
  rm -rf /tmp/kodibuild/$ID/.git /tmp/kodibuild/$ID/.gitignore
  find /tmp/kodibuild/$ID -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
  (cd /tmp/kodibuild && zip -qr "$OUT/$ID/$ID-$VER.zip" $ID)
  cp "$SRC/addon.xml" "$OUT/$ID/addon.xml"
  echo "  $ID $VER"
done

python3 - "$OUT" "${SOURCES[@]}" << 'PYEOF'
import sys, re, hashlib, os
out, srcs = sys.argv[1], sys.argv[2:]
parts = [re.sub(r'^<\?xml[^>]*\?>\s*', '', open(os.path.join(s, "addon.xml")).read()).strip()
         for s in srcs]
xml = '<?xml version="1.0" encoding="UTF-8"?>\n<addons>\n' + "\n".join(parts) + '\n</addons>\n'
open(os.path.join(out, "addons.xml"), "w").write(xml)
open(os.path.join(out, "addons.xml.md5"), "w").write(hashlib.md5(xml.encode()).hexdigest() + "\n")
PYEOF
echo "index hotový"
