import sys
import os
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from chart_viewer.run_demo import main as demo_main
import chart_viewer.run_demo as demo_module

def take_shot():
    print("Taking screenshot...")
    app = QApplication.instance()
    for w in app.topLevelWidgets():
        if w.isVisible():
            p = w.grab()
            p.save("/home/daniel/QJM/dsh_playground/screenshot_local.png")
            print("Saved to /home/daniel/QJM/dsh_playground/screenshot_local.png")
    app.quit()

# Monkeypatch sys.exit so demo_main doesn't kill our script
_exit = sys.exit
sys.exit = lambda x: None

app = QApplication(sys.argv)
# hook timer BEFORE starting demo, because demo_main calls app.exec()
QTimer.singleShot(2000, take_shot)

# run demo
demo_main()
