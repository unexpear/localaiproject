"""Application entry point.

Prefers the PySide6 UI; falls back to the legacy tkinter window if Qt isn't
installed; finally falls back to the CLI loop if no GUI is available at all.
"""
import sys


def _run_qt():
    from app.ui.qt_main_window import run_qt_app
    return run_qt_app()


def _run_tkinter():
    from app.ui.main_window import main as tk_main
    tk_main()
    return 0


def main() -> int:
    try:
        return _run_qt()
    except ImportError as exc:
        print(f"[ui] Qt unavailable ({exc}); falling back to tkinter window.")
    except Exception as exc:  # noqa: BLE001
        print(f"[ui] Qt window failed to start ({exc}); falling back to tkinter window.")

    try:
        return _run_tkinter()
    except Exception as exc:  # noqa: BLE001
        print(f"[ui] tkinter unavailable ({exc}); falling back to CLI.")
        from app.ui.main_window import _run_cli
        _run_cli()
        return 0


if __name__ == "__main__":
    sys.exit(main())
