"""
Crypto Pay API adapter.

Official docs: https://help.send.tg/en/articles/10279948-crypto-pay-api
Base URL: https://pay.crypt.bot/api/
Test URL: https://testnet-pay.crypt.bot/api/
Auth: Header ``Crypto-Pay-API-Token: <token>``

IMPORTANT:
    Before modifying this module, verify every method and field against the
    official documentation.  Do NOT implement from memory.

    The helper was authored based on the public, stable Crypto Pay API surface
    that has been available since 2022.  If any endpoint or field diverges
    from the live API, the adapter must be updated and a system_error logged.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────────────

class InvoiceStatus(str, Enum):
    ACTIVE = "active"
    PAID = "paid"
    EXPIRED = "expired"


class TransferStatus(str, Enum):
    COMPLETED = "completed"


class PaidBtnName(str, Enum):
    VIEW_ITEM = "viewItem"
    OPEN_CHANNEL = "openChannel"
    OPEN_BOT = "openBot"
    CALLBACK = "callback"


# ── Exceptions ───────────────────────────────────────────────────────────────

class CryptoPayError(Exception):
    """Base error for Crypto Pay API."""

    def __init__(self, code: int | None, message: str, method: str):
        self.code = code
        self.message = message
        self.method = method
        super().__init__(f"CryptoPay [{method}] code={code}: {message}")


class CryptoPayNetworkError(CryptoPayError):
    """Network / timeout error (ambiguous — transfer may have succeeded)."""
    pass


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InvoiceData:
    """Parsed invoice from Crypto Pay response."""
    invoice_id: int
    status: str
    asset: str
    amount: str  # Keep as string; caller converts to Decimal
    pay_url: str | None  # ``mini_app_invoice_url`` in API response
    description: str | None
    paid_at: str | None
    payload: str | None

    @classmethod
    def from_dict(cls, d: dict) -> InvoiceData:
        return cls(
            invoice_id=d["invoice_id"],
            status=d["status"],
            asset=d.get("asset", ""),
            amount=d.get("amount", "0"),
            pay_url=d.get("mini_app_invoice_url") or d.get("bot_invoice_url"),
            description=d.get("description"),
            paid_at=d.get("paid_at"),
            payload=d.get("payload"),
        )


@dataclass(frozen=True)
class TransferData:
    """Parsed transfer from Crypto Pay response."""
    transfer_id: int
    spend_id: str
    user_id: int
    asset: str
    amount: str
    status: str
    completed_at: str | None

    @classmethod
    def from_dict(cls, d: dict) -> TransferData:
        return cls(
            transfer_id=d["transfer_id"],
            spend_id=d.get("spend_id", ""),
            user_id=d.get("user_id", 0),
            asset=d.get("asset", ""),
            amount=d.get("amount", "0"),
            status=d.get("status", ""),
            completed_at=d.get("completed_at"),
        )


# ── Service ──────────────────────────────────────────────────────────────────

class CryptoPayService:
    """
    Isolated adapter for the Crypto Pay API.

    All monetary values are passed/returned as *strings* and converted to
    ``Decimal`` by the caller.  The token is never logged.

    Retry policy: up to ``max_retries`` attempts on 5xx / network errors
    with exponential back-off.
    """

    def __init__(
        self,
        api_token: str,
        base_url: str = "https://pay.crypt.bot/api",
        max_retries: int = 5,
        retry_delay: float = 2.0,
        timeout: float = 30.0,
    ):
        self._token = api_token
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            headers={"Crypto-Pay-API-Token": self._token},
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── low-level request ────────────────────────────────────────────────

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """
        POST ``/{method}`` with JSON body.  Retries on 5xx / network errors.
        """
        if self._session is None or self._session.closed:
            await self.start()

        url = f"{self._base_url}/{method}"
        last_exc: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                async with self._session.post(url, json=params or {}) as resp:  # type: ignore[union-attr]
                    body = await resp.json()

                    if resp.status >= 500:
                        logger.warning(
                            "CryptoPay %s 5xx (attempt %d/%d): %s",
                            method, attempt, self._max_retries, body,
                        )
                        last_exc = CryptoPayError(resp.status, str(body), method)
                        await asyncio.sleep(self._retry_delay * attempt)
                        continue

                    if not body.get("ok"):
                        error = body.get("error", {})
                        raise CryptoPayError(
                            code=error.get("code"),
                            message=error.get("name", str(body)),
                            method=method,
                        )

                    return body.get("result", {})

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "CryptoPay %s network error (attempt %d/%d): %s",
                    method, attempt, self._max_retries, exc,
                )
                last_exc = CryptoPayNetworkError(
                    code=None,
                    message=str(exc),
                    method=method,
                )
                await asyncio.sleep(self._retry_delay * attempt)

        raise last_exc  # type: ignore[misc]

    # ── getMe ────────────────────────────────────────────────────────────

    async def get_me(self) -> dict:
        """Test connectivity and verify token validity."""
        return await self._request("getMe")

    # ── createInvoice ────────────────────────────────────────────────────

    async def create_invoice(
        self,
        *,
        amount: str,
        asset: str = "USDT",
        description: str = "",
        payload: str = "",
        paid_btn_name: str | None = None,
        paid_btn_url: str | None = None,
        allow_comments: bool = False,
        allow_anonymous: bool = True,
        expires_in: int | None = None,
    ) -> InvoiceData:
        """
        Create an invoice.

        Parameters
        ----------
        amount : str
            Amount in ``asset`` currency (e.g. "10.50").
        asset : str
            Cryptocurrency code.  Default ``USDT``.
        description : str
            Optional description (max 1024 chars).
        payload : str
            Optional payload for identification (max 4096 bytes, not visible).
        paid_btn_name : str | None
            One of ``viewItem``, ``openChannel``, ``openBot``, ``callback``.
        paid_btn_url : str | None
            Required if ``paid_btn_name`` is set.
        allow_comments : bool
            Allow payer to add a comment.
        allow_anonymous : bool
            Allow anonymous payment.
        expires_in : int | None
            Seconds until the invoice expires (1-2678400).
        """
        params: dict[str, Any] = {
            "asset": asset,
            "amount": amount,
        }
        if description:
            params["description"] = description[:1024]
        if payload:
            params["payload"] = payload
        if paid_btn_name:
            params["paid_btn_name"] = paid_btn_name
            if paid_btn_url:
                params["paid_btn_url"] = paid_btn_url
        if not allow_comments:
            params["allow_comments"] = False
        if allow_anonymous:
            params["allow_anonymous"] = True
        if expires_in is not None:
            params["expires_in"] = max(1, min(expires_in, 2678400))

        result = await self._request("createInvoice", params)
        return InvoiceData.from_dict(result)

    # ── getInvoices ──────────────────────────────────────────────────────

    async def get_invoices(
        self,
        *,
        invoice_ids: list[int] | None = None,
        asset: str | None = None,
        status: str | None = None,
        offset: int = 0,
        count: int = 100,
    ) -> list[InvoiceData]:
        """
        Retrieve invoices.

        Parameters
        ----------
        invoice_ids : list[int] | None
            Filter by specific invoice IDs (comma-separated in API).
        asset : str | None
            Filter by asset.
        status : str | None
            Filter by status: ``active``, ``paid``, ``expired``.
        offset : int
            Pagination offset.
        count : int
            Number of invoices to return (1-1000, default 100).
        """
        params: dict[str, Any] = {
            "offset": offset,
            "count": min(count, 1000),
        }
        if invoice_ids:
            params["invoice_ids"] = ",".join(str(i) for i in invoice_ids)
        if asset:
            params["asset"] = asset
        if status:
            params["status"] = status

        result = await self._request("getInvoices", params)
        items = result if isinstance(result, list) else result.get("items", [])
        return [InvoiceData.from_dict(item) for item in items]

    async def get_invoice(self, invoice_id: int) -> InvoiceData | None:
        """Get a single invoice by ID.  Returns None if not found."""
        invoices = await self.get_invoices(invoice_ids=[invoice_id])
        return invoices[0] if invoices else None

    # ── deleteInvoice ────────────────────────────────────────────────────

    async def delete_invoice(self, invoice_id: int) -> bool:
        """Delete a specific invoice.  Returns True on success."""
        try:
            await self._request("deleteInvoice", {"invoice_id": invoice_id})
            return True
        except CryptoPayError:
            return False

    # ── transfer ─────────────────────────────────────────────────────────

    async def transfer(
        self,
        *,
        user_id: int,
        asset: str = "USDT",
        amount: str,
        spend_id: str,
        comment: str = "",
        disable_send_notification: bool = False,
    ) -> TransferData:
        """
        Send coins to a user.

        Parameters
        ----------
        user_id : int
            Telegram user ID of the recipient.
        asset : str
            Cryptocurrency code.
        amount : str
            Amount to transfer.
        spend_id : str
            **Unique** idempotency key (1-64 chars).
            Crypto Pay guarantees: a repeat transfer with the same ``spend_id``
            will NOT create a duplicate — it will return the existing transfer
            or raise an error.
        comment : str
            Optional comment (max 1024 chars).
        disable_send_notification : bool
            If True, recipient won't get a notification.

        Raises
        ------
        CryptoPayError
            API-level error (e.g. insufficient balance, invalid user).
        CryptoPayNetworkError
            Network / timeout error.  **Ambiguous** — the transfer may have
            succeeded.  Caller MUST check via ``get_transfers(spend_id=...)``
            before retrying.
        """
        params: dict[str, Any] = {
            "user_id": user_id,
            "asset": asset,
            "amount": amount,
            "spend_id": spend_id,
        }
        if comment:
            params["comment"] = comment[:1024]
        if disable_send_notification:
            params["disable_send_notification"] = True

        result = await self._request("transfer", params)
        return TransferData.from_dict(result)

    # ── getTransfers ─────────────────────────────────────────────────────

    async def get_transfers(
        self,
        *,
        transfer_ids: list[int] | None = None,
        spend_id: str | None = None,
        asset: str | None = None,
        offset: int = 0,
        count: int = 100,
    ) -> list[TransferData]:
        """
        Retrieve transfers (for reconciliation).

        Parameters
        ----------
        transfer_ids : list[int] | None
            Filter by transfer IDs.
        spend_id : str | None
            Filter by ``spend_id`` — used to verify if a transfer exists.
        asset : str | None
            Filter by asset.
        offset : int
            Pagination offset.
        count : int
            Number of transfers to return (1-1000).
        """
        params: dict[str, Any] = {
            "offset": offset,
            "count": min(count, 1000),
        }
        if transfer_ids:
            params["transfer_ids"] = ",".join(str(i) for i in transfer_ids)
        if spend_id:
            params["spend_id"] = spend_id
        if asset:
            params["asset"] = asset

        result = await self._request("getTransfers", params)
        items = result if isinstance(result, list) else result.get("items", [])
        return [TransferData.from_dict(item) for item in items]

    async def find_transfer_by_spend_id(self, spend_id: str) -> TransferData | None:
        """
        Look up a transfer by ``spend_id``.

        Used in reconciliation:
        - After network timeout, check if transfer actually went through.
        - Before retrying, ensure we don't double-pay.
        """
        transfers = await self.get_transfers(spend_id=spend_id)
        return transfers[0] if transfers else None

    # ── health check ─────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Verify API connectivity."""
        try:
            await self.get_me()
            return True
        except Exception:
            return False
