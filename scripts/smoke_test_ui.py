"""Start the installed application briefly without a physical display."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from photo_organizer.ui import main_window


class SmokeWindow(main_window.MainWindow):
    def show(self) -> None:
        super().show()
        QTimer.singleShot(1000, self.finish_check)
        QTimer.singleShot(10000, lambda: QApplication.instance().exit(1))

    def finish_check(self) -> None:
        visible = self.isVisible()
        print(json.dumps({"window_visible": visible, "qt_platform": QApplication.platformName()}))
        self.close()
        QApplication.instance().exit(0 if visible else 1)


if __name__ == "__main__":
    main_window.MainWindow = SmokeWindow
    main_window.main()
