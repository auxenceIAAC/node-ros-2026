import os
import sys
from glob import glob

from setuptools import find_packages, setup

package_name = "gui_backend"

# The built frontend lands in gui_backend/static/ and is installed with the
# package: one process serves the API and the page, because there is no CDN in
# Namibia and no second server to run on the Jetson.
#
# THE FRONTEND MUST BE BUILT BEFORE THE WORKSPACE.
#
#     cd src/asket_gui && npm install && npm run build   # first
#     colcon build --symlink-install                     # then this
#
# This list is computed while setup.py runs, so `colcon build` can only install
# files that already exist. Running colcon first is not an error that surfaces
# anywhere — it installs an empty static directory and the backend answers 503
# with a message about npm, which reads like the frontend was never built at
# all rather than like it was built in the wrong order. That cost an hour on
# the first Jetson deployment, so it warns here, during the build, where the
# mistake is being made.
_static_root = "gui_backend/static"
static_files = [
    (
        os.path.join("share", package_name, "static", os.path.relpath(root, _static_root)),
        [os.path.join(root, f) for f in files],
    )
    for root, _, files in os.walk(_static_root)
    if files
]

if not static_files:
    sys.stderr.write(
        "\n"
        "*** gui_backend: no built frontend to install ***\n"
        f"    {os.path.abspath(_static_root)} is empty or missing.\n"
        "    The page will 503 until you build the frontend AND rebuild:\n"
        "        cd src/asket_gui && npm install && npm run build\n"
        "        colcon build --symlink-install --packages-select gui_backend\n"
        "\n"
    )

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    package_data={package_name: ["static/*", "static/assets/*"]},
    include_package_data=True,
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "config"), glob("config/*.yaml")),
        (os.path.join("share", package_name, "launch"), glob("launch/*.launch.py")),
    ]
    + static_files,
    install_requires=["setuptools", "fastapi", "uvicorn", "pyyaml"],
    zip_safe=False,
    maintainer="NODE Engineering Club",
    maintainer_email="node@example.org",
    description="FastAPI + WebSocket backend serving the Asket mission GUI.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "gui_backend_node = gui_backend.gui_backend_node:main",
        ],
    },
)
