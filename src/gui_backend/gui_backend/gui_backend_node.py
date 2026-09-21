"""ROS 2 node: serve the mission GUI.

Runs the same FastAPI application as ``python3 -m gui_backend.core.app``, with a
``RosSource`` instead of a ``SimSource``. Everything above the source — the hub,
the payloads, the alarms, the command lifecycle, the frontend — is identical, so
what is exercised in sim is what runs in the field.

uvicorn runs in its own thread. ``rclpy`` spins on the main thread. The two meet
only through ``RosSource``, whose buffers are written by ROS callbacks and read
by the hub, both of them just replacing or reading a reference.
"""

from __future__ import annotations

import threading
from pathlib import Path

import rclpy
import uvicorn
import yaml
from asket_interfaces.msg import LinkStatus
from rclpy.node import Node

from gui_backend.core.hub import Hub
from gui_backend.core.link_profile import ProfileSelector, ProfileThresholds
from gui_backend.core.ros_source import RosSource
from gui_backend.core.server import create_app

DEFAULT_STATIC = Path(__file__).resolve().parent / "static"


class GuiBackendNode(Node):
    def __init__(self) -> None:
        super().__init__("gui_backend")

        # 0.0.0.0: the point of this server is being reachable from a laptop
        # on the beach. Bound to localhost it would be a GUI nobody can open.
        self.declare_parameter("host", "0.0.0.0")
        # 8090 stays clear of foxglove_bridge (8765) and of the rosbridge (9090)
        # and web_video_server (8080) entries in njord.launch.py.
        self.declare_parameter("port", 8090)
        self.declare_parameter("topics_config", "")
        self.declare_parameter("link_profiles_config", "")
        self.declare_parameter("tiles_path", "/data/maps/survey.mbtiles")
        # Where downloaded tiles are kept. One SQLite file, so it can be copied
        # to another machine or kept between deployments — a cache from a
        # previous survey is a basemap that is already there at first boot.
        self.declare_parameter("tile_cache_path", "/data/maps/tile-cache.sqlite")
        # Set false to restore the pre-internet behaviour: offline file and
        # coordinate grid only, nothing fetched. This is the switch to use if a
        # tile provider ever asks us to stop.
        self.declare_parameter("online_tiles", True)
        self.declare_parameter("static_dir", str(DEFAULT_STATIC))
        self.declare_parameter("tick_hz", 20.0)

        topics = _load_yaml(self.get_parameter("topics_config").value)
        profiles = _load_yaml(self.get_parameter("link_profiles_config").value)

        self.source = RosSource(self, topics)

        thresholds = ProfileThresholds(**(profiles.get("thresholds") or {}))
        self.hub = Hub(
            self.source,
            tick_hz=float(self.get_parameter("tick_hz").value),
            selector=ProfileSelector(thresholds),
        )

        self.pub_link = self.create_publisher(LinkStatus, "/gui/link_status", 10)
        self.create_timer(1.0, self._publish_link_status)

        app = create_app(
            self.hub,
            static_dir=self.get_parameter("static_dir").value,
            tiles_path=self.get_parameter("tiles_path").value or None,
            tile_cache_path=self.get_parameter("tile_cache_path").value or None,
            online_tiles=bool(self.get_parameter("online_tiles").value),
            ping_interval_s=float(profiles.get("ping_interval_s", 2.0)),
        )

        host = self.get_parameter("host").value
        port = int(self.get_parameter("port").value)
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, name="gui-backend-http", daemon=True
        )
        self._thread.start()

        static = Path(self.get_parameter("static_dir").value)
        if not (static / "index.html").is_file():
            self.get_logger().warn(
                f"no built frontend at {static} — the API will work but the page "
                "will not. Build it with: cd src/asket_gui && npm install && npm run build"
            )
        self.get_logger().info(f"mission GUI on http://{host}:{port}")

    def _publish_link_status(self) -> None:
        """Publish what the backend knows about the shore link.

        The backend is the only thing that measures it — it is the only part of
        the system with a client on the other end — so it publishes rather than
        subscribes, and the recorder logs it alongside everything else.
        """
        clients = list(self.hub.clients.values())
        rtts = [c.rtt_ms for c in clients if c.rtt_ms is not None]

        msg = LinkStatus()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.active_link = "wifi" if clients else "none"
        msg.quality = 1.0 if clients else 0.0
        msg.rtt_ms = float(min(rtts)) if rtts else 0.0
        msg.rate_bytes_per_s = float(sum(c.bytes_estimate_per_s for c in clients))
        msg.profile = self.hub.selector.profile
        msg.profile_manual = self.hub.selector.manual
        msg.connected_clients = len(clients)
        self.pub_link.publish(msg)

        # Feed the measurement back so automatic profile selection runs on what
        # the operator's laptop actually experiences.
        self.source.set_link_measurement(
            msg.active_link, msg.quality, msg.rtt_ms, 800_000.0
        )

    def destroy_node(self) -> bool:
        self._server.should_exit = True
        self._thread.join(timeout=3.0)
        return super().destroy_node()


def _load_yaml(path: str) -> dict:
    if not path:
        return {}
    file = Path(path)
    if not file.is_file():
        return {}
    return yaml.safe_load(file.read_text()) or {}


def main(args=None) -> None:
    rclpy.init(args=args)
    node = GuiBackendNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
