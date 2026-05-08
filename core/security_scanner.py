"""
core/security_scanner.py — Non-destructive security signal detection.

This module provides passive and validation-only checks during traversal.
It does not exploit, brute-force, bypass auth, or perform stress behaviour.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page, Response

import config

logger = logging.getLogger(__name__)


@dataclass
class SecurityFinding:
    category: str
    severity: str
    confidence: str
    route: str
    title: str
    evidence: str
    reproduction_steps: str
    remediation: str
    screenshot_path: str = ""
    network_log: str = ""
    console_log: str = ""


class SecurityScanner:
    """Passive security scanner attached to a Playwright page."""

    _SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
        ("JWT Token", re.compile(r"eyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9._-]{8,}\.[a-zA-Z0-9._-]{8,}")),
            ("Google API Key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
            ("Private Key Block", re.compile(r"-----BEGIN (RSA|EC|OPENSSH|DSA) PRIVATE KEY-----")),
            ("Generic Token", re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_\-\./+=]{12,}['\"]")),
    )

    _ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("SQL Error", re.compile(r"(?i)(sql syntax|mysql|postgresql|sqlite|odbc|ORA-\d{4,})")),
        ("Stack Trace", re.compile(r"(?i)(traceback|stack trace|Exception in thread|TypeError:|ReferenceError:|at \w+ \()")),
        ("Debug Page", re.compile(r"(?i)(werkzeug debugger|django debug|symfony exception|laravel|rails application trace)")),
    )

    _SENSITIVE_STORAGE_KEYS = (
        "token", "jwt", "session", "secret", "auth", "password", "apikey", "api_key", "bearer"
    )

    _SECURITY_HEADERS = {
        "content-security-policy": "Missing CSP can increase XSS impact.",
        "x-frame-options": "Missing X-Frame-Options can enable clickjacking.",
        "x-content-type-options": "Missing X-Content-Type-Options can allow MIME sniffing.",
        "strict-transport-security": "Missing HSTS weakens HTTPS enforcement.",
        "referrer-policy": "Missing Referrer-Policy can leak URL metadata.",
        "permissions-policy": "Missing Permissions-Policy may expose browser capabilities.",
    }

    def __init__(self, page: Page, run_id: str, base_domain: str):
        self.page = page
        self.run_id = run_id
        self.base_domain = base_domain
        self.findings: list[SecurityFinding] = []
        self._seen: set[tuple[str, str, str]] = set()
        self._network_events: list[dict[str, Any]] = []
        self._console_events: list[str] = []
        self._js_exceptions: list[str] = []
        self._passive_installed = False
        self._flushed_count = 0

        sec_dir = config.OUTPUT_DIR / "security"
        sec_dir.mkdir(parents=True, exist_ok=True)
        self._network_log_path = sec_dir / f"{run_id}_network.jsonl"
        self._console_log_path = sec_dir / f"{run_id}_console.log"

    def install_passive_hooks(self) -> None:
        if self._passive_installed:
            return

        self.page.on("console", lambda msg: asyncio.create_task(self._on_console(msg)))
        self.page.on("pageerror", lambda exc: asyncio.create_task(self._on_page_error(exc)))
        self.page.on("response", lambda resp: asyncio.create_task(self._on_response(resp)))
        self._passive_installed = True

    async def _on_console(self, msg) -> None:
        text = (msg.text or "")[:500]
        level = msg.type
        entry = f"[{level}] {text}"
        self._console_events.append(entry)
        self._append_line(self._console_log_path, entry)

        if level in {"error", "warning"}:
            for name, pat in self._ERROR_PATTERNS:
                if pat.search(text):
                    self._add_finding(
                        category="runtime_error_exposure",
                        severity="HIGH" if "Stack" in name else "MEDIUM",
                        confidence="HIGH",
                        route=self.page.url,
                        title=f"{name} Indicator in Console",
                        evidence=text,
                        reproduction_steps="Navigate to the route and inspect browser console output.",
                        remediation="Return generic client-safe errors and disable verbose debug output.",
                        console_log=entry,
                    )

        if any(k in text.lower() for k in ("token", "jwt", "secret", "apikey", "password")):
            self._add_finding(
                category="sensitive_console_logs",
                severity="MEDIUM",
                confidence="MEDIUM",
                route=self.page.url,
                title="Sensitive Data Indicator in Console Logs",
                evidence=text,
                reproduction_steps="Load the route and observe console logs.",
                remediation="Avoid logging secrets/tokens in frontend console output.",
                console_log=entry,
            )

    async def _on_page_error(self, exc) -> None:
        msg = str(exc)[:800]
        self._js_exceptions.append(msg)
        for name, pat in self._ERROR_PATTERNS:
            if pat.search(msg):
                self._add_finding(
                    category="js_exception_exposure",
                    severity="MEDIUM",
                    confidence="HIGH",
                    route=self.page.url,
                    title=f"{name} Indicator in JS Exception",
                    evidence=msg,
                    reproduction_steps="Trigger the same UI action and inspect JS exception output.",
                    remediation="Handle exceptions gracefully and avoid exposing internal traces.",
                    console_log=msg,
                )

    async def _on_response(self, response: Response) -> None:
        url = response.url
        status = response.status
        headers = {k.lower(): v for k, v in response.headers.items()}

        event = {
            "url": url,
            "status": status,
            "method": response.request.method,
            "content_type": headers.get("content-type", ""),
        }
        self._network_events.append(event)
        self._append_line(self._network_log_path, json.dumps(event, ensure_ascii=False))

        if status >= 500:
            self._add_finding(
                category="server_error_exposure",
                severity="HIGH",
                confidence="HIGH",
                route=url,
                title="HTTP 5xx Response Observed",
                evidence=f"{status} returned for {url}",
                reproduction_steps="Navigate to the route and observe network status.",
                remediation="Fix backend exception paths and return safe error responses.",
                network_log=json.dumps(event),
            )

        acao = headers.get("access-control-allow-origin", "")
        acac = headers.get("access-control-allow-credentials", "")
        if acao == "*" and acac.lower() == "true":
            self._add_finding(
                category="cors_misconfiguration",
                severity="HIGH",
                confidence="HIGH",
                route=url,
                title="Insecure CORS Policy",
                evidence=f"ACAO={acao}, ACAC={acac}",
                reproduction_steps="Inspect response headers for the endpoint.",
                remediation="Avoid wildcard origins with credentials; allowlist trusted origins.",
                network_log=json.dumps(event),
            )

        set_cookie = headers.get("set-cookie", "")
        if set_cookie:
            cookie_lower = set_cookie.lower()
            missing = []
            if "secure" not in cookie_lower:
                missing.append("Secure")
            if "httponly" not in cookie_lower:
                missing.append("HttpOnly")
            if "samesite" not in cookie_lower:
                missing.append("SameSite")
            if missing:
                self._add_finding(
                    category="insecure_cookie_flags",
                    severity="HIGH" if "Secure" in missing else "MEDIUM",
                    confidence="MEDIUM",
                    route=url,
                    title="Cookie Missing Security Attributes",
                    evidence=f"Missing attributes: {', '.join(missing)}",
                    reproduction_steps="Inspect Set-Cookie header in network response.",
                    remediation="Set Secure, HttpOnly, and SameSite on session/auth cookies.",
                    network_log=json.dumps(event),
                )

        if self._is_same_origin(url):
            await self._inspect_response_body(response, event)

    async def _inspect_response_body(self, response: Response, event: dict[str, Any]) -> None:
        content_type = (event.get("content_type") or "").lower()
        if not any(ct in content_type for ct in ("json", "javascript", "text", "html")):
            return

        try:
            body = await response.text()
        except Exception:
            return

        sample = body[:20000]
        for label, pat in self._SECRET_PATTERNS:
            m = pat.search(sample)
            if m:
                self._add_finding(
                    category="exposed_secret_indicator",
                    severity="HIGH",
                    confidence="MEDIUM",
                    route=response.url,
                    title=f"{label} Pattern Found in Response",
                    evidence=m.group(0)[:120],
                    reproduction_steps="Inspect the response body in network tab.",
                    remediation="Remove secrets from client-delivered assets and rotate exposed keys.",
                    network_log=json.dumps(event),
                )

        for name, pat in self._ERROR_PATTERNS:
            m = pat.search(sample)
            if m:
                self._add_finding(
                    category="verbose_error_response",
                    severity="HIGH" if "Stack" in name else "MEDIUM",
                    confidence="MEDIUM",
                    route=response.url,
                    title=f"{name} Indicator in Response Body",
                    evidence=m.group(0)[:160],
                    reproduction_steps="Open endpoint and inspect response payload.",
                    remediation="Return generic error responses in production.",
                    network_log=json.dumps(event),
                )

        if "sourcemappingurl=" in sample.lower() or response.url.endswith(".map"):
            self._add_finding(
                category="source_map_exposure",
                severity="LOW",
                confidence="MEDIUM",
                route=response.url,
                title="Source Map Exposure Indicator",
                evidence="sourceMappingURL reference observed in client asset response.",
                reproduction_steps="Open JS bundle and inspect for sourceMappingURL reference.",
                remediation="Disable source maps in production builds or protect access.",
                network_log=json.dumps(event),
            )

    async def scan_page(self, route_url: str, nav_response: Optional[Response] = None) -> None:
        await self._check_security_headers(route_url, nav_response)
        await self._check_mixed_content(route_url)
        await self._check_client_storage(route_url)
        await self._check_exposed_env(route_url)
        await self._check_dom_xss_indicators(route_url)
        await self._check_debug_and_admin_indicators(route_url)
        await self._check_access_flow_indicators(route_url)
        await self._check_csrf_presence(route_url)
        await self._check_config_listing_indicators(route_url)
        await self._check_file_upload_controls(route_url)
        # Additional non-destructive checks
        await self._check_iframe_embedding(route_url)
        await self._check_idor_indicators(route_url)

    async def run_safe_input_probes(self, route_url: str) -> None:
        # Limited probes with strict bounds.
        payloads = [
            "'\"<safe-xss-test>",
            "A" * 128,
            "invalid@@format",
        ]
        try:
            fields = await self.page.query_selector_all(
                "input:not([type='password']):not([type='file']), textarea"
            )
            fields = fields[: max(1, config.SECURITY_MAX_SAFE_PROBES_PER_PAGE)]
        except Exception:
            fields = []

        for idx, field in enumerate(fields):
            payload = payloads[idx % len(payloads)]
            try:
                await field.scroll_into_view_if_needed()
                await field.fill(payload, timeout=min(config.ACTION_TIMEOUT, 4000))
                await field.blur()
                await asyncio.sleep(0.2)
            except Exception:
                continue

        try:
            html_text = await self.page.content()
            if "<safe-xss-test>" in html_text:
                self._add_finding(
                    category="xss_reflection_indicator",
                    severity="MEDIUM",
                    confidence="LOW",
                    route=route_url,
                    title="Potential Reflected Input Rendering",
                    evidence="Test marker '<safe-xss-test>' present in rendered DOM after safe probe.",
                    reproduction_steps="Fill a visible input with harmless marker and observe rendered output.",
                    remediation="Escape user input before rendering; apply output encoding by context.",
                )
        except Exception:
            return

    def export_findings(self) -> list[dict[str, Any]]:
        return [asdict(f) for f in self.findings]

    def export_new_findings(self) -> list[dict[str, Any]]:
        if self._flushed_count >= len(self.findings):
            return []
        out = [asdict(f) for f in self.findings[self._flushed_count :]]
        self._flushed_count = len(self.findings)
        return out

    async def _check_security_headers(self, route_url: str, nav_response: Optional[Response]) -> None:
        if nav_response is None:
            return
        headers = {k.lower(): v for k, v in nav_response.headers.items()}
        for hdr, note in self._SECURITY_HEADERS.items():
            if hdr not in headers:
                severity = "HIGH" if hdr in {"content-security-policy", "x-frame-options"} else "MEDIUM"
                self._add_finding(
                    category="missing_security_header",
                    severity=severity,
                    confidence="HIGH",
                    route=route_url,
                    title=f"Missing Header: {hdr}",
                    evidence=f"Header '{hdr}' absent in navigation response.",
                    reproduction_steps="Check response headers on initial document request.",
                    remediation=note,
                )

    async def _check_mixed_content(self, route_url: str) -> None:
        if not route_url.startswith("https://"):
            return
        try:
            insecure = await self.page.evaluate(
                """
                () => {
                  const urls = [];
                  const all = [...document.querySelectorAll('script[src],img[src],link[href],iframe[src]')];
                  for (const el of all) {
                    const u = el.src || el.href || '';
                    if (u.startsWith('http://')) urls.push(u);
                  }
                  return urls.slice(0, 5);
                }
                """
            )
            if insecure:
                self._add_finding(
                    category="mixed_content",
                    severity="MEDIUM",
                    confidence="HIGH",
                    route=route_url,
                    title="HTTP Resource on HTTPS Page",
                    evidence=", ".join(insecure),
                    reproduction_steps="Open the page and inspect network/resource URLs.",
                    remediation="Serve all resources over HTTPS only.",
                )
        except Exception:
            return

    async def _check_client_storage(self, route_url: str) -> None:
        try:
            storage_data = await self.page.evaluate(
                """
                () => {
                  const out = [];
                  const scan = (store, type) => {
                    for (let i = 0; i < store.length; i++) {
                      const k = store.key(i) || '';
                      const v = store.getItem(k) || '';
                      out.push({type, key: k, value: v.slice(0, 150)});
                    }
                  };
                  scan(window.localStorage, 'localStorage');
                  scan(window.sessionStorage, 'sessionStorage');
                  return out;
                }
                """
            )
        except Exception:
            storage_data = []

        for item in storage_data:
            k = (item.get("key") or "").lower()
            v = (item.get("value") or "")
            if any(token in k for token in self._SENSITIVE_STORAGE_KEYS):
                self._add_finding(
                    category="insecure_client_storage",
                    severity="MEDIUM",
                    confidence="MEDIUM",
                    route=route_url,
                    title=f"Sensitive Key in {item.get('type')}",
                    evidence=f"{item.get('type')} key={item.get('key')}",
                    reproduction_steps="Inspect browser storage entries for auth/session values.",
                    remediation="Prefer HttpOnly cookies for auth/session secrets.",
                )
            if any(label in v.lower() for label in ("bearer ", "eyj", "apikey", "secret")):
                self._add_finding(
                    category="token_storage_exposure",
                    severity="HIGH",
                    confidence="LOW",
                    route=route_url,
                    title=f"Token-like Value in {item.get('type')}",
                    evidence=f"{item.get('key')}={v[:80]}",
                    reproduction_steps="Inspect browser storage values after page load.",
                    remediation="Avoid storing long-lived sensitive tokens in Web Storage.",
                )

    async def _check_exposed_env(self, route_url: str) -> None:
        try:
            env_keys = await self.page.evaluate(
                """
                () => {
                  const keys = Object.keys(window).filter(k =>
                    /^(env|config|settings|__env|__config|runtimeconfig|process)$/i.test(k)
                  );
                  return keys.slice(0, 10);
                }
                """
            )
            if env_keys:
                self._add_finding(
                    category="exposed_environment_indicator",
                    severity="LOW",
                    confidence="MEDIUM",
                    route=route_url,
                    title="Exposed Runtime Config Keys",
                    evidence=", ".join(env_keys),
                    reproduction_steps="Inspect global window object for env/config keys.",
                    remediation="Do not expose sensitive server-side env vars in client runtime.",
                )
        except Exception:
            return

    async def _check_dom_xss_indicators(self, route_url: str) -> None:
        try:
            has_sink = await self.page.evaluate(
                """
                () => {
                  const html = document.documentElement.outerHTML.toLowerCase();
                  const sinks = ['innerhtml', 'document.write', 'outerhtml'];
                  const src = ['location.hash', 'location.search', 'document.url'];
                  return sinks.some(s => html.includes(s)) && src.some(s => html.includes(s));
                }
                """
            )
            if has_sink:
                self._add_finding(
                    category="dom_xss_indicator",
                    severity="MEDIUM",
                    confidence="LOW",
                    route=route_url,
                    title="Possible DOM XSS Source/Sink Pattern",
                    evidence="Client HTML includes source/sink indicator strings.",
                    reproduction_steps="Review client scripts for unsafe assignment from URL-controlled sources.",
                    remediation="Avoid unsafe DOM sinks; sanitize untrusted values.",
                )
        except Exception:
            return

    async def _check_debug_and_admin_indicators(self, route_url: str) -> None:
        url_lower = route_url.lower()
        if any(k in url_lower for k in ("/admin", "/debug", "__debug", "swagger", "actuator")):
            self._add_finding(
                category="sensitive_route_indicator",
                severity="MEDIUM",
                confidence="HIGH",
                route=route_url,
                title="Potentially Sensitive Route Accessible",
                evidence=route_url,
                reproduction_steps="Navigate directly to the route.",
                remediation="Protect sensitive routes with strict auth/authorization checks.",
            )

        try:
            body = (await self.page.inner_text("body"))[:2000].lower()
        except Exception:
            body = ""

        if any(k in body for k in ("debug", "stack trace", "exception", "directory listing for", "index of /")):
            self._add_finding(
                category="debug_or_listing_indicator",
                severity="MEDIUM",
                confidence="MEDIUM",
                route=route_url,
                title="Debug/Directory Listing Indicator in Page Content",
                evidence=body[:220],
                reproduction_steps="Load the page and inspect visible content.",
                remediation="Disable debug modes and directory listing in production.",
            )

    async def _check_access_flow_indicators(self, route_url: str) -> None:
        parsed = urlparse(route_url)
        params = parse_qs(parsed.query)
        redirect_params = {k: v for k, v in params.items() if k.lower() in {"next", "redirect", "returnurl", "url"}}
        if redirect_params:
            self._add_finding(
                category="open_redirect_indicator",
                severity="LOW",
                confidence="LOW",
                route=route_url,
                title="Open Redirect Parameter Indicator",
                evidence=json.dumps(redirect_params)[:200],
                reproduction_steps="Inspect redirect-related query params and validation behaviour.",
                remediation="Allowlist redirect targets and reject external URLs.",
            )

        try:
            login_markers = await self.page.evaluate(
                """
                () => {
                  const hasLogout = !!document.querySelector('a[href*="logout"],button[id*="logout"],button[class*="logout"]');
                  const hasLogin = !!document.querySelector('input[type="password"],form[action*="login"],a[href*="login"]');
                  return {hasLogout, hasLogin};
                }
                """
            )
            if login_markers.get("hasLogin") and login_markers.get("hasLogout"):
                self._add_finding(
                    category="auth_flow_inconsistency",
                    severity="LOW",
                    confidence="LOW",
                    route=route_url,
                    title="Authentication UI State Inconsistency",
                    evidence="Page shows login and logout indicators simultaneously.",
                    reproduction_steps="Open page and verify auth state markers.",
                    remediation="Ensure consistent auth-session UI rendering.",
                )
        except Exception:
            return

    async def _check_csrf_presence(self, route_url: str) -> None:
        try:
            forms = await self.page.query_selector_all("form")
            for form in forms[:5]:
                has_token = await form.query_selector(
                    "input[name*='csrf' i],input[name*='token' i],meta[name='csrf-token']"
                )
                if has_token is None:
                    self._add_finding(
                        category="csrf_token_absence_indicator",
                        severity="MEDIUM",
                        confidence="LOW",
                        route=route_url,
                        title="CSRF Token Field Not Observed in Form",
                        evidence="Form detected without obvious CSRF token input/meta.",
                        reproduction_steps="Inspect form markup for anti-CSRF tokens.",
                        remediation="Include server-validated anti-CSRF tokens on state-changing forms.",
                    )
        except Exception:
            return

    async def _check_config_listing_indicators(self, route_url: str) -> None:
        lower = route_url.lower()
        if any(x in lower for x in ("/.env", "config", "settings", "application.properties", "web.config")):
            self._add_finding(
                category="public_config_path_indicator",
                severity="MEDIUM",
                confidence="LOW",
                route=route_url,
                title="Potential Public Config Path Reached",
                evidence=route_url,
                reproduction_steps="Navigate to route and verify if sensitive config is exposed.",
                remediation="Deny public access to server config and env files.",
            )

    async def _check_file_upload_controls(self, route_url: str) -> None:
        try:
            uploads = await self.page.evaluate(
                """
                () => {
                  const inputs = Array.from(document.querySelectorAll('input[type=\"file\"]'));
                  return inputs.map(el => ({
                    accept: el.getAttribute('accept') || '',
                    multiple: !!el.multiple,
                    name: el.getAttribute('name') || '',
                  }));
                }
                """
            )
        except Exception:
            uploads = []

        for up in uploads:
            accept = (up.get("accept") or "").lower()
            if not accept:
                self._add_finding(
                    category="insecure_file_upload_validation_indicator",
                    severity="MEDIUM",
                    confidence="MEDIUM",
                    route=route_url,
                    title="File Upload Input Missing Extension/MIME Restrictions",
                    evidence=f"file input name='{up.get('name', '')}' has no accept attribute",
                    reproduction_steps="Inspect file input controls and validation restrictions.",
                    remediation="Restrict upload types via allowlisted extensions/MIME server-side and client-side.",
                )
            if any(ext in accept for ext in (".php", ".exe", ".sh", ".bat", "application/x-msdownload")):
                self._add_finding(
                    category="unrestricted_file_extension_indicator",
                    severity="HIGH",
                    confidence="MEDIUM",
                    route=route_url,
                    title="Potentially Unsafe Executable Extension Allowed",
                    evidence=f"accept={accept}",
                    reproduction_steps="Review allowed upload extensions and MIME types.",
                    remediation="Block executable/script file uploads and enforce strict server-side validation.",
                )

    async def _check_iframe_embedding(self, route_url: str) -> None:
        """Detect potentially unsafe iframe embedding patterns (non-destructive).

        Flags iframes lack sandboxing or include permissive allow attributes.
        Low/Medium confidence; does not attempt any interaction with the iframe.
        """
        try:
            frames = await self.page.evaluate(
                """
                () => {
                  const out = [];
                  for (const el of document.querySelectorAll('iframe')) {
                    out.push({
                      src: el.getAttribute('src') || '',
                      sandbox: el.getAttribute('sandbox') || '',
                      allow: el.getAttribute('allow') || '',
                      scrolling: el.getAttribute('scrolling') || ''
                    });
                  }
                  return out.slice(0, 20);
                }
                """
            )
        except Exception:
            frames = []

        for f in frames:
            src = (f.get("src") or "").lower()
            sandbox = (f.get("sandbox") or "")
            allow = (f.get("allow") or "").lower()
            if not sandbox:
                self._add_finding(
                    category="unsafe_iframe_embedding",
                    severity="MEDIUM",
                    confidence="LOW",
                    route=route_url,
                    title="Iframe Missing sandbox Attribute",
                    evidence=f"iframe src={src} missing sandbox",
                    reproduction_steps="Inspect iframe elements and their attributes.",
                    remediation="Add a restrictive sandbox attribute to untrusted iframes.",
                )
            if any(tok in allow for tok in ("allow-same-origin", "allow-top-navigation")):
                self._add_finding(
                    category="unsafe_iframe_embedding",
                    severity="MEDIUM",
                    confidence="LOW",
                    route=route_url,
                    title="Iframe Allowlist Permissive Attribute",
                    evidence=f"allow='{allow}' on iframe {src}",
                    reproduction_steps="Inspect iframe allow attributes for privilege grants.",
                    remediation="Avoid allow-same-origin / allow-top-navigation unless absolutely needed.",
                )

    async def _check_idor_indicators(self, route_url: str) -> None:
        """Passive detection for possible IDOR-like URL patterns.

        Scans collected network events and page links for numeric ID segments
        and common param names. This is a heuristic indicator only (LOW/LOW-MEDIUM).
        """
        # ===== OPTIMIZATION START =====
        # Slice _network_events once — was sliced identically twice below.
        recent_events = self._network_events[-200:]
        # ===== OPTIMIZATION END =====

        # Analyze recent network URLs
        candidates = set()
        for ev in recent_events:
            u = ev.get("url") or ""
            if not u:
                continue
            # look for path segments that are purely numeric or short hex ids
            parts = urlparse(u).path.split('/')
            for p in parts:
                if re.fullmatch(r"\d{3,}", p) or re.fullmatch(r"[0-9a-fA-F]{6,}", p):
                    candidates.add(u)
        # Also scan anchors on page (same-origin) — non-invasive
        try:
            anchors = await self.page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]')).map(a=>a.href).slice(0,200)"
            )
        except Exception:
            anchors = []
        for a in anchors:
            if not a:
                continue
            parts = urlparse(a).path.split('/')
            for p in parts:
                if re.fullmatch(r"\d{3,}", p) or re.fullmatch(r"[0-9a-fA-F]{6,}", p):
                    candidates.add(a)

        # Param-based candidates
        suspect_params = ("id", "user", "uid", "account", "profile", "resource")
        for ev in recent_events:
            q = urlparse(ev.get("url", "")).query
            if not q:
                continue
            if any(re.search(rf"[?&]{p}=\d+", q) for p in suspect_params):
                candidates.add(ev.get("url"))

        for c in list(candidates)[:30]:
            self._add_finding(
                category="idor_indicator",
                severity="LOW",
                confidence="LOW",
                route=route_url,
                title="Potential IDOR Pattern Detected",
                evidence=f"URL shows numeric/id-like path or param: {c}",
                reproduction_steps="Review parameterised endpoints and verify access controls server-side.",
                remediation="Enforce object-level authorization and avoid predictable object identifiers.",
            )

    def _add_finding(
        self,
        category: str,
        severity: str,
        confidence: str,
        route: str,
        title: str,
        evidence: str,
        reproduction_steps: str,
        remediation: str,
        screenshot_path: str = "",
        network_log: str = "",
        console_log: str = "",
    ) -> None:
        key = (category, route, title)
        if key in self._seen:
            return
        self._seen.add(key)
        self.findings.append(
            SecurityFinding(
                category=category,
                severity=severity,
                confidence=confidence,
                route=route,
                title=title,
                evidence=evidence[:600],
                reproduction_steps=reproduction_steps,
                remediation=remediation,
                screenshot_path=screenshot_path,
                network_log=network_log[:600],
                console_log=console_log[:600],
            )
        )

    def _is_same_origin(self, url: str) -> bool:
        try:
            return urlparse(url).netloc == self.base_domain
        except Exception:
            return False

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
