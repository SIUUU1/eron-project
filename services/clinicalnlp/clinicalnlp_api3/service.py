from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .http_service import ClinicalNlpHttpServer, create_http_server


class ConfigurationError(ValueError):
    """A sanitized service configuration error safe for operational reporting."""


class AssetError(RuntimeError):
    """Required local dictionary assets are unavailable or invalid."""


SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(values.get(name, str(default)))
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


def _positive_float(values: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(values.get(name, str(default)))
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{name} must be positive") from error
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return value


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name, "true" if default else "false").strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True)
class ServiceSettings:
    llm_provider: str
    ollama_api_key: str
    ollama_base_url: str
    ollama_model: str
    ollama_timeout_seconds: float
    host: str
    port: int
    request_timeout_seconds: float
    context_size: int
    clinical_max_tokens: int
    query_max_tokens: int
    query_passes: int
    translation_batch_size: int
    db_root: Path
    vector_index: Path
    policy_index: Path
    alias_db: Path
    umls_enabled: bool
    umls_timeout_seconds: float
    umls_python: Path | None
    umls_worker: Path
    umls_cache_root: Path

    @classmethod
    def from_environment(cls) -> "ServiceSettings":
        import os

        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "ServiceSettings":
        provider = values.get("CLINICAL_LLM_PROVIDER", "ollama_cloud").strip().casefold()
        if provider != "ollama_cloud":
            raise ConfigurationError("CLINICAL_LLM_PROVIDER must be ollama_cloud")
        api_key = values.get("OLLAMA_API_KEY", "").strip()
        if not api_key:
            raise ConfigurationError("OLLAMA_API_KEY is required")
        base_url = values.get("OLLAMA_BASE_URL", "https://ollama.com").strip().rstrip("/")
        if not base_url:
            raise ConfigurationError("OLLAMA_BASE_URL is required")
        model = values.get("OLLAMA_MODEL", "gemma4:31b").strip()
        if not model:
            raise ConfigurationError("OLLAMA_MODEL is required")
        try:
            port = int(values.get("CLINICALNLP_HTTP_PORT", "8000"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                "CLINICALNLP_HTTP_PORT must be between 0 and 65535"
            ) from error
        if port < 0 or port > 65535:
            raise ConfigurationError(
                "CLINICALNLP_HTTP_PORT must be between 0 and 65535"
            )
        translation_batch_size = _positive_int(
            values,
            "CLINICALNLP_TRANSLATION_BATCH_SIZE",
            3,
        )
        if translation_batch_size > 8:
            raise ConfigurationError(
                "CLINICALNLP_TRANSLATION_BATCH_SIZE must be between 1 and 8"
            )
        umls_python_value = values.get("CLINICALNLP_UMLS_PYTHON", "").strip()
        return cls(
            llm_provider=provider,
            ollama_api_key=api_key,
            ollama_base_url=base_url,
            ollama_model=model,
            ollama_timeout_seconds=_positive_float(values, "OLLAMA_TIMEOUT", 170),
            host=values.get("CLINICALNLP_HTTP_HOST", "0.0.0.0").strip()
            or "0.0.0.0",
            port=port,
            request_timeout_seconds=_positive_float(
                values,
                "CLINICALNLP_HTTP_TIMEOUT",
                180,
            ),
            context_size=_positive_int(values, "CLINICALNLP_API3_CONTEXT", 8192),
            clinical_max_tokens=_positive_int(
                values,
                "CLINICALNLP_GEMMA_MAX_TOKENS",
                3072,
            ),
            query_max_tokens=_positive_int(
                values,
                "CLINICALNLP_QUERY_EXPANSION_MAX_TOKENS",
                1536,
            ),
            query_passes=_positive_int(
                values,
                "CLINICALNLP_QUERY_EXPANSION_PASSES",
                1,
            ),
            translation_batch_size=translation_batch_size,
            db_root=Path(
                values.get(
                    "CLINICALNLP_API3_DB_ROOT",
                    str(SERVICE_ROOT / "runtime" / "medical-dictionaries"),
                )
            ),
            vector_index=Path(
                values.get(
                    "CLINICALNLP_API3_VECTOR_INDEX",
                    str(SERVICE_ROOT / "runtime" / "vectors" / "api3_vectors.sqlite"),
                )
            ),
            policy_index=Path(
                values.get(
                    "CLINICALNLP_POLICY_INDEX",
                    str(SERVICE_ROOT / "runtime" / "policy" / "policy_vectors.sqlite"),
                )
            ),
            alias_db=Path(
                values.get(
                    "CLINICALNLP_ALIAS_DB",
                    str(SERVICE_ROOT / "runtime" / "state" / "alias_feedback.sqlite"),
                )
            ),
            umls_enabled=_boolean(values, "CLINICALNLP_UMLS_ENABLED", True),
            umls_timeout_seconds=_positive_float(
                values,
                "CLINICALNLP_UMLS_TIMEOUT",
                90,
            ),
            umls_python=Path(umls_python_value) if umls_python_value else None,
            umls_worker=Path(
                values.get(
                    "CLINICALNLP_UMLS_WORKER",
                    str(SERVICE_ROOT / "scripts" / "medical_span_worker.py"),
                )
            ),
            umls_cache_root=Path(
                values.get(
                    "CLINICALNLP_UMLS_CACHE_ROOT",
                    str(SERVICE_ROOT / "runtime" / "scispacy" / "cache"),
                )
            ),
        )


@dataclass
class PreparedService:
    server: ClinicalNlpHttpServer
    runtime_bundle: Any | None = None

    def close(self) -> None:
        if self.runtime_bundle is not None:
            self.runtime_bundle.close()
        self.server.server_close()


@dataclass
class ServiceRuntimeBundle:
    runtime: Any
    span_worker: Any | None
    vector_enabled: bool

    def close(self) -> None:
        if self.span_worker is not None:
            self.span_worker.close()


def build_service_runtime(settings: ServiceSettings) -> ServiceRuntimeBundle:
    """Compose the production draft runtime from local assets and Ollama Cloud."""

    import os

    os.environ["CLINICALNLP_POLICY_INDEX"] = str(settings.policy_index)

    from .alias_feedback import (
        VersionedAliasStore,
        VersionedApprovedAliasRetriever,
    )
    from .clinical_llm import OllamaCloudClinicalLlmClient
    from .medical_span_worker import MedicalSpanWorker
    from .query_expansion import LlamaServerMedicalQueryExpander
    from .record_extractor import LlamaServerClinicalExtractor
    from .retrieval import HybridRetriever, SqliteDictionaryRetriever
    from .runtime import create_clinical_runtime
    from .umls_query_resolver import (
        UmlsPrimaryMedicalQueryResolver,
        VerifiedLocalDictionary,
    )
    from .vector_store import SqliteVectorRetriever

    try:
        lexical = SqliteDictionaryRetriever(settings.db_root)
        vector = (
            SqliteVectorRetriever(settings.vector_index)
            if settings.vector_index.is_file()
            else None
        )
        base_retriever = HybridRetriever(lexical=lexical, vector=vector)
        alias_store = VersionedAliasStore(settings.alias_db)
        retriever = VersionedApprovedAliasRetriever(base_retriever, alias_store)
    except (OSError, ValueError) as error:
        raise AssetError("ClinicalNLP dictionary assets are unavailable") from error

    clinical_client = OllamaCloudClinicalLlmClient(
        settings.ollama_base_url,
        model_name=settings.ollama_model,
        api_key=settings.ollama_api_key,
        max_output_tokens=settings.clinical_max_tokens,
        timeout=settings.ollama_timeout_seconds,
    )
    query_client = OllamaCloudClinicalLlmClient(
        settings.ollama_base_url,
        model_name=settings.ollama_model,
        api_key=settings.ollama_api_key,
        max_output_tokens=settings.query_max_tokens,
        timeout=settings.ollama_timeout_seconds,
    )
    clinical_extractor = LlamaServerClinicalExtractor(
        settings.ollama_base_url,
        model_name=settings.ollama_model,
        context_size=settings.context_size,
        max_output_tokens=settings.clinical_max_tokens,
        timeout=settings.ollama_timeout_seconds,
        llm_client=clinical_client,
    )
    query_expander = LlamaServerMedicalQueryExpander(
        settings.ollama_base_url,
        model_name=settings.ollama_model,
        context_size=settings.context_size,
        max_output_tokens=settings.query_max_tokens,
        max_passes=settings.query_passes,
        translation_batch_size=settings.translation_batch_size,
        timeout=settings.ollama_timeout_seconds,
        llm_client=query_client,
    )

    span_worker = None
    medical_query_resolver = None
    if settings.umls_enabled:
        span_worker = MedicalSpanWorker(
            SERVICE_ROOT,
            timeout_seconds=settings.umls_timeout_seconds,
            python_path=settings.umls_python,
            worker_path=settings.umls_worker,
            cache_root=settings.umls_cache_root,
        )
        verified_dictionary = VerifiedLocalDictionary(
            settings.db_root,
            raw_retriever=retriever,
            vector_index=settings.vector_index if vector is not None else None,
        )
        medical_query_resolver = UmlsPrimaryMedicalQueryResolver(
            dictionary=verified_dictionary,
            span_linker=span_worker,
        )
        span_worker.start()

    runtime = create_clinical_runtime(
        retriever=retriever,
        clinical_extractor=clinical_extractor,
        query_expander=query_expander,
        medical_query_resolver=medical_query_resolver,
    )
    return ServiceRuntimeBundle(
        runtime=runtime,
        span_worker=span_worker,
        vector_enabled=vector is not None,
    )


def _listener(values: Mapping[str, str]) -> tuple[str, int, float]:
    host = values.get("CLINICALNLP_HTTP_HOST", "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = int(values.get("CLINICALNLP_HTTP_PORT", "8000"))
    except (TypeError, ValueError):
        port = 8000
    if port < 0 or port > 65535:
        port = 8000
    try:
        timeout = float(values.get("CLINICALNLP_HTTP_TIMEOUT", "180"))
    except (TypeError, ValueError):
        timeout = 180.0
    if timeout <= 0:
        timeout = 180.0
    return host, port, timeout


def prepare_service(values: Mapping[str, str]) -> PreparedService:
    host, port, timeout = _listener(values)
    try:
        settings = ServiceSettings.from_mapping(values)
    except ConfigurationError:
        server = create_http_server(
            host,
            port,
            runtime=None,
            request_timeout_seconds=timeout,
            unavailable_reason="configuration",
        )
        return PreparedService(server=server)
    try:
        runtime_bundle = build_service_runtime(settings)
    except AssetError:
        server = create_http_server(
            host,
            port,
            runtime=None,
            request_timeout_seconds=timeout,
            unavailable_reason="assets",
        )
        return PreparedService(server=server)
    except Exception:
        server = create_http_server(
            host,
            port,
            runtime=None,
            request_timeout_seconds=timeout,
            unavailable_reason="startup",
        )
        return PreparedService(server=server)

    server = create_http_server(
        settings.host,
        settings.port,
        runtime=runtime_bundle.runtime,
        request_timeout_seconds=settings.request_timeout_seconds,
    )
    return PreparedService(server=server, runtime_bundle=runtime_bundle)


def main() -> None:
    import json
    import os

    prepared = prepare_service(os.environ)
    print(
        json.dumps(
            {
                "service": "clinicalnlp",
                "status": (
                    "ready" if prepared.runtime_bundle is not None else "unavailable"
                ),
                "host": prepared.server.server_address[0],
                "port": prepared.server.server_port,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        prepared.server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        prepared.close()


if __name__ == "__main__":
    main()
