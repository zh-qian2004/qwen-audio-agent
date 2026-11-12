import time

from metrics import ResourceMonitor
from processors.object_detection import ObjectDetectionProcessor
from processors.person_tracking import PersonTrackingProcessor
from result_bus import ResultBus
from ros_runtime import VisionRosRuntime


class VisionService:
    def __init__(self, config, runtime_factory=VisionRosRuntime):
        self.config = config
        self.service_config = config["service"]
        processor_config = config["processors"]["object_detection"]
        tracking_config = config["processors"]["person_tracking"]
        self.started_at = time.time()
        self.bus = ResultBus(self.service_config.get("history_size", 100))
        self.detection_bus = ResultBus(self.service_config.get("history_size", 100))
        self.resources = ResourceMonitor()
        self.processor = None
        self.tracking_processor = PersonTrackingProcessor(tracking_config, self.bus)

        def on_result(raw, received_at, received_monotonic):
            if self.processor is not None:
                self.processor.handle_raw_result(raw, received_at, received_monotonic)

        self.runtime = runtime_factory(config.get("ros", {}), processor_config, on_result)
        self.processor = ObjectDetectionProcessor(
            processor_config,
            self.detection_bus,
            self.runtime,
            on_frame=self.tracking_processor.process_frame,
        )
        self.processors = {
            self.processor.processor_id: self.processor,
            self.tracking_processor.processor_id: self.tracking_processor,
        }

    def start(self):
        self.runtime.start()
        if self.tracking_processor.auto_start:
            self.tracking_processor.start()
        if self.processor.auto_start:
            self.processor.start()

    def stop(self):
        try:
            self.processor.stop()
            self.tracking_processor.stop()
        finally:
            self.runtime.stop()

    def processor_statuses(self):
        return [processor.status() for processor in self.processors.values()]

    def start_processor(self, processor_id):
        processor = self.processors.get(processor_id)
        if processor is None:
            raise KeyError(processor_id)
        if processor_id == self.processor.processor_id and self.tracking_processor.state != "running":
            self.tracking_processor.start()
        return processor.start()

    def stop_processor(self, processor_id):
        processor = self.processors.get(processor_id)
        if processor is None:
            raise KeyError(processor_id)
        status = processor.stop()
        if processor_id == self.processor.processor_id:
            self.tracking_processor.reset()
        return status

    def health(self):
        runtime = self.runtime.status()
        processors = self.processor_statuses()
        status = "ok"
        if not runtime["available"]:
            status = "error"
        elif any(item["state"] in ("degraded", "failed") for item in processors):
            status = "degraded"
        return {
            "status": status,
            "version": self.service_config.get("version", "0.1.0"),
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "ros": runtime,
            "processors": processors,
            "result_bus": self.bus.status(),
            "detection_bus": self.detection_bus.status(),
            "resources": self.resources.sample(),
        }
