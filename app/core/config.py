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
    approved_ritms_path: Path = PROJECT_ROOT / "approved_ritms.json"

    db_server: str
    db_name: str
    db_driver: str = "ODBC Driver 18 for SQL Server"
    db_encrypt: bool = True
    db_trust_server_certificate: bool = False

    # The model behind the extract, map and write stages: DeepSeek's OpenAI-format chat API, JSON mode.
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"  # or "deepseek-v4-pro"
    llm_max_tokens: int = 8192
    llm_timeout: float = 300.0
    llm_max_retries: int = 2  # extra attempts on 429 / 5xx / timeouts
    # Thinking is off: JSON mode together with thinking is undocumented, and temperature has no effect while it is
    # on. With it off the answers are made repeatable by a fixed temperature of 0.
    llm_temperature: float = 0.0
    llm_thinking: bool = False
    llm_reasoning_effort: str | None = None  # low | high | max, only with LLM_THINKING=true

    extract_prompt_version: str = "extract-v1"
    mapping_prompt_version: str = "mapping-v1"
    write_prompt_version: str = "write-v1"
    # Ask the source database to compile each written query (no execution, no rows read) so the model can be shown
    # SQL Server's own errors. A server that cannot be reached skips the check instead of failing the request.
    write_compile_check: bool = True

    # Redact emails / long digit runs from the RITM `description` before it leaves the network boundary.
    redact_ritm_description: bool = True

    catalog_db_server: str = "localhost"
    catalog_db_port: int = 5435
    catalog_db_name: str = "schema_catalog"
    catalog_db_user: str = "catalog"
    catalog_db_password: str = "catalog"

    # Embeddings come from Voyage AI (DeepSeek has no embeddings API).
    # Changing the model or dimension requires rebuilding the catalog tables (see the pgvector column size).
    voyage_api_key: str | None = None
    voyage_base_url: str = "https://api.voyageai.com/v1"
    voyage_request_timeout: float = 60.0
    embedding_model: str = "voyage-code-4"
    embedding_dim: int = 1024

    # Table search (retrieval_service): each table scores keyword_weight * keyword match + semantic_weight * embedding match.
    retrieval_keyword_weight: float = 0.5
    retrieval_semantic_weight: float = 0.5
    retrieval_top_k: int = 5  # only the default for a caller that passes no top_k; GET /catalog/search and schema selection both do

    # The schema slice /sql/map shows the model (schema_context_service): several search queries are run, their top
    # tables are ranked by how many queries found them, then FK neighbours fill the remaining room.
    schema_max_queries: int = 10  # search queries per request: the whole request, then one per field and per condition
    schema_search_top_k: int = 5  # tables taken from each query's results
    schema_max_tables: int = 20  # cap on the slice: tables from similar examples, search hits and FK neighbours together

    # Approved (already-solved) RITMs shown to the model as reference solutions. They are embedded in memory, so
    # they can use a different model from the schema catalog. voyage-code-4 (the catalog's model) ranked the
    # right family first for 8 of 10 test tickets and its scores overlapped the wrong ones; voyage-4 got 10 of
    # 10, with the right family scoring at least 0.58. The floor is only a coarse filter on top of the
    # ranking -- retune it as the approved pool grows.
    approved_embedding_model: str = "voyage-4"
    approved_examples_k: int = 4
    approved_example_min_similarity: float = 0.5

    catalog_join_path_cap: int = 5
    # A table this many foreign keys point at is a hub (merchant): join paths may end at one but not pass through it.
    catalog_hub_min_references: int = 5
    # Which tables are hubs, named outright: comma-separated "schema.table" or "schema.*" (case-insensitive). The
    # count above only suits a schema with one tenant-like table; in a banking schema it also catches the core
    # entities (account, customer, loan) that reports legitimately join through. Empty falls back to the count.
    catalog_hub_tables: str = ""

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
