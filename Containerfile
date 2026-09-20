FROM docker.io/ros:jazzy-ros-base AS base

RUN apt-get update && apt-get install -y \
    libgtk-3-0 \
    libglib2.0-0 \
    libgl1 \
    pkg-config \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

RUN apt-get update && apt-get install -y --no-install-recommends \
    # MAVROS2 + MAVLink
    ros-jazzy-mavros \
    ros-jazzy-mavros-extras \
    ros-jazzy-mavros-msgs \
    ros-jazzy-geographic-msgs \
    ros-jazzy-tf2-ros \
    ros-jazzy-tf2-geometry-msgs \
    # Sensors
    v4l-utils \
    ros-jazzy-cv-bridge \
    # Vision / Perception
    libgl1 \
    ros-jazzy-vision-msgs \
    # Localization
    ros-jazzy-robot-localization \
    # Navigation
    ros-jazzy-nav2-bringup \
    # Control / Mission
    ros-jazzy-nav2-msgs \
    ros-jazzy-twist-mux \
    # Custom message generation
    ros-jazzy-rosidl-default-generators \
    # Telemetry bridge
    # ros-jazzy-rosbridge-suite \
    # ros-jazzy-web-video-server \
    ros-jazzy-foxglove-bridge \
    ros-jazzy-ros-gz-bridge \
    # Camera calibration
    ros-jazzy-camera-calibration-parsers \
    ros-jazzy-camera-info-manager \
    ros-jazzy-camera-info-manager-py \
    ros-jazzy-launch-testing-ament-cmake \
    ros-jazzy-camera-calibration \
    # LiDAR-camera extrinsic calibration
    ros-jazzy-laser-geometry \
    python3-scipy \
    python3-pip \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

RUN usermod -aG dialout root

# Python packages with no rosdep keys, needed wherever this workspace RUNS.
#
# These are not optional extras: gui_backend is a FastAPI application and will
# not import without them, so a container built without them starts the launch
# file and then fails the one node the GUI exists for. They were previously
# only in the devcontainer's postCreateCommand, which Podman never runs, so
# every `podman build` produced an image that could not serve the GUI and said
# so only at launch time.
RUN pip install --break-system-packages --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    websockets \
    pyserial \
    pyyaml

# --- Dev target: workspace is bind-mounted, used by devcontainer ---------------
FROM base AS dev

# Node, for building the GUI frontend.
#
# Ubuntu's `nodejs` is 18 and Vite asks for 20+; it builds anyway but warns,
# and warnings that are always there stop being read. NodeSource 20 is pinned
# so the container is usable as built rather than after a manual install that
# vanishes on exit.
#
#     cd src/asket_gui && npm install && npm run build   # BEFORE colcon build
#
# That order matters: gui_backend/setup.py collects the built frontend while it
# runs, so building the workspace first installs an empty static directory.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# The rest of the devcontainer's postCreateCommand, which Podman never runs.
RUN pip install --break-system-packages --no-cache-dir \
    "numpy<2" \
    onnxruntime \
    opencv-python-headless \
    rplidar-roboticia \
    simple-pid \
    scikit-learn

RUN echo "source /opt/ros/jazzy/setup.bash" >> /etc/bash.bashrc && \
    echo '[ -f /workspace/install/setup.bash ] && source /workspace/install/setup.bash' >> /etc/bash.bashrc
WORKDIR /workspace

# --- Prod target: code baked in, built with colcon ----------------------------
FROM base AS prod

# GeographicLib datasets required by MAVROS GPS plugins
RUN wget -q https://raw.githubusercontent.com/mavlink/mavros/ros2/mavros/scripts/install_geographiclib_datasets.sh \
    && chmod +x install_geographiclib_datasets.sh \
    && ./install_geographiclib_datasets.sh \
    && rm install_geographiclib_datasets.sh

# pip-only deps (no rosdep keys exist for these)
RUN pip install --break-system-packages --no-cache-dir \
     "numpy<2" \
     onnxruntime \
     opencv-python \
     rplidar-roboticia \
     transforms3d

COPY src/ /ros2_ws/src/

RUN . /opt/ros/jazzy/setup.sh && \
    colcon build \
        --base-paths /ros2_ws/src \
        --install-base /opt/njord \
        --cmake-args -DCMAKE_BUILD_TYPE=Release

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["ros2", "launch", "bringup", "njord.launch.py"]
