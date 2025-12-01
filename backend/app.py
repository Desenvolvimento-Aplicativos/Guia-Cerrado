import os
import json
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import requests
from supabase import create_client, Client

# -------------------------------------------------------------------
# Configuração básica
# -------------------------------------------------------------------

# Carrega variáveis do .env
load_dotenv()

PAGARME_SECRET_KEY = os.getenv("PAGARME_SECRET_KEY")
# Ambiente de testes da Pagar.me (muda para produção depois)
PAGARME_BASE_URL = os.getenv(
    "PAGARME_BASE_URL",
    "https://sdx-api.pagar.me/core/v5"  # sandbox, de acordo com docs de paymentlinks
)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "*")  # ex: http://127.0.0.1:5500

if not PAGARME_SECRET_KEY:
    raise RuntimeError("PAGARME_SECRET_KEY não está definido no .env")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL ou SUPABASE_SERVICE_ROLE_KEY ausentes no .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": FRONTEND_ORIGIN},
                     r"/webhook/*": {"origins": "*"}})

# -------------------------------------------------------------------
# Rota raiz (para não dar Not Found no domínio principal)
# -------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "ok",
        "message": "Backend Guia Cerrado online ✅",
        "routes": [
            "/api/health",
            "/api/criar-checkout",
            "/webhook/pagarme"
        ]
    }), 200

# -------------------------------------------------------------------
# Utilitários
# -------------------------------------------------------------------

def criar_link_pagamento(tipo: str, email: str, nome: str, metadata_extra: dict | None = None):
    """
    Cria um Link de Pagamento / Checkout na Pagar.me para:
      - tipo = 'restaurante'  -> R$ 100,00
      - tipo = 'membro'       -> R$ 29,99

    Retorna o JSON da Pagar.me (contendo 'url' e 'id') ou levanta Exception em erro.
    """
    tipo = (tipo or "").lower()
    if tipo == "restaurante":
        amount_cents = 1  # R$ 0,01 para testes
        item_name = "Assinatura Restaurante – Guia Cerrado"
    elif tipo == "membro":
        amount_cents = 1  # R$ 0,01 para testes
        item_name = "Assinatura Membro – Guia Cerrado"
    else:
        raise ValueError("tipo deve ser 'restaurante' ou 'membro'.")

    payload = {
        "type": "order",
        "name": f"{item_name} ({email})",
        "is_building": False,
        "payment_settings": {
            "accepted_payment_methods": ["credit_card", "boleto", "pix"],
            "credit_card_settings": {
                "operation_type": "auth_and_capture",
                "installments_setup": {
                    "interest_type": "simple"
                },
            }
        },
        "cart_settings": {
            "items": [
                {
                    "amount": amount_cents,
                    "name": item_name,
                    "default_quantity": 1
                }
            ]
        },
        "customer_settings": {
            "customer": {
                "name": nome or email,
                "email": email
            }
        },
        "metadata": {
            "tipo_assinatura": tipo,
            **(metadata_extra or {})
        }
    }

    url = f"{PAGARME_BASE_URL.rstrip('/')}/paymentlinks"

    response = requests.post(
        url,
        auth=(PAGARME_SECRET_KEY, ""),
        json=payload,
        timeout=30,
    )

    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"Erro ao criar link de pagamento ({response.status_code}): {detail}")

    return response.json()


def salvar_pagamento_supabase(
    user_id: str | None,
    email: str,
    tipo: str,
    pagarme_link: dict,
    origem: str | None = None,
):
    payment_link_id = pagarme_link.get("id")
    checkout_url = pagarme_link.get("url")
    status = pagarme_link.get("status", "active")

    now_iso = datetime.now(timezone.utc).isoformat()

    row = {
        "user_id": user_id,
        "email": email,
        "tipo": tipo,
        "payment_link_id": payment_link_id,
        "checkout_url": checkout_url,
        "pagarme_status": status,
        "pagarme_raw": pagarme_link,
        "origem": origem or "checkout_site",
        "created_at": now_iso,
    }

    res = supabase.table("pagamentos").insert(row).execute()
    return res

# -------------------------------------------------------------------
# Rotas API
# -------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "env": "dev",
            "pagarme_base_url": PAGARME_BASE_URL,
        }
    )

@app.route("/api/criar-checkout", methods=["POST"])
def api_criar_checkout():
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "JSON inválido"}), 400

    tipo = (data.get("tipo") or "").lower()
    email = (data.get("email") or "").strip()
    nome = (data.get("nome") or "").strip()
    user_id = data.get("user_id")
    extra = data.get("extra") or {}

    if not email:
        return jsonify({"ok": False, "error": "Campo 'email' é obrigatório."}), 400

    if tipo not in ("restaurante", "membro"):
        return jsonify(
            {"ok": False, "error": "Campo 'tipo' deve ser 'restaurante' ou 'membro'."}
        ), 400

    try:
        pagarme_link = criar_link_pagamento(
            tipo=tipo,
            email=email,
            nome=nome,
            metadata_extra={
                "supabase_user_id": user_id or "",
                **extra,
            },
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    try:
        salvar_pagamento_supabase(
            user_id=user_id,
            email=email,
            tipo=tipo,
            pagarme_link=pagarme_link,
            origem=data.get("origem"),
        )
    except Exception as e:
        print("Erro ao salvar no Supabase:", e)

    return jsonify(
        {
            "ok": True,
            "checkout_url": pagarme_link.get("url"),
            "payment_link_id": pagarme_link.get("id"),
            "pagarme": pagarme_link,
        }
    )

@app.route("/webhook/pagarme", methods=["POST"])
def webhook_pagarme():
    raw_body = request.get_data(as_text=True)
    try:
        payload = json.loads(raw_body or "{}")
    except Exception:
        payload = {}

    event_type = payload.get("type")
    data_obj = payload.get("data", {})

    try:
        supabase.table("webhooks_pagarme").insert(
            {
                "event_type": event_type,
                "payload": payload,
                "received_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()
    except Exception as e:
        print("Erro ao salvar webhook no Supabase:", e)

    try:
        if event_type in ("order.paid", "charge.paid", "checkout.closed"):
            order_id = data_obj.get("id")
            metadata = data_obj.get("metadata") or {}
            payment_link_id = metadata.get("payment_link_id")

            update_data = {
                "pagarme_status": event_type,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            if payment_link_id:
                supabase.table("pagamentos").update(update_data).eq(
                    "payment_link_id", payment_link_id
                ).execute()
            elif order_id:
                update_data["order_id"] = order_id
                supabase.table("pagamentos").update(update_data).eq(
                    "order_id", order_id
                ).execute()
    except Exception as e:
        print("Erro ao atualizar pagamento pelo webhook:", e)

    return jsonify({"received": True}), 200

# -------------------------------------------------------------------
# Execução local
# -------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
