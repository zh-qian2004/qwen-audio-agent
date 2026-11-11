"""Read the private media domain graph without invoking robot interfaces."""
import json
import time

import rclpy
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node

context = Context()
rclpy.init(context=context, domain_id=43)
node = Node("media_graph_inspector", context=context, start_parameter_services=False)
executor = SingleThreadedExecutor(context=context)
executor.add_node(node)
try:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        executor.spin_once(timeout_sec=0.1)
    result = []
    for name, namespace in node.get_node_names_and_namespaces():
        if name in ("media_graph_inspector", "audio_service_media_bridge"):
            continue
        result.append({
            "node": name,
            "namespace": namespace,
            "subscriptions": node.get_subscriber_names_and_types_by_node(name, namespace),
            "publishers": node.get_publisher_names_and_types_by_node(name, namespace),
            "services": node.get_service_names_and_types_by_node(name, namespace),
        })
    print(json.dumps(result, ensure_ascii=False, indent=2))
finally:
    executor.shutdown()
    node.destroy_node()
    context.try_shutdown()
