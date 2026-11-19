"""Launcher for ``python my_depthai/camera_broker.py`` (dev scripts, ``deploy/build_app.sh``).

The code lives in the package, ``mydepthai.broker.camera_broker``; an installed package runs it as the
``mydepthai-broker`` console script. This file is not part of the wheel.
"""

from mydepthai.broker.camera_broker import main

if __name__ == "__main__":
    main()
