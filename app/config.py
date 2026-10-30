import importlib.util
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)


def _get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _resolve_secret_from_provider(provider_name: str) -> str:
    provider = (provider_name or "env").strip().lower()
    if provider in {"env", "environment", "none", ""}:
        return ""

    if provider in {"aws_ssm", "aws-ssm", "aws_ssm_parameter_store", "aws-secrets-manager", "aws_secrets_manager"}:
        secret_name = os.getenv("AWS_SECRET_NAME") or os.getenv("SECRET_NAME") or os.getenv("AWS_SSM_PATH") or ""
        if not secret_name:
            return ""
        if importlib.util.find_spec("boto3") is None:
            raise RuntimeError("SECRET_MANAGER_PROVIDER requires boto3 to be installed")
        import boto3

        response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_name)
        secret = response.get("SecretString") or response.get("SecretBinary") or ""
        if isinstance(secret, bytes):
            secret = secret.decode("utf-8")
        return str(secret).strip()

    if provider in {"gcp_secret_manager", "gcp-secret-manager", "google_secret_manager", "gcp"}:
        secret_name = os.getenv("GCP_SECRET_NAME") or os.getenv("SECRET_NAME") or ""
        if not secret_name:
            return ""
        if importlib.util.find_spec("google.cloud.secretmanager") is None:
            raise RuntimeError("SECRET_MANAGER_PROVIDER requires google-cloud-secret-manager")
        from google.cloud import secretmanager_v1

        client = secretmanager_v1.SecretManagerServiceClient()
        project_id = os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT") or ""
        if not project_id:
            raise RuntimeError("GCP_SECRET_MANAGER needs GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT")
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        payload = response.payload.data.decode("utf-8")
        return payload.strip()

    if provider in {"azure_key_vault", "azure-key-vault", "azure"}:
        secret_name = os.getenv("AZURE_SECRET_NAME") or os.getenv("SECRET_NAME") or ""
        if not secret_name:
            return ""
        if importlib.util.find_spec("azure.identity") is None or importlib.util.find_spec("azure.keyvault.secrets") is None:
            raise RuntimeError("SECRET_MANAGER_PROVIDER requires azure-identity and azure-keyvault-secrets")
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        vault_url = os.getenv("AZURE_KEY_VAULT_URL", "").strip()
        if not vault_url:
            raise RuntimeError("AZURE_KEY_VAULT_URL is required when SECRET_MANAGER_PROVIDER=azure")
        client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
        return client.get_secret(secret_name).value.strip()

    raise RuntimeError(f"Unsupported SECRET_MANAGER_PROVIDER '{provider}'")


class Settings:
    @staticmethod
    def app_env() -> str:
        return os.getenv("APP_ENV", "production").strip() or "production"

    @staticmethod
    def secret_manager_provider() -> str:
        return os.getenv("SECRET_MANAGER_PROVIDER", "env").strip()

    @staticmethod
    def api_secret_key() -> str:
        candidates = [
            os.getenv("API_SECRET_KEY", "").strip(),
            os.getenv("APP_SECRET_KEY", "").strip(),
            os.getenv("SECRET_KEY", "").strip(),
        ]
        for value in candidates:
            if value:
                return value

        provider = Settings.secret_manager_provider()
        secret = _resolve_secret_from_provider(provider)
        if secret:
            return secret
        return ""

    @staticmethod
    def database_url() -> str:
        return (
            os.getenv("TEST_DATABASE_URL", "").strip()
            or os.getenv("DATABASE_URL", "sqlite:///./edu_billing.db").strip()
            or "sqlite:///./edu_billing.db"
        )

    @staticmethod
    def cookie_secure() -> bool:
        return _get_bool("COOKIE_SECURE", default=False)

    @staticmethod
    def cors_origins() -> str:
        return os.getenv("CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").strip()

    @staticmethod
    def is_demo_allowed() -> bool:
        return _get_bool("ALLOW_INSECURE_DEMO", default=False) or _get_bool("API_ALLOW_INSECURE_DEMO", default=False)

    @staticmethod
    def auto_start_bot() -> bool:
        raw = os.getenv("AUTO_START_BOT")
        if raw is None:
            return bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
        return _get_bool("AUTO_START_BOT", default=False)

    @staticmethod
    def telegram_admin_ids() -> str:
        raw = os.getenv("TELEGRAM_ADMIN_IDS", "").strip() or os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        return raw

    @staticmethod
    def telegram_admin_ids_list() -> list[int]:
        raw = Settings.telegram_admin_ids()
        values = []
        for token in raw.replace(";", ",").split(","):
            cleaned = token.strip()
            if cleaned and cleaned.isdigit():
                values.append(int(cleaned))
        return values

    @staticmethod
    def validate_runtime_config() -> None:
        app_env = Settings.app_env().lower()
        secret_key = Settings.api_secret_key()
        demo_allowed = Settings.is_demo_allowed()

        if app_env == "production" and not secret_key and not demo_allowed:
            raise RuntimeError("Production requires API_SECRET_KEY to be configured")
        if app_env == "production" and secret_key == "replace_with_a_long_random_secret" and not demo_allowed:
            raise RuntimeError("API_SECRET_KEY must be replaced before running in production")
        if app_env == "production" and len(secret_key) < 32 and not demo_allowed:
            raise RuntimeError("API_SECRET_KEY is too weak for production; use at least 32 random chars")
        if (
            app_env == "production"
            and not Settings.cookie_secure()
            and not demo_allowed
            and "localhost" not in Settings.cors_origins()
        ):
            raise RuntimeError("Production requires COOKIE_SECURE=1 unless behind a trusted local reverse proxy")

        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if bot_token and Settings.auto_start_bot():
            admin_ids = Settings.telegram_admin_ids_list()
            if app_env == "production" and not admin_ids:
                raise RuntimeError("TELEGRAM_BOT_TOKEN is configured but TELEGRAM_ADMIN_IDS is empty; bot payments are blocked")


settings = Settings()
