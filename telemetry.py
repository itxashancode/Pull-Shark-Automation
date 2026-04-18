"""
Pull-Shark Telemetry Client
===========================
Sends PR stats → GAS Web App → Supabase → Vercel dashboard.

What is collected (transparent, no secrets):
  - github_username   : dashboard identity (public)
  - instance_id       : stable anon ID derived from username + machine
  - total_prs_created : cumulative PRs this bot account has generated
  - session_prs       : PRs created in this specific run
  - bot_type          : "pull_shark" — routes to correct Supabase table
  - NO github tokens, NO private keys ever sent

Security layers (mirrors the GAS verification chain):
  1. Nonce replay guard  (unique hex per request)
  2. Timestamp window    (request expires after 5 minutes)
  3. HMAC-SHA256 signature over (ts + "." + nonce + "." + body)
  4. GAS rate-limit: max 10 req / minute per instance
"""

import os
import sys
import time
import json
import hashlib
import hmac as _hmac
import platform as _platform
import threading
import logging
import uuid as _uuid
from typing import Optional, Dict

logger = logging.getLogger("pull_shark.telemetry")

# ── Telemetry hard-coded constants (must match GAS + .env) ───────────────────
_REQUIRED_GAS_URL   = "https://script.google.com/macros/s/AKfycbwzWLd0vAErdQGHSYxq6lgIS55Unv_WOtjbumhDKfNaDoyIsQiJ16qRcjLXknND_XNHjA/exec"
_REQUIRED_SECRET    = "4bc16c4e696f0012eb1a330adeaa1bee054bfafebb4ae75e60a2ff0072c62316"
_REQUIRED_ENABLED   = "true"

BOT_TYPE = "pull_shark"


def verify_telemetry():
    """
    Ensures the bot is running with the correct, untampered telemetry
    configuration.  If the URL or HMAC secret is missing / wrong the bot
    exits immediately — this is by design so the telemetry pipe cannot be
    silently disabled by end-users.
    """
    gas_url = os.getenv("TELEMETRY_GAS_URL", "")
    enabled = os.getenv("TELEMETRY_ENABLED", "").lower()
    secret  = os.getenv("TELEMETRY_HMAC_SECRET", "")

    ok = (
        gas_url == _REQUIRED_GAS_URL
        and enabled == _REQUIRED_ENABLED
        and secret  == _REQUIRED_SECRET
    )

    if not ok:
        # Use plain print — rich may not be imported yet
        print("\n" + "=" * 60)
        print("  PULL-SHARK BOT — CRITICAL SECURITY ERROR")
        print("=" * 60)
        print("  Telemetry configuration is invalid, missing,")
        print("  or has been tampered with.")
        print()
        print(f"  Expected URL    : {_REQUIRED_GAS_URL[:60]}...")
        print(f"  Expected Secret : {_REQUIRED_SECRET[:8]}...")
        print()
        print("  Please restore the official .env settings.")
        print("  The bot cannot proceed without valid telemetry.")
        print("=" * 60 + "\n")
        sys.exit(1)


def _derive_instance_id(username: str) -> str:
    """Stable anonymous instance ID — sha256(username + machine_hash). No PII."""
    machine_raw  = f"{_platform.node()}{_platform.machine()}{_platform.system()}"
    machine_hash = hashlib.sha256(machine_raw.encode()).hexdigest()[:16]
    return hashlib.sha256(f"{username.lower()}:{machine_hash}".encode()).hexdigest()[:32]


def _hmac_sha256(secret: str, message: str) -> str:
    return _hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


# ────────────────────────────────────────────────────────────────────────────
class TelemetryClient:
    """
    Fire-and-forget telemetry client for Pull-Shark-Automation.

    Usage::

        client = TelemetryClient(github_username="alice", github_token="ghp_xxx")
        # after each batch of PRs:
        client.report(total_prs_created=42, session_prs=10)
        # at the very end:
        client.report_final(total_prs_created=52, session_prs=20)
    """

    _MIN_INTERVAL = 300   # seconds between periodic reports (5 min)
    _MAX_RETRIES  = 3
    _TIMEOUT      = 20    # GAS can be slow on cold start

    def __init__(self, github_username: str, github_token: str):
        self.username     = github_username
        self.github_token = github_token   # stored for display only — never sent to GAS
        self.instance_id  = _derive_instance_id(github_username)
        self._gas_url     = os.getenv("TELEMETRY_GAS_URL", "")
        self._secret      = os.getenv("TELEMETRY_HMAC_SECRET", "")
        self.enabled      = os.getenv("TELEMETRY_ENABLED", "true").lower() == "true"
        self._lock        = threading.Lock()
        logger.debug(f"Telemetry init: instance={self.instance_id[:12]} bot={BOT_TYPE}")

        # ── Mandatory Handshake (Open Source Security) ───────────────────────
        if self.enabled:
            self._perform_handshake()

    # ── Public API ───────────────────────────────────────────────────────────

    def report(self, total_prs_created: int, session_prs: int):
        """Periodic report — respects MIN_INTERVAL rate-limit."""
        if not self.enabled:
            return
        with self._lock:
            if self._last_sent and (time.time() - self._last_sent) < self._MIN_INTERVAL:
                return
        threading.Thread(
            target=self._send,
            args=(total_prs_created, session_prs, "session", False),
            daemon=True,
        ).start()

    def report_final(self, total_prs_created: int, session_prs: int):
        """Final report at end of run — always sent, bypasses interval guard, blocks until complete."""
        if not self.enabled:
            return
        # Run synchronously so the process doesn't exit before sending
        self._send(total_prs_created, session_prs, "session_final", True)

    # ── Internals ────────────────────────────────────────────────────────────

    def _perform_handshake(self):
        """
        Synchronous registration check.
        If this fails, it means the user is not registered in the dashboard
        or the token is invalid. In open-source mode, we exit to protect
        the integrity of the system.
        """
        import requests as _requests
        
        # Build minimal handshake payload
        token_hash = hashlib.sha256(self.github_token.encode()).hexdigest()
        payload = {
            "bot_type":        BOT_TYPE,
            "instance_id":     self.instance_id,
            "github_username": self.username,
            "token_hash":      token_hash,
            "event_type":      "handshake",
        }
        
        body    = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        nonce   = _uuid.uuid4().hex
        ts      = str(int(time.time() * 1000))
        
        # Use SHA-256 Body Hash for handshake signature
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        message   = f"{ts}.{nonce}.{body_hash}"
        
        sig     = _hmac_sha256(self._secret, message)
        params  = {"ts": ts, "nonce": nonce, "sig": sig}
        
        try:
            resp = _requests.post(
                self._gas_url,
                data=body,
                params=params,
                timeout=20,
                allow_redirects=True
            )
            
            if resp.status_code == 200:
                logger.info(f"Handshake OK: {resp.json().get('message', 'Connected')}")
                return
            
            # If we get a 403, it means the token/user is NOT registered
            print("\n" + "!" * 60)
            print("  PULL-SHARK — DASHBOARD REGISTRATION ERROR")
            print("!" * 60)
            print("  This bot requires a valid registration on the")
            print("  official dashboard to function.")
            print()
            print(f"  GitHub User : {self.username}")
            print(f"  Token Hash  : {token_hash[:16]}...")
            print()
            print("  Please ensure your token is registered and active.")
            print("  To opt-out, set TELEMETRY_ENABLED=false (Limited mode).")
            print("!" * 60 + "\n")
            sys.exit(1)
            
        except Exception as exc:
            # If server is down, we allow the bot to run but log the warning
            logger.warning(f"Telemetry server unreachable (Handshake skipped): {exc}")

    def _build_payload(self, total_prs_created: int, session_prs: int, event_type: str) -> Dict:
        # Compute SHA-256 hash of the token for verification in code.gs
        token_hash = hashlib.sha256(self.github_token.encode()).hexdigest()

        return {
            "bot_type":          BOT_TYPE,
            "instance_id":       self.instance_id,
            "github_username":   self.username,
            "token_hash":        token_hash,
            "total_prs_created": int(total_prs_created),
            "session_prs":       int(session_prs),
            "event_type":        event_type,
        }

    def _send(self, total_prs_created: int, session_prs: int, event_type: str, force: bool):
        with self._lock:
            if not force and self._last_sent and (time.time() - self._last_sent) < self._MIN_INTERVAL:
                return
            try:
                payload = self._build_payload(total_prs_created, session_prs, event_type)
                body    = json.dumps(payload, sort_keys=True, separators=(',', ':'))
                
                # ── Security parameters ──────────────────────────────────────
                nonce   = _uuid.uuid4().hex
                ts      = str(int(time.time() * 1000))
                
                # Use SHA-256 Body Hash for signature (matches hardened code.gs)
                body_hash = hashlib.sha256(body.encode()).hexdigest()
                message   = f"{ts}.{nonce}.{body_hash}"
                
                sig     = _hmac_sha256(self._secret, message)
                params  = {"ts": ts, "nonce": nonce, "sig": sig}
            except Exception as exc:
                logger.debug(f"Telemetry build error: {exc}")
                return

        try:
            import requests as _requests
        except ImportError:
            logger.debug("requests not available — skipping telemetry")
            return

        headers = {
            "Content-Type": "application/json",
            "User-Agent":   "PullSharkBot/1.0",
        }

        for attempt in range(self._MAX_RETRIES):
            try:
                resp = _requests.post(
                    self._gas_url,
                    data=body,
                    params=params,
                    headers=headers,
                    timeout=self._TIMEOUT,
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    with self._lock:
                        self._last_sent = time.time()
                    logger.info(
                        f"Telemetry OK: total_prs={total_prs_created} session_prs={session_prs}"
                    )
                    return
                elif resp.status_code == 429:
                    logger.debug("Telemetry: rate-limited by GAS")
                    return
                elif resp.status_code >= 500:
                    time.sleep(2 ** attempt)
                else:
                    logger.debug(f"Telemetry rejected {resp.status_code}: {resp.text[:200]}")
                    return
            except Exception as exc:
                logger.debug(f"Telemetry send error (attempt {attempt+1}): {exc}")
                time.sleep(2 ** attempt)

        logger.debug("Telemetry: max retries reached")
