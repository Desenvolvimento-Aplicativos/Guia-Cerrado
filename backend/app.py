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

    # Body conforme docs de Link de Pagamento / Checkout Pagar.me :contentReference[oaicite:2]{index=2}
    payload = {
        "type": "order",  # para recorrência real você pode depois migrar para 'subscription' + plano
        "name": f"{item_name} ({email})",
        "is_building": False,
        "payment_settings": {
            "accepted_payment_methods": ["credit_card", "boleto", "pix"],
            "credit_card_settings": {
                "operation_type": "auth_and_capture",
                "installments_setup": {
                    "interest_type": "simple"
                },
                # aqui dá pra configurar parcelamento se quiser
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
        # customer_settings é opcional, mas ajuda nos relatórios
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
        auth=(PAGARME_SECRET_KEY, ""),  # Basic Auth com secret key como username :contentReference[oaicite:3]{index=3}
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
    """
    Salva o registro do pagamento no Supabase, na tabela 'pagamentos'.
    Campos sugeridos para a tabela:
      id (serial / uuid)        - PK
      user_id (text)
      email (text)
      tipo (text)
      payment_link_id (text)
      checkout_url (text)
      pagarme_status (text)
      pagarme_raw (jsonb)
      origem (text)
      created_at (timestamp)
    """
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
# Rotas
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
    """
    Rota chamada pelo front (assinatura.html) para criar o Checkout.

    Espera um JSON assim:
    {
      "tipo": "restaurante" | "membro",
      "email": "cliente@exemplo.com",
      "nome": "Nome do Cliente",
      "user_id": "... opcional, supabase auth ...",
      "extra": { ... opcional ... }
    }

    Resposta:
    {
      "ok": true,
      "checkout_url": "https://payment-link.pagar.me/pl_...",
      "payment_link_id": "pl_...",
      "pagarme": { ...resposta bruta... }
    }
    """
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

    # Salva no Supabase (ignora erro silenciosamente para não quebrar o checkout)
    try:
        salvar_pagamento_supabase(
            user_id=user_id,
            email=email,
            tipo=tipo,
            pagarme_link=pagarme_link,
            origem=data.get("origem"),
        )
    except Exception as e:
        # log básico; em produção, use algo melhor
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
    """
    Endpoint para receber webhooks da Pagar.me.
    Você deve configurar essa URL na dashboard da Pagar.me, associando
    aos eventos relevantes (ex.: order.paid, charge.paid). :contentReference[oaicite:4]{index=4}

    Aqui fazemos:
      1. Guardar o webhook bruto na tabela 'webhooks_pagarme' (debug).
      2. Se for um evento de pagamento aprovado, marcar 'pagamentos' como pago.
    """
    raw_body = request.get_data(as_text=True)
    try:
        payload = json.loads(raw_body or "{}")
    except Exception:
        payload = {}

    event_type = payload.get("type")
    data_obj = payload.get("data", {})

    # 1) Salvar webhook bruto
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

    # 2) Tentar marcar pagamento como pago, se for evento relevante.
    # Exemplo de payload de order.paid nas docs :contentReference[oaicite:5]{index=5}
    try:
        if event_type in ("order.paid", "charge.paid", "checkout.closed"):
            # Tenta identificar via metadata ou ids
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
                # Se você guardar order_id em 'pagarme_raw', pode filtrar via contains
                # ou criar uma coluna separada em 'pagamentos' com esse id.
                # Aqui assumimos que existe uma coluna order_id.
                update_data["order_id"] = order_id
                supabase.table("pagamentos").update(update_data).eq(
                    "order_id", order_id
                ).execute()
    except Exception as e:
        print("Erro ao atualizar pagamento pelo webhook:", e)

    # Webhook precisa responder 2xx para ser considerado OK
    return jsonify({"received": True}), 200


# -------------------------------------------------------------------
# Execução local
# -------------------------------------------------------------------

if __name__ == "__main__":
    # Em produção, use gunicorn/uwsgi, etc.
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
