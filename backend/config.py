import os
from dotenv import load_dotenv

# Carrega variáveis do arquivo .env
load_dotenv()

# ========= Pagar.me =========
# Chave secreta (Secret API Key) criada no painel da Pagar.me.
# Ex.: ak_test_xxxxxxxxxxxxxxxxxxxxxxxxx
PAGARME_SECRET_KEY: str | None = os.getenv("PAGARME_SECRET_KEY")

# URL base da API da Pagar.me (padrão: sandbox v5)
# Você pode sobrescrever no .env se precisar.
PAGARME_BASE_URL: str = os.getenv(
    "PAGARME_BASE_URL",
    "https://sdx-api.pagar.me/core/v5"
)

# ========= Supabase =========
SUPABASE_URL: str | None = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY: str | None = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# ========= Geral / CORS =========
# Origem permitida para o front (onde está rodando seu HTML)
# Exemplo comum em dev: http://127.0.0.1:5500
FRONTEND_ORIGIN: str = os.getenv("FRONTEND_ORIGIN", "*")

ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
PORT: int = int(os.getenv("PORT", "5000"))


def validate_config() -> None:
    """
    Valida se as variáveis obrigatórias estão presentes.
    Pode ser chamada no início do app.py.
    Levanta RuntimeError se algo importante estiver faltando.
    """
    missing = []

    if not PAGARME_SECRET_KEY:
        missing.append("PAGARME_SECRET_KEY")

    if not SUPABASE_URL:
        missing.append("SUPABASE_URL")

    if not SUPABASE_SERVICE_ROLE_KEY:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")

    if missing:
        raise RuntimeError(
            f"Variáveis obrigatórias ausentes no .env: {', '.join(missing)}"
        )
