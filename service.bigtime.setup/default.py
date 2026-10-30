# Skript v Doplnky -> Programové doplnky: stav záplat a IPTV Simple s možnosťou
# okamžitej kontroly a opravy.
import bigtime_core as core

if __name__ == '__main__':
    try:
        core.run_dialog()
    except Exception as exc:
        core.log('dialóg zlyhal: %r' % exc)
