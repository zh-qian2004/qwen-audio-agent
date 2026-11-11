import time

from metrics import ResourceMonitor
from processors.object_detection import ObjectDetectionProcessor
from result_bus import ResultBus
from ros_runtime import VisionRosRuntime


class VisionService:
    def __init__(self, config, runtime_factory=VisionRosRuntime):
        self.config = config
        self.service_config = config["service"]
        processor_config = config["processors"]["object_detection"]
        self.started_at = time.time()
        self.bus = ResultBus(self.service_config.get("history_size", 100))
        self.resources = ResourceMonitor()
        self.processor = None

        def on_result(raw, received_at, received_monotonic):
            if self.processor is not None:
                self.processor.handle_raw_result(raw, received_at, received_monotonic)

        self.runtime = runtime_factory(config.get("ros", {}), processor_config, on_result)
        self.processor = ObjectDetectionProcessor(processor_config, self.bus, self.runtime)

    def start(self):
        self.runtime.start()
        if self.processor.auto_start:
            self.processor.start()

    def stop(self):
        try:
            self.processor.stop()
        finally:
            self.runtime.stop()

    def processor_statuses(self):
        return [self.processor.status()]

    def start_processor(self, processor_id):
        if processor_id != self.processor.processor_id:
            raise KeyError(processor_id)
        return self.processor.start()

    def stop_processor(self, processor_id):
        if processor_id != self.processor.processor_id:
            raise KeyError(processor_id)
        return self.processor.stop()

    def health(self):
        runtime = self.runtime.status()
        processor = self.processor.status()
        status = "ok"
        if not runtime["available"]:
            status = "error"
        elif processor["state"] in ("degraded", "failed"):
            status = "degraded"
        return {
            "status": status,
            "version": self.service_config.get("version", "0.1.0"),
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "ros": runtime,
            "processors": [processor],
            "result_bus": self.bus.status(),
            "resources": self.resources.sample(),
        }
