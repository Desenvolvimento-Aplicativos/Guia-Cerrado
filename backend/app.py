import os
import json
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import requests
from supabase import create_client, Client

# NOVO: usar os módulos centralizados
from supabase_client import (
    salvar_webhook_pagarme as sb_salvar_webhook_pagarme,
    atualizar_pagamento_por_payment_link,
    atualizar_pagamento_por_order_id,
    listar_itens_carrinho,
    upsert_pedido_header,
    atualizar_status_pedido,
    marcar_itens_com_status,
)
from pagarme_client import criar_link_pagamento_pedido, PagarmeError

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

# -------------------------------------------------------------------
# Flask: serve frontend estático + API
# -------------------------------------------------------------------
# app.py está em backend/, o index.html está na raiz do projeto.
# Por isso usamos static_folder="../" para apontar pra raiz.
app = Flask(
    __name__,
    static_folder="../",   # raiz do projeto
    static_url_path=""     # arquivos estáticos a partir de "/"
)

CORS(app, resources={r"/api/*": {"origins": FRONTEND_ORIGIN},
                     r"/webhook/*": {"origins": "*"}})

# -------------------------------------------------------------------
# Utilitários – ASSINATURA (membro/restaurante)
# -------------------------------------------------------------------

def criar_link_pagamento(tipo: str, email: str, nome: str, metadata_extra: dict | None = None):
    """
    Cria um Link de Pagamento / Checkout na Pagar.me para:
      - tipo = 'restaurante'  -> R$ 0,01 (teste)
      - tipo = 'membro'       -> R$ 0,01 (teste)

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
        auth=(PAGARME_SECRET_KEY, ""),  # Basic Auth com secret key como username
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
    (fluxo de ASSINATURA – mantém o que já funciona)
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
# Rotas – frontend básico
# -------------------------------------------------------------------

# Raiz: devolve o index.html da raiz do projeto
@app.route("/", methods=["GET"])
def serve_index():
    # como configuramos static_folder="../", o send_static_file
    # procura a partir da raiz do projeto
    return app.send_static_file("index.html")


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "env": "dev",
            "pagarme_base_url": PAGARME_BASE_URL,
        }
    )


# -------------------------------------------------------------------
# Rota de ASSINATURA (já existente)
# -------------------------------------------------------------------

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


# -------------------------------------------------------------------
# NOVO: Rota de CHECKOUT de PEDIDO (marmitas)
# -------------------------------------------------------------------

@app.route("/api/pedidos/checkout", methods=["POST"])
def api_pedidos_checkout():
    """
    Front chama ao clicar em "Comprar pedido" no pedidos.html.

    Espera JSON:
    {
      "numero_compra": "GC-...",
      "cliente": {
        "nome": "...",
        "email": "...",
        "cpf": "...",
        "telefone": "...",
        "endereco": "..."
      }
    }
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "JSON inválido"}), 400

    numero_compra = (data.get("numero_compra") or "").strip()
    cliente = data.get("cliente") or {}

    if not numero_compra:
        return jsonify({"ok": False, "error": "numero_compra é obrigatório."}), 400
    if not cliente.get("email"):
        return jsonify({"ok": False, "error": "email do cliente é obrigatório."}), 400

    # 1) Buscar itens do carrinho no Supabase
    try:
        itens_carrinho = listar_itens_carrinho(numero_compra)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Erro ao buscar carrinho: {e}"}), 500

    if not itens_carrinho:
        return jsonify({"ok": False, "error": "Carrinho vazio para este número de compra."}), 400

    # 2) Montar itens para Pagar.me
    itens_pagarme = []
    total_cents = 0

    for row in itens_carrinho:
        prato = row.get("prato") or "Item"
        preco_raw = row.get("preco")  # numeric do Supabase geralmente vem como string
        quantidade_raw = row.get("quantidade") or 1

        try:
            preco_float = float(preco_raw)
        except Exception:
            return jsonify({"ok": False, "error": f"Preço inválido para o item: {prato}"}), 500

        try:
            quantidade = int(quantidade_raw)
        except Exception:
            quantidade = 1

        if quantidade <= 0:
            quantidade = 1

        amount_cents = int(round(preco_float * 100))

        itens_pagarme.append(
            {
                "name": prato,
                "amount_cents": amount_cents,
                "quantity": quantidade,
            }
        )
        total_cents += amount_cents * quantidade

    total_reais = total_cents / 100.0

    # 3) Criar link de pagamento na Pagar.me para o PEDIDO
    try:
        pagarme_link = criar_link_pagamento_pedido(
            numero_compra=numero_compra,
            itens=itens_pagarme,
            cliente={
                "nome": cliente.get("nome"),
                "email": cliente.get("email"),
                "cpf": cliente.get("cpf"),
                "telefone": cliente.get("telefone"),
                "endereco": cliente.get("endereco"),
            },
            metadata_extra={
                "origem": "pedido_marmita",
            },
        )
    except PagarmeError as e:
        return jsonify({"ok": False, "error": f"Pagar.me: {e}"}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": f"Erro ao criar link de pagamento: {e}"}), 500

    # 4) Salvar/atualizar cabeçalho do pedido no Supabase
    try:
        upsert_pedido_header(
            numero_compra=numero_compra,
            total=total_reais,
            dados_cliente=cliente,
            status="aguardando_pagamento",
            pagarme_transaction_id=pagarme_link.get("id"),
            pagarme_checkout_url=pagarme_link.get("url"),
        )
    except Exception as e:
        print("Erro ao salvar pedido_header no Supabase:", e)

    return jsonify(
        {
            "ok": True,
            "checkout_url": pagarme_link.get("url"),
            "pagarme": pagarme_link,
        }
    )


# -------------------------------------------------------------------
# Webhook da Pagar.me (ASSINATURA + PEDIDOS)
# -------------------------------------------------------------------

@app.route("/webhook/pagarme", methods=["POST"])
def webhook_pagarme():
    raw_body = request.get_data(as_text=True)
    try:
        payload = json.loads(raw_body or "{}")
    except Exception:
        payload = {}

    event_type = payload.get("type")
    data_obj = payload.get("data", {}) or {}

    # Salva webhook bruto via módulo centralizado
    try:
        sb_salvar_webhook_pagarme(event_type or "", payload)
    except Exception as e:
        print("Erro ao salvar webhook no Supabase:", e)

    # Atualização de pagamentos (ASSINATURA)
    try:
        if event_type in ("order.paid", "charge.paid", "checkout.closed"):
            order_id = data_obj.get("id")
            metadata = data_obj.get("metadata") or {}
            payment_link_id = metadata.get("payment_link_id")

            # Fluxo antigo: tabela 'pagamentos'
            novo_status_pagamento = event_type

            if payment_link_id:
                atualizar_pagamento_por_payment_link(
                    payment_link_id=payment_link_id,
                    novo_status=novo_status_pagamento,
                    extra={"order_id": order_id} if order_id else None,
                )
            elif order_id:
                atualizar_pagamento_por_order_id(
                    order_id=order_id,
                    novo_status=novo_status_pagamento,
                )

            # NOVO: fluxo de PEDIDOS (marmitas)
            tipo = metadata.get("tipo") or metadata.get("tipo_assinatura")
            numero_compra = metadata.get("numero_compra")

            if tipo == "pedido" and numero_compra:
                if event_type in ("order.paid", "charge.paid"):
                    novo_status_pedido = "pago"
                elif event_type == "checkout.closed":
                    # checkout fechado sem confirmação explícita de pagamento
                    novo_status_pedido = "cancelado"
                else:
                    novo_status_pedido = "desconhecido"

                try:
                    atualizar_status_pedido(
                        numero_compra=numero_compra,
                        novo_status=novo_status_pedido,
                        pagarme_transaction_id=order_id,
                    )
                    marcar_itens_com_status(numero_compra, novo_status_pedido)
                except Exception as e:
                    print("Erro ao atualizar pedido a partir do webhook:", e)

    except Exception as e:
        print("Erro ao processar webhook Pagar.me:", e)

    return jsonify({"received": True}), 200


# -------------------------------------------------------------------
# Execução local
# -------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
