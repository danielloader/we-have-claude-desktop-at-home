from ..config import Settings
from .base import Telemetry
from .noop import NoopTelemetry


def build_telemetry(settings: Settings) -> Telemetry:
    match settings.telemetry:
        case "none":
            return NoopTelemetry()
        case "file":
            from .file import FileTelemetry

            return FileTelemetry(settings.telemetry_file, fmt=settings.telemetry_file_format)
        case "elastic_apm":
            from .elastic_apm import ElasticApmTelemetry

            return ElasticApmTelemetry(
                server_url=settings.apm_server_url,
                service_name=settings.apm_service_name,
                environment=settings.apm_environment,
                secret_token=settings.apm_secret_token,
                api_key=settings.apm_api_key,
            )
    raise ValueError(f"unknown telemetry backend {settings.telemetry!r}")
