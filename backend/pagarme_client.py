# pagarme_client.py
"""
Módulo de integração com a API da Pagar.me (v5).

Responsável por:
- Criar link de pagamento (Checkout) para assinatura membro/restaurante.
- (Opcional) Consultar recursos se necessário no futuro.
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
# Função principal que você vai usar no app.py
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
