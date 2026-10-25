"""
url_security.py — Two-Layer Security Gate for Cossito (Phase 2).

Implements:
1. Layer 1 (Denylist): Enforces automated blocking of prohibited domains/patterns.
2. Layer 2 (Allowlist & Human Gate): Validates origin hierarchy, allowed domains,
   and OAuth/SSO login pathways. When an unknown external domain is encountered,
   it prompts the user via HITL/Overlay for explicit approval.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional, Set
from urllib.parse import urlparse
import ipaddress


def normalize_target_url(value: str) -> str:
    """Validate targets and infer HTTP for local addresses, HTTPS otherwise."""
    value = (value or '').strip()
    if not value:
        raise ValueError('Enter a target URL.')
    if '://' not in value:
        host = urlparse('//' + value).hostname or ''
        local = host == 'localhost' or host.endswith('.localhost')
        try:
            address = ipaddress.ip_address(host)
            local = local or address.is_loopback or address.is_private
        except ValueError:
            pass
        value = ('http://' if local else 'https://') + value
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Use an HTTP or HTTPS target URL.')
    if parsed.username or parsed.password or any(c.isspace() for c in parsed.netloc):
        raise ValueError('Enter a valid target host without embedded credentials.')
    _ = parsed.port  # Validate the port before starting the browser.
    return value

from models import ControlCommand, SecurityCheckResult, SecurityDecision, SecurityPolicy

logger = logging.getLogger(__name__)

_DEFAULT_DENYLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "url_denylist.json")
_DEFAULT_ALLOWLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "url_allowlist.json")

# Standard auth & identity provider domains and keyword patterns
_AUTH_DOMAINS: set[str] = {
    "accounts.google.com",
    "github.com",
    "login.microsoftonline.com",
    "appleid.apple.com",
    "auth0.com",
    "okta.com",
}

_AUTH_KEYWORDS: list[str] = [
    "login", "auth", "signin", "sign-in", "sso", "oauth", "captcha",
    "accounts.google", "github.com/login", "identity", "saml"
]


class URLSecurityGate:
    """Two-layer URL security gate and policy validator."""

    def __init__(
        self,
        denylist_path: Optional[str] = None,
        allowlist_path: Optional[str] = None,
        target_base_url: str = "",
    ) -> None:
        self.denylist_path = denylist_path or _DEFAULT_DENYLIST_PATH
        self.allowlist_path = allowlist_path or _DEFAULT_ALLOWLIST_PATH
        self.target_base_url = target_base_url
        self.policy = SecurityPolicy(target_base_url=target_base_url)
        self._load_policies()

    def _load_policies(self) -> None:
        """Load denylist and allowlist from JSON configuration files."""
        # Load Denylist
        denied_items = []
        if os.path.exists(self.denylist_path):
            try:
                with open(self.denylist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        denied_items = [str(x).strip().lower() for x in data if x]
            except Exception as e:
                logger.warning(f"[URLSecurityGate] Failed to load denylist: {e}")
        self.policy.denied_patterns = denied_items

        # Load Allowlist
        allowed_items = []
        if os.path.exists(self.allowlist_path):
            try:
                with open(self.allowlist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        allowed_items = [str(x).strip().lower() for x in data if x]
            except Exception as e:
                logger.warning(f"[URLSecurityGate] Failed to load allowlist: {e}")
        self.policy.allowed_domains = allowed_items

    def save_allowlist(self) -> None:
        """Persist updated allowlist to file."""
        try:
            with open(self.allowlist_path, "w", encoding="utf-8") as f:
                json.dump(self.policy.allowed_domains, f, indent=2)
        except Exception as e:
            logger.warning(f"[URLSecurityGate] Failed to save allowlist: {e}")

    def add_allowed_domain(self, domain: str, persistent: bool = True) -> None:
        """Approve a domain permanently or for the active session."""
        dom = domain.strip().lower()
        if not dom:
            return
        if persistent:
            if dom not in self.policy.allowed_domains:
                self.policy.allowed_domains.append(dom)
                self.save_allowlist()
        else:
            self.policy.session_approved_domains.add(dom)

    @staticmethod
    def extract_domain(url: str) -> str:
        """Extract clean hostname/domain from a URL."""
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            return parsed.netloc.lower().split(":")[0]
        except Exception:
            return ""

    @staticmethod
    def extract_root_domain(host: str) -> str:
        """Extract root domain (e.g. example.com from app.example.com)."""
        if not host:
            return ""
        parts = host.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host

    def check_bad_urls_denylist(self, url: str) -> Optional[SecurityCheckResult]:
        """Layer 1 Denylist (BadUrlsChecker equivalent) — Fail-Closed evaluation."""
        try:
            curr_lower = url.lower().strip()
            curr_domain = self.extract_domain(url)

            # Fail-closed on dangerous non-HTTP schemes
            for bad_scheme in ("javascript:", "vbscript:", "file://"):
                if curr_lower.startswith(bad_scheme):
                    return SecurityCheckResult(
                        url=url,
                        decision=SecurityDecision.DENIED,
                        reason=f"Layer 1 Denylist (Fail-Closed): Forbidden protocol scheme '{bad_scheme}'",
                        domain=curr_domain,
                    )

            for pattern in self.policy.denied_patterns:
                if pattern in curr_lower or (curr_domain and pattern == curr_domain):
                    return SecurityCheckResult(
                        url=url,
                        decision=SecurityDecision.DENIED,
                        reason=f"URL matched forbidden denylist pattern: '{pattern}'",
                        domain=curr_domain,
                    )
            return None
        except Exception as e:
            # Fail-Closed: If Layer 1 checker encounters an unexpected error, deny navigation
            return SecurityCheckResult(
                url=url,
                decision=SecurityDecision.DENIED,
                reason=f"Layer 1 Denylist (Fail-Closed): Verification error ({e})",
                domain="",
            )

    def evaluate_url(
        self,
        current_url: str,
        target_url: Optional[str] = None,
        is_hitl_paused: bool = False,
    ) -> SecurityCheckResult:
        """Evaluate a URL through Layer 1 (Denylist) and Layer 2 (Allowlist/Approval)."""
        if not current_url or current_url in ("about:blank", "chrome://newtab/", "chrome://version/"):
            return SecurityCheckResult(
                url=current_url,
                decision=SecurityDecision.ALLOWED,
                reason="Internal browser page",
                domain="browser_internal",
            )

        curr_domain = self.extract_domain(current_url)
        curr_lower = current_url.lower()

        # ------------------------------------------------------------------
        # Layer 1: Denylist Enforcement (BadUrlsChecker Fail-Closed)
        # ------------------------------------------------------------------
        denied_res = self.check_bad_urls_denylist(current_url)
        if denied_res is not None:
            return denied_res

        # ------------------------------------------------------------------
        # Auth & Login Gateway Exemption
        # ------------------------------------------------------------------
        # If user is in an interactive HITL pause (logging in), allow navigation freely
        if is_hitl_paused:
            return SecurityCheckResult(
                url=current_url,
                decision=SecurityDecision.ALLOWED,
                reason="User active in HITL pause session",
                domain=curr_domain,
            )

        # Check known auth providers / OAuth pathways
        if any(auth_d in curr_domain for auth_d in _AUTH_DOMAINS) or any(kw in curr_lower for kw in _AUTH_KEYWORDS):
            return SecurityCheckResult(
                url=current_url,
                decision=SecurityDecision.ALLOWED,
                reason="Authentication / SSO identity provider verified",
                domain=curr_domain,
            )

        # ------------------------------------------------------------------
        # Layer 2: Origin & Allowlist Verification
        # ------------------------------------------------------------------
        effective_target = target_url or self.policy.target_base_url or self.target_base_url
        target_domain = self.extract_domain(effective_target)

        if curr_domain:
            # 1. Target Host Match
            if target_domain and curr_domain == target_domain:
                return SecurityCheckResult(
                    url=current_url,
                    decision=SecurityDecision.ALLOWED,
                    reason=f"Matches target host: '{target_domain}'",
                    domain=curr_domain,
                )

            # 2. Subdomain / Root Domain Match
            curr_root = self.extract_root_domain(curr_domain)
            target_root = self.extract_root_domain(target_domain) if target_domain else ""
            if target_root and curr_root == target_root:
                return SecurityCheckResult(
                    url=current_url,
                    decision=SecurityDecision.ALLOWED,
                    reason=f"Matches target root domain hierarchy: '{target_root}'",
                    domain=curr_domain,
                )

            # 3. Pre-configured Allowlist Check
            for allowed in self.policy.allowed_domains:
                if curr_domain == allowed or curr_domain.endswith("." + allowed) or curr_root == allowed:
                    return SecurityCheckResult(
                        url=current_url,
                        decision=SecurityDecision.ALLOWED,
                        reason=f"Domain '{curr_domain}' verified in allowlist",
                        domain=curr_domain,
                    )

            # 4. Active Session Approved Domains Check
            if curr_domain in self.policy.session_approved_domains:
                return SecurityCheckResult(
                    url=current_url,
                    decision=SecurityDecision.ALLOWED,
                    reason=f"Domain '{curr_domain}' approved for current session",
                    domain=curr_domain,
                )

        # ------------------------------------------------------------------
        # Layer 2 Fallback: Unknown External Domain -> Needs Human Approval
        # ------------------------------------------------------------------
        return SecurityCheckResult(
            url=current_url,
            decision=SecurityDecision.NEEDS_APPROVAL,
            reason=f"External domain '{curr_domain}' is not in the allowlist. Human approval required.",
            domain=curr_domain,
        )

    def is_url_allowed(
        self,
        current_url: str,
        target_url: Optional[str] = None,
        is_hitl_paused: bool = False,
    ) -> bool:
        """Convenience boolean check for fast-path routing."""
        res = self.evaluate_url(current_url, target_url, is_hitl_paused)
        return res.decision == SecurityDecision.ALLOWED
