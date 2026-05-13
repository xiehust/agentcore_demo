"""
Idempotent setup for the AgentCore Payments demo.

Sub-commands:
    all         create/reuse every resource (credential provider, manager+
                connector, instrument, session)
    manager     create/reuse Payment Manager + Connector + Credential Provider
    instrument  create/reuse Payment Instrument
    session     create a new Payment Session (sessions expire -- re-run often)
    status      describe every known resource and print its state

Resource IDs are persisted to .env.local so agent.py can read them.

Usage:
    python setup.py all
    python setup.py session    # after a session expires
    python setup.py status
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from dotenv import dotenv_values, load_dotenv

try:
    from bedrock_agentcore.payments import PaymentClient, PaymentManager
except ImportError:
    # Fallback path seen in some docs snippets.
    from bedrock_agentcore.payments.client import PaymentClient  # type: ignore[no-redef]
    from bedrock_agentcore.payments.manager import PaymentManager  # type: ignore[no-redef]

from bedrock_agentcore.services.identity import IdentityClient

# Connector IDs must match this regex (stricter than what CreatePaymentConnector
# accepts for the name). We use it to detect un-deletable orphan connectors.
CONNECTOR_ID_RE = re.compile(r"^([0-9a-z][-]?){1,100}-[0-9a-z]{10}$")


ROOT = Path(__file__).parent
ENV_LOCAL = ROOT / ".env.local"


# ---------- .env.local persistence ----------

def _load_env() -> None:
    load_dotenv(ROOT / ".env")
    load_dotenv(ENV_LOCAL, override=True)


def _write_local(updates: dict[str, str]) -> None:
    """Merge updates into .env.local, preserving existing keys."""
    existing: dict[str, str] = {}
    if ENV_LOCAL.exists():
        for k, v in dotenv_values(ENV_LOCAL).items():
            if v is not None:
                existing[k] = v
    existing.update(updates)
    lines = [f"{k}={v}" for k, v in existing.items()]
    ENV_LOCAL.write_text("\n".join(lines) + "\n")
    for k, v in updates.items():
        os.environ[k] = v
        print(f"  wrote {k} to .env.local")


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: environment variable {name} is required. See .env.example.")
    return val


# ---------- client factories ----------

def _region() -> str:
    return os.environ.get("AWS_REGION", "us-west-2")


# ---------- payment provider selection ----------

def _provider_spec() -> dict[str, Any]:
    """Resolve PAYMENT_PROVIDER (default 'coinbase') to everything the
    credential provider + connector create calls need. Supports 'coinbase'
    (CoinbaseCDP) and 'privy' (StripePrivy)."""
    p = os.environ.get("PAYMENT_PROVIDER", "coinbase").lower().strip()
    if p in ("coinbase", "coinbasecdp", "cdp"):
        return {
            "id": "coinbase",
            "vendor": "CoinbaseCDP",                  # credential vendor
            "connector_type": "CoinbaseCDP",          # connector type
            "config_key": "coinbaseCDP",              # connector config dict key
            "credentials_input": {
                "coinbaseCdpConfiguration": {
                    "apiKeyId": _require("CDP_API_KEY_ID"),
                    "apiKeySecret": _require("CDP_API_KEY_SECRET"),
                    "walletSecret": _require("CDP_WALLET_SECRET"),
                },
            },
            "name_slug": "coinbase-cdp",              # for credential provider name
            "connector_short": "coinbaseconnector",   # for connector name
        }
    if p in ("privy", "stripeprivy", "stripe", "stripe-privy"):
        # Privy generates authorization keys with a "wallet-auth:" prefix
        # that AgentCore rejects -- strip it if the user left it in.
        auth_priv = _require("PRIVY_AUTH_PRIVATE_KEY")
        if auth_priv.startswith("wallet-auth:"):
            auth_priv = auth_priv[len("wallet-auth:"):]
            print("note: stripped 'wallet-auth:' prefix from PRIVY_AUTH_PRIVATE_KEY")
        return {
            "id": "privy",
            "vendor": "StripePrivy",
            "connector_type": "StripePrivy",
            "config_key": "stripePrivy",
            "credentials_input": {
                "stripePrivyConfiguration": {
                    "appId": _require("PRIVY_APP_ID"),
                    "appSecret": _require("PRIVY_APP_SECRET"),
                    "authorizationPrivateKey": auth_priv,
                    "authorizationId": _require("PRIVY_AUTH_ID"),
                },
            },
            "name_slug": "stripe-privy",
            "connector_short": "privyconnector",
        }
    sys.exit(f"ERROR: unknown PAYMENT_PROVIDER {p!r}. Use 'coinbase' or 'privy'.")


def _payment_client() -> PaymentClient:
    return PaymentClient(region_name=_region())


def _identity_client() -> IdentityClient:
    return IdentityClient(_region())


def _payment_manager(manager_arn: str) -> PaymentManager:
    return PaymentManager(payment_manager_arn=manager_arn, region_name=_region())


# ---------- sub-commands ----------

def cmd_manager() -> None:
    """Create (or reuse) Payment Credential Provider + Manager + Connector.

    Three independently-idempotent steps so we don't produce cascade orphans
    when one step fails. Lookup order for each step:
      1. existing ID in .env.local
      2. existing resource with the expected name
      3. create new
    """
    prefix = os.environ.get("RESOURCE_PREFIX", "agentcore-payment-demo")
    spec = _provider_spec()
    print(f"provider: {spec['vendor']}")
    manager_name = _manager_name(prefix)
    connector_name = _connector_name(prefix, spec)
    provider_name = _provider_name(prefix, spec)

    pc = _payment_client()
    ic = _identity_client()

    cred_arn = _ensure_credential_provider(ic, provider_name, spec)
    manager_arn = _ensure_manager(pc, manager_name)
    connector_id = _ensure_connector(
        pc, _id_from_arn(manager_arn), connector_name, cred_arn, spec
    )

    _write_local({
        "PAYMENT_MANAGER_ARN": manager_arn,
        "PAYMENT_CONNECTOR_ID": connector_id,
        "PAYMENT_CREDENTIAL_PROVIDER_ARN": cred_arn,
    })


def _ensure_credential_provider(ic: IdentityClient, name: str,
                                spec: dict[str, Any]) -> str:
    """Return credentialProviderArn; reuse if a provider with this name exists."""
    try:
        listing = ic.list_payment_credential_providers()
        for p in listing.get("credentialProviders", []):
            if p.get("name") == name:
                arn = (p.get("credentialProviderArn")
                       or p.get("paymentCredentialProviderArn"))
                if arn:
                    print(f"reusing credential provider: {arn}")
                    return arn
    except ClientError as e:
        print(f"WARN: list_payment_credential_providers failed: {e}; will create")

    print(f"creating credential provider {name!r} (vendor={spec['vendor']})...")
    resp = ic.create_payment_credential_provider(
        name=name,
        credential_provider_vendor=spec["vendor"],
        provider_configuration_input=spec["credentials_input"],
    )
    arn = (resp.get("credentialProviderArn")
           or resp.get("paymentCredentialProviderArn"))
    if not arn:
        sys.exit(f"ERROR: create_payment_credential_provider returned: {resp!r}")
    return arn


def _ensure_manager(pc: PaymentClient, name: str) -> str:
    """Return paymentManagerArn; reuse a READY manager with this name."""
    try:
        listing = pc.list_payment_managers()
        for pm in listing.get("paymentManagers", []):
            if pm.get("name") == name and pm.get("status") == "READY":
                arn = pm.get("paymentManagerArn")
                print(f"reusing manager: {arn}")
                return arn
    except ClientError as e:
        print(f"WARN: list_payment_managers failed: {e}; will create")

    print(f"creating manager {name!r}...")
    resp = pc.create_payment_manager(
        name=name,
        role_arn=_require("PAYMENTS_ROLE_ARN"),
        authorizer_type="AWS_IAM",
        description="AgentCore Payments demo",
        wait_for_ready=True,
    )
    pm = resp.get("paymentManager", resp)
    arn = pm.get("paymentManagerArn")
    if not arn:
        sys.exit(f"ERROR: create_payment_manager returned: {resp!r}")
    return arn


def _ensure_connector(pc: PaymentClient, manager_id: str, name: str,
                      cred_arn: str, spec: dict[str, Any]) -> str:
    """Return paymentConnectorId; reuse a connector on this manager whose
    type matches the selected provider and whose ID matches the strict regex.

    Prefers the ID already recorded in .env.local so downstream resources
    (instrument, session) stay consistent across re-runs.
    """
    try:
        listing = pc.list_payment_connectors(payment_manager_id=manager_id)
        candidates = []
        for c in listing.get("paymentConnectors", []):
            cid = c.get("paymentConnectorId")
            if not cid:
                continue
            if not CONNECTOR_ID_RE.match(cid):
                print(f"skipping un-retrievable orphan connector: {cid}")
                continue
            existing_type = c.get("providerType") or c.get("type")
            if existing_type != spec["connector_type"]:
                continue
            if c.get("status") == "READY":
                candidates.append(cid)

        recorded = os.environ.get("PAYMENT_CONNECTOR_ID")
        if recorded and recorded in candidates:
            print(f"reusing recorded connector: {recorded}")
            return recorded
        if candidates:
            print(f"reusing connector: {candidates[0]}")
            return candidates[0]
    except ClientError as e:
        print(f"WARN: list_payment_connectors failed: {e}; will create")

    print(f"creating connector {name!r} (type={spec['connector_type']})...")
    resp = pc.create_payment_connector(
        payment_manager_id=manager_id,
        name=name,
        connector_type=spec["connector_type"],
        credential_provider_configurations=[
            {spec["config_key"]: {"credentialProviderArn": cred_arn}},
        ],
        description=f"{spec['vendor']} connector for the demo",
        wait_for_ready=True,
    )
    conn = resp.get("paymentConnector", resp)
    cid = conn.get("paymentConnectorId")
    if not cid:
        sys.exit(f"ERROR: create_payment_connector returned: {resp!r}")
    return cid


def cmd_instrument() -> None:
    """Create (or reuse) an embedded crypto wallet for the user."""
    manager_arn = _require("PAYMENT_MANAGER_ARN")
    connector_id = _require("PAYMENT_CONNECTOR_ID")
    user_id = _require("USER_ID")
    user_email = _require("USER_EMAIL")

    mgr = _payment_manager(manager_arn)

    existing_id = os.environ.get("PAYMENT_INSTRUMENT_ID")
    if existing_id:
        try:
            resp = mgr.get_payment_instrument(
                user_id=user_id,
                payment_instrument_id=existing_id,
            )
            status = resp.get("status") or resp.get("Status")
            bound_connector = resp.get("paymentConnectorId")
            if bound_connector and bound_connector != connector_id:
                print(f"recorded instrument is bound to connector "
                      f"{bound_connector}, but current connector is "
                      f"{connector_id}. Creating a new instrument.")
            elif status in ("ACTIVE", "INITIATED"):
                print(f"reusing Payment Instrument: {existing_id} (status={status})")
                _maybe_print_fund_url(resp)
                return
            else:
                print(f"recorded instrument in state {status!r}, creating new one")
        except ClientError as e:
            print(f"recorded instrument not found ({e.response['Error']['Code']}); creating new")

    print(f"creating Payment Instrument for user {user_id}...")
    instrument = mgr.create_payment_instrument(
        user_id=user_id,
        payment_connector_id=connector_id,
        payment_instrument_type="EMBEDDED_CRYPTO_WALLET",
        payment_instrument_details={
            "embeddedCryptoWallet": {
                "network": "ETHEREUM",
                "linkedAccounts": [{"email": {"emailAddress": user_email}}],
            },
        },
    )
    instrument_id = instrument.get("paymentInstrumentId") or instrument.get("PaymentInstrumentId")
    if not instrument_id:
        sys.exit(f"ERROR: create_payment_instrument returned unexpected shape: {instrument!r}")
    print(f"  instrument: {instrument_id}")
    _write_local({"PAYMENT_INSTRUMENT_ID": instrument_id})
    _maybe_print_fund_url(instrument)


def _maybe_print_fund_url(instrument: dict[str, Any]) -> None:
    details = instrument.get("paymentInstrumentDetails", {})
    wallet = details.get("embeddedCryptoWallet", {})
    redirect = wallet.get("redirectUrl")
    address = wallet.get("walletAddress")
    banner = "=" * 72
    print(f"\n{banner}\nNEXT STEP: Fund the wallet and grant agent permissions")
    if address:
        print(f"Wallet address: {address}")
    if redirect:
        # Coinbase WalletHub -- fund + grant permission in one page.
        print(f"Open this URL in your browser (Coinbase WalletHub):\n  {redirect}")
    else:
        # Privy -- no hosted hub; point to Circle faucet + SDK template.
        print("Fund via Circle faucet (Base Sepolia USDC):")
        print("  https://faucet.circle.com/")
        print("Grant agent permission: run the Privy AgentCore SDK frontend")
        print("  https://github.com/privy-io/aws-agentcore-sdk")
    print(f"{banner}\n")


def cmd_session() -> None:
    """Create a fresh Payment Session. Always creates new -- sessions expire."""
    manager_arn = _require("PAYMENT_MANAGER_ARN")
    user_id = _require("USER_ID")
    max_usd = os.environ.get("SESSION_MAX_USD", "1.00")
    expiry_min = int(os.environ.get("SESSION_EXPIRY_MINUTES", "60"))

    mgr = _payment_manager(manager_arn)
    print(f"creating Payment Session (max={max_usd} USD, expiry={expiry_min}min)...")
    session = mgr.create_payment_session(
        user_id=user_id,
        limits={"maxSpendAmount": {"value": max_usd, "currency": "USD"}},
        expiry_time_in_minutes=expiry_min,
    )
    session_id = session.get("paymentSessionId") or session.get("PaymentSessionId")
    if not session_id:
        sys.exit(f"ERROR: create_payment_session returned unexpected shape: {session!r}")
    print(f"  session: {session_id}")
    _write_local({"PAYMENT_SESSION_ID": session_id})


def cmd_status() -> None:
    """Describe every known resource."""
    manager_arn = os.environ.get("PAYMENT_MANAGER_ARN")
    instrument_id = os.environ.get("PAYMENT_INSTRUMENT_ID")
    session_id = os.environ.get("PAYMENT_SESSION_ID")
    user_id = os.environ.get("USER_ID", "")

    if not manager_arn:
        print("no PAYMENT_MANAGER_ARN recorded -- run `python setup.py manager` first")
        return

    pc = _payment_client()
    mgr = _payment_manager(manager_arn)

    print(f"Region: {_region()}\n")

    print(f"Payment Manager:  {manager_arn}")
    try:
        pm = pc.get_payment_manager(payment_manager_id=_id_from_arn(manager_arn))
        print(f"  status: {pm.get('status') or pm.get('Status')}\n")
    except ClientError as e:
        print(f"  ERROR: {e}\n")

    if instrument_id:
        print(f"Payment Instrument: {instrument_id}")
        try:
            inst = mgr.get_payment_instrument(
                user_id=user_id, payment_instrument_id=instrument_id,
            )
            wallet = inst.get("paymentInstrumentDetails", {}).get("embeddedCryptoWallet", {})
            print(f"  status: {inst.get('status')}")
            print(f"  network: {wallet.get('network')}")
            print(f"  address: {wallet.get('walletAddress')}")

            connector_id = os.environ.get("PAYMENT_CONNECTOR_ID", "")
            # Chain enum: SOLANA_DEVNET|SOLANA|BASE_SEPOLIA|BASE|ETHEREUM
            chain = os.environ.get("BALANCE_CHAIN", "BASE_SEPOLIA")
            token = os.environ.get("BALANCE_TOKEN", "USDC")
            try:
                bal = mgr.get_payment_instrument_balance(
                    payment_connector_id=connector_id,
                    payment_instrument_id=instrument_id,
                    chain=chain,
                    token=token,
                    user_id=user_id,
                )
                tb = bal.get("tokenBalance", {})
                raw = tb.get("amount")
                decimals = tb.get("decimals", 0)
                if raw is not None:
                    human = int(raw) / (10 ** int(decimals)) if decimals else raw
                    print(f"  balance ({chain}/{token}): {human} {tb.get('token', '')}")
                else:
                    print(f"  balance ({chain}/{token}): 0 (wallet not yet funded)")
            except Exception as e:
                print(f"  balance: (unavailable: {e})")
            _maybe_print_fund_url(inst)
        except ClientError as e:
            print(f"  ERROR: {e}")
        print()

    if session_id:
        print(f"Payment Session: {session_id}")
        try:
            s = mgr.get_payment_session(
                user_id=user_id, payment_session_id=session_id,
            )
            print(f"  status: {s.get('status')}")
            print(f"  spent:  {s.get('spentAmount')}")
            print(f"  limits: {s.get('limits')}")
            print(f"  remaining: {s.get('remainingAmount')}")
        except Exception as e:
            print(f"  session expired or not found: {e}")
            print("  => run `python setup.py session` to create a new one")


def cmd_all() -> None:
    cmd_manager()
    cmd_instrument()
    cmd_session()
    print("\nAll resources ready. Run `python agent.py` after funding the wallet.")


# ---------- helpers ----------

def _id_from_arn(arn: str) -> str:
    """Extract the trailing resource id from a Payment Manager ARN."""
    return arn.rsplit("/", 1)[-1]


# Resource name constraints differ per resource type. The service enforces
# different regexes, so we sanitize the user-supplied RESOURCE_PREFIX per type.
def _manager_name(prefix: str) -> str:
    """Payment Manager name: [a-zA-Z][a-zA-Z0-9]{0,47} -- letters and digits only."""
    cleaned = "".join(c for c in prefix if c.isalnum()) or "demo"
    if not cleaned[0].isalpha():
        cleaned = "d" + cleaned
    return (cleaned + "Manager")[:48]


def _connector_name(prefix: str, spec: dict[str, Any]) -> str:
    """Connector name: lowercase alphanumeric only.

    The name regex ([a-zA-Z][a-zA-Z0-9_]{0,47}) and the ID regex
    (([0-9a-z][-]?){1,100}-[0-9a-z]{10}) are mutually incompatible -- the
    first forbids hyphens, the second forbids uppercase and underscores.
    Lowercase alphanumerics is the intersection.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "", prefix.lower()) or "demo"
    if not cleaned[0].isalpha():
        cleaned = "d" + cleaned
    suffix = spec["connector_short"]
    keep = 48 - len(suffix)
    return cleaned[:keep] + suffix


def _provider_name(prefix: str, spec: dict[str, Any]) -> str:
    """Credential provider name: letters, digits, '_' and '-'."""
    return f"{prefix}-{spec['name_slug']}"[:48]


# ---------- entry point ----------

COMMANDS = {
    "all": cmd_all,
    "manager": cmd_manager,
    "instrument": cmd_instrument,
    "session": cmd_session,
    "status": cmd_status,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=sorted(COMMANDS), help="which step to run")
    args = parser.parse_args()

    _load_env()
    COMMANDS[args.command]()


if __name__ == "__main__":
    main()
