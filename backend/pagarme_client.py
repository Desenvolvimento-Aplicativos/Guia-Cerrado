# pagarme_client.py
"""
Módulo de integração com a API da Pagar.me (v5).

Responsável por:
- Criar link de pagamento (Checkout) para assinatura membro/restaurante.
- (Opcional) Consultar recursos se necessário no futuro.
- (NOVO) Criar link de pagamento para pedidos avulsos (marmitas).
"""

from __future__ import annotations

import requests
from typing import Any, Dict, Optional

from config import PAGARME_SECRET_KEY, PAGARME_BASE_URL


class PagarmeError(Exception):
    """Erro genérico da integração com a Pagar.me."""


def _auth_tuple() -> tuple[str, str]:
    """
    Retorna a tupla de autenticação usada pelo requests (Basic Auth).
    Na Pagar.me v5 usamos a Secret Key como "username" e senha vazia.
    """
    if not PAGARME_SECRET_KEY:
        raise PagarmeError("PAGARME_SECRET_KEY não configurada.")
    return (PAGARME_SECRET_KEY, "")


def _base_url(path: str) -> str:
    """
    Monta a URL completa da Pagar.me, garantindo que não tenha barras duplicadas.
    """
    return f"{PAGARME_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


# -------------------------------------------------------------------
# Função principal que você vai usar no app.py (ASSINATURA)
# -------------------------------------------------------------------


def criar_link_pagamento(
    tipo: str,
    email: str,
    nome: str,
    metadata_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Cria um Link de Pagamento / Checkout na Pagar.me.

    Regras de negócio atuais:
      - tipo == "restaurante" → R$ 100,00 / mês
      - tipo == "membro"      → R$ 29,99 / mês

    Retorna o JSON da Pagar.me (dict com, por exemplo, "id" e "url").

    Levanta PagarmeError em caso de erro.
    """
    tipo = (tipo or "").lower()

    if tipo == "restaurante":
        amount_cents = 10000  # R$ 100,00
        item_name = "Assinatura Restaurante – Guia Cerrado"
    elif tipo == "membro":
        amount_cents = 2999  # R$ 29,99
        item_name = "Assinatura Membro – Guia Cerrado"
    else:
        raise PagarmeError("tipo deve ser 'restaurante' ou 'membro'.")

    metadata = {"tipo_assinatura": tipo}
    if metadata_extra:
        metadata.update(metadata_extra)

    payload: Dict[str, Any] = {
        "type": "order",
        "name": f"{item_name} ({email})",
        "is_building": False,
        "payment_settings": {
            "accepted_payment_methods": ["credit_card", "boleto", "pix"],
            "credit_card_settings": {
                "operation_type": "auth_and_capture",
                "installments_setup": {
                    "interest_type": "simple",
                },
            },
        },
        "cart_settings": {
            "items": [
                {
                    "amount": amount_cents,
                    "name": item_name,
                    "default_quantity": 1,
                }
            ]
        },
        "customer_settings": {
            "customer": {
                "name": nome or email,
                "email": email,
            }
        },
        "metadata": metadata,
    }

    url = _base_url("paymentlinks")

    try:
        resp = requests.post(
            url,
            auth=_auth_tuple(),
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise PagarmeError(f"Erro de rede ao chamar Pagar.me: {exc}") from exc

    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise PagarmeError(
            f"Erro ao criar link de pagamento ({resp.status_code}): {detail}"
        )

    try:
        data = resp.json()
    except Exception as exc:
        raise PagarmeError(f"Resposta inválida da Pagar.me: {resp.text}") from exc

    return data


# -------------------------------------------------------------------
# (NOVO) Link de pagamento para PEDIDOS (marmitas)
# -------------------------------------------------------------------

def criar_link_pagamento_pedido(
    numero_compra: str,
    itens: list[Dict[str, Any]],
    cliente: Dict[str, Any],
    metadata_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Cria um Link de Pagamento / Checkout na Pagar.me para um PEDIDO avulso.

    Esperado:
      - numero_compra: ex. "GC-1733423533" (mesmo usado no Supabase)
      - itens: lista de dicts com, no mínimo:
          {
            "name": "Frango cremoso, arroz...",
            "amount_cents": 2990,      # em centavos
            "quantity": 2              # opcional, default = 1
          }
      - cliente: dict com:
          {
            "nome": "Fulano",
            "email": "fulano@exemplo.com",
            "cpf": "00000000000",       # opcional
            "telefone": "+55..."
          }

    Retorna o JSON da Pagar.me (com "id", "url", etc.) ou levanta PagarmeError.
    """
    if not itens:
        raise PagarmeError("Lista de itens vazia ao criar link de pagamento de pedido.")

    # Monta itens no formato esperado pela Pagar.me
    cart_items: list[Dict[str, Any]] = []
    total_cents = 0

    for item in itens:
        name = item.get("name")
        amount_cents = int(item.get("amount_cents", 0))
        quantity = int(item.get("quantity", 1) or 1)

        if not name or amount_cents <= 0 or quantity <= 0:
            raise PagarmeError(
                f"Item inválido ao criar link de pagamento de pedido: {item}"
            )

        cart_items.append(
            {
                "amount": amount_cents,
                "name": name,
                "default_quantity": quantity,
            }
        )
        total_cents += amount_cents * quantity

    # Dados do cliente
    nome_cliente = cliente.get("nome") or cliente.get("email") or "Cliente"
    email_cliente = cliente.get("email")
    if not email_cliente:
        raise PagarmeError("Email do cliente é obrigatório para criar o link do pedido.")

    metadata: Dict[str, Any] = {
        "tipo": "pedido",
        "numero_compra": numero_compra,
    }
    if metadata_extra:
        metadata.update(metadata_extra)

    payload: Dict[str, Any] = {
        "type": "order",
        "name": f"Pedido {numero_compra} – Guia Cerrado",
        "is_building": False,
        "payment_settings": {
            "accepted_payment_methods": ["credit_card", "boleto", "pix"],
            "credit_card_settings": {
                "operation_type": "auth_and_capture",
                "installments_setup": {
                    "interest_type": "simple",
                },
            },
        },
        "cart_settings": {
            "items": cart_items
        },
        "customer_settings": {
            "customer": {
                "name": nome_cliente,
                "email": email_cliente,
                # Se quiser usar documento/telefone, pode estender aqui:
                # "document": cliente.get("cpf"),
                # "phones": {...}
            }
        },
        "metadata": metadata,
    }

    url = _base_url("paymentlinks")

    try:
        resp = requests.post(
            url,
            auth=_auth_tuple(),
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise PagarmeError(f"Erro de rede ao chamar Pagar.me: {exc}") from exc

    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise PagarmeError(
            f"Erro ao criar link de pagamento de pedido ({resp.status_code}): {detail}"
        )

    try:
        data = resp.json()
    except Exception as exc:
        raise PagarmeError(f"Resposta inválida da Pagar.me: {resp.text}") from exc

    return data


# -------------------------------------------------------------------
# Exemplos extras que podem ser úteis depois (não usados ainda)
# -------------------------------------------------------------------


def obter_payment_link(payment_link_id: str) -> Dict[str, Any]:
    """
    Consulta um payment link específico na Pagar.me.
    Útil para debug ou validações extras.
    """
    url = _base_url(f"paymentlinks/{payment_link_id}")

    try:
        resp = requests.get(url, auth=_auth_tuple(), timeout=30)
    except requests.RequestException as exc:
        raise PagarmeError(f"Erro de rede ao consultar payment link: {exc}") from exc

    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise PagarmeError(
            f"Erro ao consultar payment link ({resp.status_code}): {detail}"
        )

    return resp.json()
