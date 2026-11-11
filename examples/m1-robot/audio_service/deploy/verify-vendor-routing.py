"""Live audio-only routing checks; never invokes robot motion interfaces."""
import asyncio
import json
import threading
import time
import urllib.request

import rclpy
from rclpy.context import Context
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String

BASE = "http://127.0.0.1:8091"


def status(path):
    with urllib.request.urlopen(BASE + path, timeout=3) as response:
        return json.load(response)


async def main():
    contexts = []
    nodes = []
    executors = []
    threads = []
    private_prompts = []
    try:
        for domain in (0, 43):
            context = Context()
            rclpy.init(context=context, domain_id=domain)
            contexts.append(context)
            node = Node(f"vendor_routing_check_{domain}", context=context,
                        start_parameter_services=False)
            nodes.append(node)
            executor = MultiThreadedExecutor(num_threads=1, context=context)
            executor.add_node(node)
            executors.append(executor)
            thread = threading.Thread(target=executor.spin, daemon=True)
            thread.start()
            threads.append(thread)
        nodes[1].create_subscription(String, "/audio/speaker_pcm_interrupt",
                                     lambda msg: private_prompts.append(msg.data), 10)
        publisher = nodes[0].create_publisher(String, "/audio/speaker_pcm_interrupt", 10)
        await asyncio.sleep(3)
        before = status("/api/vendor/events/status")
        assert before["agent"]["active"], before
        message = String()
        message.data = "0014"
        publisher.publish(message)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            after = status("/api/vendor/events/status")
            if after["stats"]["received"] > before["stats"]["received"] and after["pending"] == 0:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
        assert after["stats"]["ignored"] == before["stats"]["ignored"] + 1, after
        assert private_prompts == [], private_prompts
        assert status("/api/audio/status")["online"], "capture offline"
        print(json.dumps({"test": "online_vendor_suppression", "passed": True,
                          "decision": after["recent_events"][0],
                          "private_prompt_count": len(private_prompts)}, ensure_ascii=False))

        # Briefly advertise an unready Agent to check the deployed fallback,
        # then let the primary Harness reconnect automatically.
        import websockets
        async with websockets.connect("ws://127.0.0.1:8091/ws/vendor/events") as websocket:
            await websocket.send(json.dumps({"type": "agent_hello", "instance_id": "routing-check",
                                             "ready": False}))
            assert json.loads(await websocket.recv())["type"] == "vendor_event_ready"
            previous_count = len(private_prompts)
            publisher.publish(message)
            deadline = time.monotonic() + 3
            while len(private_prompts) == previous_count and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            fallback = status("/api/vendor/events/status")
            assert len(private_prompts) == previous_count + 1, private_prompts
            assert fallback["recent_events"][0]["decision"] == "play_vendor", fallback
            print(json.dumps({"test": "unready_agent_fallback", "passed": True,
                              "decision": fallback["recent_events"][0],
                              "private_prompt_count": len(private_prompts)}, ensure_ascii=False))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            resumed = status("/api/vendor/events/status")["agent"]
            if resumed["active"] and resumed["instance_id"] == "m1-robot-primary":
                break
            await asyncio.sleep(0.2)
        assert resumed["active"] and resumed["instance_id"] == "m1-robot-primary", resumed

        # The vendor's read-only query tests the real service round trip.
        from video_ros_msgs.srv import VolumeControl
        client = nodes[0].create_client(VolumeControl, "/speaker_volume_control")
        deadline = time.monotonic() + 4
        while not client.service_is_ready() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert client.service_is_ready(), "volume proxy unavailable"
        request = VolumeControl.Request()
        request.command = "get_volume"
        future = client.call_async(request)
        deadline = time.monotonic() + 5
        while not future.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert future.done(), "volume proxy did not reply"
        response = future.result()
        bridge = status("/api/health")["media_audio_bridge"]
        assert bridge["services"]["/speaker_volume_control"]["completed"] >= 1, bridge
        print(json.dumps({"test": "volume_service_round_trip", "passed": True,
                          "success": response.success, "message": response.message}, ensure_ascii=False))

        # Exercise the real output WS with silence, preserving the user's volume.
        async with websockets.connect("ws://127.0.0.1:8091/ws/audio/output") as websocket:
            await websocket.send(json.dumps({"type": "audio_start", "stream_id": "vendor-routing-check",
                                             "response_id": "vendor-routing-verification",
                                             "encoding": "pcm_24000_16_1", "sample_rate": 24000,
                                             "channels": 1, "sample_width": 2}))
            events = []
            ready = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
            events.append(ready)
            assert ready["type"] == "output_ready", ready
            await websocket.send(bytes(12000))
            await websocket.send(json.dumps({"type": "audio_end"}))
            while events[-1]["type"] != "playback_ended":
                events.append(json.loads(await asyncio.wait_for(websocket.recv(), timeout=10)))
            assert any(item["type"] == "playback_started" for item in events), events
            print(json.dumps({"test": "silent_output", "passed": True, "events": events}, ensure_ascii=False))
    finally:
        for executor in executors:
            executor.shutdown(timeout_sec=2)
        for thread in threads:
            thread.join(timeout=2)
        for node in nodes:
            node.destroy_node()
        for context in contexts:
            context.try_shutdown()


asyncio.run(main())
