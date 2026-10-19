"""统一 stdlib logging + JSON formatter(§10 结构化日志;全仓不混用两套日志体系)。"""

import json
import logging


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key in ("request_id", "task_id", "kb_id", "trace_id"):
            v = getattr(record, key, None)
            if v:
                payload[key] = v
        # 遥测指标走独立的 metric 出口(见 infra/metering.py):与临时加的日志字段分开,
        # 日志 schema 才能稳定到可以被下游按字段聚合
        metric = getattr(record, "metric", None)
        if metric:
            payload["metric"] = metric
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
