# Skript v Doplnky -> Programové doplnky: stav playlistu streamletu a okamžitá obnova.
import streamlet_core as core

if __name__ == '__main__':
    try:
        core.run_dialog()
    except Exception as exc:
        core.log('dialóg zlyhal: %r' % exc)
