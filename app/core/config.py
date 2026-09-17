from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "text-to-sql"
    debug: bool = False

    ritm_samples_path: Path = PROJECT_ROOT / "ritm_samples.json"

    db_server: str
    db_name: str
    db_driver: str = "ODBC Driver 18 for SQL Server"
    db_encrypt: bool = True
    db_trust_server_certificate: bool = False

    ollama_base_url: str = "http://localhost:11434"
    ollama_request_timeout: float = 120.0

    extraction_prompt_version: str = "v3"

    catalog_db_server: str = "localhost"
    catalog_db_port: int = 5435
    catalog_db_name: str = "schema_catalog"
    catalog_db_user: str = "catalog"
    catalog_db_password: str = "catalog"

    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768

    retrieval_keyword_weight: float = 0.5
    retrieval_semantic_weight: float = 0.5
    retrieval_top_k: int = 5

    catalog_entity_top_k: int = 3
    catalog_entity_min_score: float = 0.3
    catalog_column_auto_accept_score: float = 85.0
    catalog_column_min_accept_score: float = 60.0
    catalog_column_llm_trigger_score: float = 40.0
    catalog_fallback_search_top_k: int = 3

    catalog_field_table_top_k: int = 3
    catalog_field_table_min_score: float = 0.3
    catalog_field_table_promote_min_hits: int = 2
    catalog_field_table_promote_min_score: float = 0.6

    catalog_join_path_cap: int = 5

    @property
    def catalog_database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.catalog_db_user}:{self.catalog_db_password}"
            f"@{self.catalog_db_server}:{self.catalog_db_port}/{self.catalog_db_name}"
        )

    @property
    def database_url(self) -> str:
        driver = self.db_driver.replace(" ", "+")
        encrypt = "yes" if self.db_encrypt else "no"
        trust_cert = "yes" if self.db_trust_server_certificate else "no"
        return (
            f"mssql+pyodbc://@{self.db_server}/{self.db_name}"
            f"?driver={driver}&Encrypt={encrypt}&TrustServerCertificate={trust_cert}"
            f"&trusted_connection=yes"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
