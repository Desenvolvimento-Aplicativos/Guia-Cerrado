# supabase_client.py
"""
Módulo de integração com o Supabase.

Responsável por:
- Criar o cliente Supabase (service role)
- Salvar registros de pagamento (tabela 'pagamentos')
- Salvar webhooks recebidos da Pagar.me (tabela 'webhooks_pagarme')
- Atualizar status de pagamento quando o webhook confirmar o pagamento

(INCREMENTO)
- Operações de pedidos (tabelas 'pedidos' e 'pedidos_header')
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, List  # <-- apenas acrescentei List

from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY


class SupabaseError(Exception):
    """Erro genérico da integração com o Supabase."""


# -------------------------------------------------------------------
# Inicialização do cliente
# -------------------------------------------------------------------

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise SupabaseError("SUPABASE_URL ou SUPABASE_SERVICE_ROLE_KEY não configurados.")

_supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def get_client() -> Client:
    """Retorna o cliente Supabase já inicializado."""
    return _supabase


# -------------------------------------------------------------------
# Funções de pagamento
# -------------------------------------------------------------------

def salvar_pagamento(
    user_id: Optional[str],
    email: str,
    tipo: str,
    pagarme_link: Dict[str, Any],
    origem: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Salva um registro na tabela 'pagamentos'.

    Campos sugeridos na tabela 'pagamentos':
      - id (uuid ou serial, PK)
      - user_id (text)
      - email (text)
      - tipo (text)
      - payment_link_id (text)
      - checkout_url (text)
      - pagarme_status (text)
      - pagarme_raw (jsonb)
      - origem (text)
      - created_at (timestamp)
      - updated_at (timestamp)
    """
    supabase = get_client()

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
      "updated_at": now_iso,
    }

    try:
        res = supabase.table("pagamentos").insert(row).execute()
    except Exception as exc:
        raise SupabaseError(f"Erro ao salvar pagamento no Supabase: {exc}") from exc

    return res.data[0] if getattr(res, "data", None) else row


def atualizar_pagamento_por_payment_link(
    payment_link_id: str,
    novo_status: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Atualiza a linha de 'pagamentos' com base no payment_link_id.

    Exemplo de uso:
      atualizar_pagamento_por_payment_link(
          payment_link_id="pl_123",
          novo_status="paid",
          extra={"data_pagamento": "..."}
      )
    """
    supabase = get_client()

    update_data: Dict[str, Any] = {
        "pagarme_status": novo_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        update_data.update(extra)

    try:
        supabase.table("pagamentos").update(update_data).eq(
            "payment_link_id", payment_link_id
        ).execute()
    except Exception as exc:
        raise SupabaseError(
            f"Erro ao atualizar pagamento (payment_link_id={payment_link_id}): {exc}"
        ) from exc


def atualizar_pagamento_por_order_id(
    order_id: str,
    novo_status: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Variante para quando você guardar o order_id da Pagar.me na tabela 'pagamentos'.
    """
    supabase = get_client()

    update_data: Dict[str, Any] = {
        "pagarme_status": novo_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        update_data.update(extra)

    try:
        supabase.table("pagamentos").update(update_data).eq(
            "order_id", order_id
        ).execute()
    except Exception as exc:
        raise SupabaseError(
            f"Erro ao atualizar pagamento (order_id={order_id}): {exc}"
        ) from exc


# -------------------------------------------------------------------
# Webhooks
# -------------------------------------------------------------------

def salvar_webhook_pagarme(event_type: str, payload: Dict[str, Any]) -> None:
    """
    Salva o webhook bruto na tabela 'webhooks_pagarme' para debug/auditoria.

    Campos sugeridos:
      - id (uuid, PK)
      - event_type (text)
      - payload (jsonb)
      - received_at (timestamp)
    """
    supabase = get_client()

    row = {
        "event_type": event_type,
        "payload": payload,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        supabase.table("webhooks_pagarme").insert(row).execute()
    except Exception as exc:
        # não levantamos erro aqui para não quebrar o webhook da Pagar.me
        print("Erro ao salvar webhook no Supabase:", exc)


# ===================================================================
# NOVAS FUNÇÕES – PEDIDOS (itens + cabeçalho)
# ===================================================================

# -----------------------------
# Itens do pedido (tabela 'pedidos')
# -----------------------------

def listar_itens_carrinho(numero_compra: str) -> List[Dict[str, Any]]:
    """
    Retorna todos os itens com status 'carrinho' para um numero_compra.
    Usado pelo pedidos.html e pelo backend antes de criar o checkout.
    """
    supabase = get_client()

    try:
        res = (
            supabase
            .table("pedidos")
            .select("*")
            .eq("numero_compra", numero_compra)
            .eq("status", "carrinho")
            .order("created_at", desc=False)
            .execute()
        )
    except Exception as exc:
        raise SupabaseError(
            f"Erro ao listar itens do carrinho (numero_compra={numero_compra}): {exc}"
        ) from exc

    return res.data or []


def marcar_itens_com_status(
    numero_compra: str,
    status: str,
) -> None:
    """
    Atualiza o campo 'status' de todos os itens da tabela 'pedidos'
    para um determinado numero_compra.
    Ex.: status = 'pago' ou 'cancelado'.
    """
    supabase = get_client()

    try:
        (
            supabase
            .table("pedidos")
            .update({"status": status})
            .eq("numero_compra", numero_compra)
            .execute()
        )
    except Exception as exc:
        raise SupabaseError(
            f"Erro ao atualizar itens do pedido (numero_compra={numero_compra}): {exc}"
        ) from exc


# -----------------------------
# Cabeçalho do pedido (tabela 'pedidos_header')
# -----------------------------

def upsert_pedido_header(
    numero_compra: str,
    total: float,
    dados_cliente: Optional[Dict[str, Any]] = None,
    status: str = "aguardando_pagamento",
    pagarme_transaction_id: Optional[str] = None,
    pagarme_checkout_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Cria ou atualiza (upsert) o registro em 'pedidos_header' para o numero_compra.

    - Usa 'numero_compra' como chave de conflito.
    - Atualiza total, status, dados do cliente e informações do Pagar.me.
    """
    supabase = get_client()
    now_iso = datetime.now(timezone.utc).isoformat()

    dados_cliente = dados_cliente or {}

    row = {
        "numero_compra": numero_compra,
        "total": total,
        "status": status,
        "pagarme_transaction_id": pagarme_transaction_id,
        "pagarme_checkout_url": pagarme_checkout_url,
        "cliente_nome": dados_cliente.get("nome"),
        "cliente_email": dados_cliente.get("email"),
        "cliente_cpf": dados_cliente.get("cpf"),
        "cliente_telefone": dados_cliente.get("telefone"),
        "cliente_endereco": dados_cliente.get("endereco"),
        "updated_at": now_iso,
    }

    try:
        res = (
            supabase
            .table("pedidos_header")
            .upsert(row, on_conflict="numero_compra")
            .execute()
        )
    except Exception as exc:
        raise SupabaseError(
            f"Erro ao upsert pedido_header (numero_compra={numero_compra}): {exc}"
        ) from exc

    # retorna a linha criada/atualizada, se o Supabase devolver
    return res.data[0] if getattr(res, "data", None) else row


def atualizar_status_pedido(
    numero_compra: str,
    novo_status: str,
    pagarme_transaction_id: Optional[str] = None,
) -> None:
    """
    Atualiza apenas o status (e opcionalmente o transaction_id) em 'pedidos_header'.
    Usado pelo webhook da Pagar.me.
    """
    supabase = get_client()
    now_iso = datetime.now(timezone.utc).isoformat()

    update_data: Dict[str, Any] = {
        "status": novo_status,
        "updated_at": now_iso,
    }
    if pagarme_transaction_id:
        update_data["pagarme_transaction_id"] = pagarme_transaction_id

    try:
        (
            supabase
            .table("pedidos_header")
            .update(update_data)
            .eq("numero_compra", numero_compra)
            .execute()
        )
    except Exception as exc:
        raise SupabaseError(
            f"Erro ao atualizar status do pedido (numero_compra={numero_compra}): {exc}"
        ) from exc


def obter_pedido_header(numero_compra: str) -> Optional[Dict[str, Any]]:
    """
    Busca o registro de 'pedidos_header' para um numero_compra específico.
    Útil para o histórico no pedidos.html.
    """
    supabase = get_client()

    try:
        res = (
            supabase
            .table("pedidos_header")
            .select("*")
            .eq("numero_compra", numero_compra)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise SupabaseError(
            f"Erro ao obter pedido_header (numero_compra={numero_compra}): {exc}"
        ) from exc

    if not res.data:
        return None
    return res.data[0]
