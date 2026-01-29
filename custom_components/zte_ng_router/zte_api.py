from __future__ import annotations

import asyncio
import logging
import json
import time
from typing import Any, Optional

import aiohttp
from aiohttp import ClientError, ClientSession, CookieJar
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)


class ZteRouterApi:
    """Async low-level API wrapper for ZTE 5G routers (e.g. G5TC)."""

    def __init__(
        self,
        hass: HomeAssistant | None = None,
        base_url: str = "",
        password: str = "",
        router_type: str = "",
        verify_tls: bool = True,
        session: ClientSession | None = None,
    ) -> None:
        # base_url like "http://192.168.254.1" or "https://192.168.254.1"
        self.hass = hass
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.router_type = router_type
        self.verify_tls = verify_tls

        # Use Home Assistant managed aiohttp session when hass is available.
        # If hass is not provided (e.g. due to an integration bug), fall back to a standalone session.
        if session is not None:
            self._session = session
        elif hass is not None:
            # verify_tls=False allows self-signed certificates.
            self._session = async_get_clientsession(hass, verify_ssl=verify_tls)
        else:
            _LOGGER.warning(
                "ZteRouterApi initialized without hass; falling back to a standalone aiohttp session"
            )
            connector = aiohttp.TCPConnector(ssl=verify_tls)
            jar = CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(connector=connector, cookie_jar=jar)

        self._session_id: Optional[str] = None
        self._logged_in: bool = False
        self._webtoken: Optional[str] = None

        self._auth_lock = asyncio.Lock()

        # Headers similar to the JS script environment
        self._base_headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Z-Mode": "1",
            "Origin": self.base_url,
            "Referer": self.base_url + "/index.html",
        }

    # --------------------------------------------------------------------
    # Helper: ubus URL
    # --------------------------------------------------------------------
    def _ubus_url(self) -> str:
        # WebUI uses a cache-busting timestamp query param
        return f"{self.base_url}/ubus/?t={int(time.time() * 1000)}"

    # --------------------------------------------------------------------
    # Helper: hashing
    # --------------------------------------------------------------------
    @staticmethod
    def sha256(text: str) -> str:
        import hashlib

        return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()

    @staticmethod
    def _is_conn_reset_104(exc: BaseException) -> bool:
        """Return True if exception chain contains errno 104 (Connection reset by peer)."""
        seen: set[int] = set()
        cur: BaseException | None = exc

        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))

            if isinstance(cur, ConnectionResetError) and getattr(cur, "errno", None) == 104:
                return True
            if isinstance(cur, OSError) and getattr(cur, "errno", None) == 104:
                return True

            cur = cur.__cause__ or cur.__context__

        return False

    def _update_webtoken_from_response(self, resp: aiohttp.ClientResponse) -> None:
        """Capture rotating 'webtoken' from response cookies (Set-Cookie)."""
        try:
            c = resp.cookies.get("webtoken")
            if c is not None and c.value:
                new_token = c.value.strip('"')
                if new_token and new_token != self._webtoken:
                    _LOGGER.debug("Updated webtoken from response")
                    self._webtoken = new_token
        except Exception:
            # Best-effort only
            pass

    def _apply_webtoken_cookie(self, headers: dict[str, str]) -> None:
        """Attach current webtoken cookie to request headers."""
        if self._webtoken:
            headers["Cookie"] = f'webtoken="{self._webtoken}"'

    # --------------------------------------------------------------------
    # Band helpers (simplified)
    # --------------------------------------------------------------------
    def _convert_lte_earfcn_to_band(self, earfcn: int | None) -> int | None:
        """Map LTE EARFCN to band number (subset of common bands)."""
        if earfcn is None:
            return None

        # [band, n_min, n_max]
        lte_bands = [
            (1, 0, 599),
            (3, 1200, 1949),
            (4, 1950, 2399),
            (5, 2400, 2649),
            (7, 2750, 3449),
            (8, 3450, 3799),
            (20, 6150, 6449),
            (28, 9210, 9659),
            (32, 9920, 10359),
            (38, 37750, 38249),
            (40, 38650, 39649),
            (42, 41590, 43589),
            (43, 43590, 45589),
        ]
        for band, nmin, nmax in lte_bands:
            if nmin <= earfcn <= nmax:
                return band

        return None

    def _convert_nr_arfcn_to_band(self, arfcn: int | None) -> int | None:
        """Map NR ARFCN to band number (subset of n1/n3/n28/n78...)."""
        if arfcn is None:
            return None

        # [band, n_min, n_max]
        nr_bands = [
            (1, 422000, 434000),
            (3, 361000, 376000),
            (5, 173800, 178800),
            (7, 524000, 538000),
            (8, 185000, 192000),
            (28, 151600, 160600),
            (40, 460000, 480000),
            (41, 499200, 537999),
            (75, 286400, 303400),
            (78, 620000, 653333),
            (79, 693334, 733333),
        ]

        for band, nmin, nmax in nr_bands:
            if nmin <= arfcn <= nmax:
                return band

        return None

    def _compute_bands_and_bw(self, netinfo: dict[str, Any]) -> tuple[str, float]:
        """Compute a simple band summary and total bandwidth in MHz."""
        bands: list[str] = []
        total_bw: float = 0.0

        # LTE primary
        try:
            lte_earfcn = int(netinfo.get("lte_action_channel"))
        except (TypeError, ValueError):
            lte_earfcn = None
        try:
            lte_bw = float(netinfo.get("lte_bandwidth"))
        except (TypeError, ValueError):
            lte_bw = None

        if lte_earfcn is not None and lte_bw is not None and lte_bw > 0:
            b = self._convert_lte_earfcn_to_band(lte_earfcn)
            if b is not None:
                bands.append(f"B{b}")
            total_bw += lte_bw

        # NR primary
        try:
            nr_arfcn = int(netinfo.get("nr5g_action_channel"))
        except (TypeError, ValueError):
            nr_arfcn = None
        try:
            nr_bw = float(netinfo.get("nr5g_bandwidth"))
        except (TypeError, ValueError):
            nr_bw = None

        if nr_arfcn is not None and nr_bw is not None and nr_bw > 0:
            b = self._convert_nr_arfcn_to_band(nr_arfcn)
            if b is not None:
                bands.append(f"n{b}")
            total_bw += nr_bw

        bands_summary = " + ".join(bands) if bands else "-"
        return bands_summary, total_bw

    # --------------------------------------------------------------------
    # Authentication
    # --------------------------------------------------------------------
    async def async_init_session(self) -> None:
        """Fetch the start page to get cookies/CSRF (if any)."""
        url = self.base_url + "/"
        try:
            async with self._session.get(url, timeout=10) as resp:
                await resp.read()
                self._update_webtoken_from_response(resp)
                _LOGGER.debug("async_init_session: status=%s", resp.status)
        except (ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.warning("async_init_session GET %s failed: %s", url, exc)

    async def async_login(self) -> None:
        """Perform web_login to ZTE router and store ubus session ID."""

        # 1) get salt
        salt_req = {
            "service": "zwrt_web",
            "method": "web_login_info",
            "params": {},
        }
        salt_res = await self.async_call_ubus(
            salt_req,
            session_id="0" * 32,
            retry_on_access_denied=False,
        )

        data = salt_res.get("data") or {}
        salt = data.get("zte_web_sault")
        if not salt:
            raise RuntimeError("Could not retrieve login salt")

        # 2) final hash = SHA256(SHA256(password) + salt)
        pw_hash = self.sha256(self.password)
        final = self.sha256(pw_hash + salt)

        # 3) login
        login_req = {
            "service": "zwrt_web",
            "method": "web_login",
            "params": {"password": final},
        }
        login_res = await self.async_call_ubus(
            login_req,
            session_id="0" * 32,
            retry_on_access_denied=False,
        )
        d = login_res.get("data") or {}
        sid = d.get("ubus_rpc_session")
        if not sid:
            raise RuntimeError(f"Login failed, response: {d}")

        self._session_id = sid
        self._logged_in = True
        _LOGGER.info("ZTE NG Router login successful")

    async def _async_ensure_logged_in(self, *, force: bool = False) -> None:
        """Ensure we have a valid ubus session.

        Uses a lock to prevent concurrent logins and avoids repeated logins within the same update cycle.
        """
        async with self._auth_lock:
            if not force and self._logged_in and self._session_id:
                return

            await self.async_init_session()
            await self.async_login()

    # --------------------------------------------------------------------
    # ubus caller with automatic re-login on -32002
    # --------------------------------------------------------------------
    async def async_call_ubus(
        self,
        call: dict,
        session_id: Optional[str] = None,
        *,
        retry_on_access_denied: bool = True,
        retry_on_connreset_104: bool = True,
    ) -> dict:
        """Call ubus. If access denied (-32002), try a re-login and retry once."""

        if session_id is None:
            # Lazily login only when we actually need an authenticated call.
            if not self._session_id or not self._logged_in:
                await self._async_ensure_logged_in()
            session_id = self._session_id

        req = [
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "call",
                "params": [
                    session_id,
                    call["service"],
                    call["method"],
                    call.get("params", {}) or {},
                ],
            }
        ]

        url = self._ubus_url()
        try:
            try:
                sid_preview = (session_id or "")[:8]
            except Exception:
                sid_preview = ""
            _LOGGER.debug(
                "ubus call: service=%s method=%s sid=%s",
                call.get("service"),
                call.get("method"),
                sid_preview,
            )
            headers = dict(self._base_headers)
            # Match WebUI behavior: Z-Mode=1 only for authenticated calls
            if self._logged_in and self._session_id and session_id != "0" * 32:
                headers["Z-Mode"] = "1"
            else:
                headers["Z-Mode"] = "0"

            # jQuery adds this header; some firmwares are picky for action calls
            headers.setdefault("X-Requested-With", "XMLHttpRequest")

            # Always apply webtoken cookie if available
            self._apply_webtoken_cookie(headers)

            # WebUI sets Z-Tag to the ubus method name (or UCI config name for uci.get)
            try:
                svc = str(call.get("service") or "")
                if svc == "uci":
                    ztag = str((call.get("params") or {}).get("config") or "")
                else:
                    ztag = str(call.get("method") or "")
                if ztag:
                    headers["Z-Tag"] = ztag
            except Exception:
                pass
            async with self._session.post(
                url,
                json=req,
                headers=headers,
                timeout=10,
            ) as resp:
                resp.raise_for_status()
                self._update_webtoken_from_response(resp)
                res_list = await resp.json(content_type=None)
        except Exception as exc:
            _LOGGER.warning("HTTP error while calling ubus: %s", exc)

            # Handle TCP reset-by-peer (errno 104) with a single retry
            if retry_on_connreset_104 and self._is_conn_reset_104(exc):
                _LOGGER.warning("Connection reset by peer (104), attempting re-login")

                self._logged_in = False
                self._session_id = None

                try:
                    await self._async_ensure_logged_in(force=True)
                except Exception as exc2:
                    _LOGGER.error("Re-login failed after 104: %s", exc2)
                    return {"success": False, "data": None}

                return await self.async_call_ubus(
                    call,
                    session_id=None,
                    retry_on_access_denied=retry_on_access_denied,
                    retry_on_connreset_104=False,
                )

            return {"success": False, "data": None}

        if not isinstance(res_list, list) or not res_list:
            _LOGGER.warning("Invalid JSON from ubus")
            return {"success": False, "data": None}

        res0 = res_list[0]

        _LOGGER.debug(
            "ubus response: service=%s method=%s has_error=%s",
            call.get("service"),
            call.get("method"),
            "error" in res0,
        )

        # Error case
        if "error" in res0:
            err = res0["error"]
            code = err.get("code")
            msg = err.get("message")

            # Access denied → re-login (expected on some firmwares when session expires)
            if code == -32002 and retry_on_access_denied:
                _LOGGER.debug(
                    "ubus access denied: service=%s method=%s code=%s msg=%s; attempting re-login",
                    call.get("service"),
                    call.get("method"),
                    code,
                    msg,
                )

                # Mark current session as invalid and perform a single forced re-login.
                self._logged_in = False
                self._session_id = None

                try:
                    await self._async_ensure_logged_in(force=True)
                except Exception as exc:
                    _LOGGER.error("Re-login failed: %s", exc)
                    return {"success": False, "data": None}

                return await self.async_call_ubus(
                    call,
                    session_id=None,
                    retry_on_access_denied=False,
                    retry_on_connreset_104=retry_on_connreset_104,
                )

            # Non-retryable error (or retry disabled)
            if code == -32002:
                # Avoid log spam: this can happen even after a re-login attempt on some firmwares.
                _LOGGER.debug(
                    "ubus access denied: service=%s method=%s code=%s msg=%s (no further retry)",
                    call.get("service"),
                    call.get("method"),
                    code,
                    msg,
                )
            else:
                _LOGGER.warning(
                    "ubus error: service=%s method=%s code=%s msg=%s",
                    call.get("service"),
                    call.get("method"),
                    code,
                    msg,
                )
            return {"success": False, "data": None}

        # Normal result
        result = res0.get("result") or []
        if isinstance(result, list) and result and result[0] == 0:
            # Some ubus methods (especially SET/actions) may return only [0] without a payload.
            data = result[1] if len(result) > 1 else None
            return {"success": True, "data": data}

        return {"success": False, "data": None}


    async def async_call_ubus_batch(
        self,
        calls: list[dict[str, Any]],
        *,
        retry_on_connreset_104: bool = True,
        retry_on_access_denied: bool = True,
    ) -> list[dict[str, Any]]:
        """Send multiple ubus calls in a single JSON-RPC batch request.

        Returns a list of per-call results in the same order as `calls`, each item:
          {"success": bool, "data": Any, "error": Optional[dict]}

        Note: all calls in a batch share the same ubus session id.
        """

        if not self._session_id or not self._logged_in:
            await self._async_ensure_logged_in()
        session_id = self._session_id

        # Build JSON-RPC batch
        req: list[dict[str, Any]] = []
        id_to_index: dict[int, int] = {}
        for idx, call in enumerate(calls):
            rpc_id = idx
            id_to_index[rpc_id] = idx
            req.append(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "method": "call",
                    "params": [
                        session_id,
                        call["service"],
                        call["method"],
                        call.get("params", {}) or {},
                    ],
                }
            )

        url = self._ubus_url()
        try:
            sid_preview = (session_id or "")[:8]
            _LOGGER.debug(
                "ubus batch call: n=%s sid=%s", len(req), sid_preview
            )
            headers = dict(self._base_headers)
            headers["Z-Mode"] = "1"
            headers.setdefault("X-Requested-With", "XMLHttpRequest")
            # Always apply webtoken cookie if available
            self._apply_webtoken_cookie(headers)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                try:
                    _LOGGER.debug("ubus batch request payload: %s", json.dumps(req))
                except Exception:
                    pass
            async with self._session.post(
                url,
                json=req,
                headers=headers,
                timeout=10,
            ) as resp:
                resp.raise_for_status()
                self._update_webtoken_from_response(resp)
                raw_text = await resp.text()
                _LOGGER.debug("ubus batch raw response: %s", raw_text)
                res_list = json.loads(raw_text)
                # If the session expired, the router may return -32002 for batch items.
                if retry_on_access_denied and isinstance(res_list, list):
                    any_denied = False
                    for it in res_list:
                        if isinstance(it, dict) and "error" in it:
                            err = it.get("error") or {}
                            if err.get("code") == -32002:
                                any_denied = True
                                break

                    if any_denied:
                        _LOGGER.debug("ubus batch access denied (-32002) detected; re-login and retry once")
                        self._logged_in = False
                        self._session_id = None
                        await self._async_ensure_logged_in(force=True)
                        return await self.async_call_ubus_batch(
                            calls,
                            retry_on_connreset_104=retry_on_connreset_104,
                            retry_on_access_denied=False,
                        )
        except Exception as exc:
            _LOGGER.warning("HTTP error while calling ubus batch: %s", exc)

            # Handle TCP reset-by-peer (errno 104) with a single retry
            if retry_on_connreset_104 and self._is_conn_reset_104(exc):
                _LOGGER.warning("Connection reset by peer (104) during batch, attempting re-login")
                self._logged_in = False
                self._session_id = None
                try:
                    await self._async_ensure_logged_in(force=True)
                except Exception as exc2:
                    _LOGGER.error("Re-login failed after 104 during batch: %s", exc2)
                    return [{"success": False, "data": None, "error": {"message": str(exc2)}} for _ in calls]
                return await self.async_call_ubus_batch(
                    calls,
                    retry_on_connreset_104=False,
                    retry_on_access_denied=retry_on_access_denied,
                )

            return [{"success": False, "data": None, "error": {"message": str(exc)}} for _ in calls]

        if not isinstance(res_list, list):
            _LOGGER.warning("Invalid JSON from ubus batch")
            return [{"success": False, "data": None, "error": {"message": "invalid_json"}} for _ in calls]

        # Prepare output list
        out: list[dict[str, Any]] = [{"success": False, "data": None, "error": None} for _ in calls]

        for item in res_list:
            if not isinstance(item, dict):
                continue
            rpc_id = item.get("id")
            if rpc_id not in id_to_index:
                continue
            idx = id_to_index[rpc_id]

            if "error" in item:
                out[idx] = {"success": False, "data": None, "error": item.get("error")}
                continue

            result = item.get("result") or []
            if isinstance(result, list) and result and result[0] == 0:
                # Some ubus methods return only [0] (no payload) on success.
                data = result[1] if len(result) > 1 else None
                out[idx] = {"success": True, "data": data, "error": None}
            else:
                out[idx] = {"success": False, "data": None, "error": {"message": "nonzero_result"}}

        return out
    
    def build_ubus_call(
        self,
        service: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a ubus call dict in the format expected by `async_call_ubus`."""
        return {
            "service": service,
            "method": method,
            "params": params or {},
        }


    async def async_execute_ubus_action(
        self,
        call: dict[str, Any],
        *,
        success_key: str | None = None,
        success_values: set[str] | None = None,
    ) -> bool:
        """Execute an action-style ubus call and return True if it succeeded.

        Intended for HA Buttons/Services (reboot, poweroff, factory reset, start update, etc.).

        If `success_key` is provided, we additionally check the returned payload
        (e.g. {"result": "success"}).
        """
        res = await self.async_call_ubus(call)
        if not res.get("success"):
            return False

        if success_key is None:
            return True

        data = res.get("data") or {}
        v = data.get(success_key)
        if v is None:
            return False

        if success_values is None:
            return bool(v)

        return str(v) in success_values


    async def async_execute_action_def(self, action: dict[str, Any]) -> bool:
        """Execute an action definition.

        Expected shape:
        {
            "service": "...",
            "method": "...",
            "params": {...},               # optional
            "success_key": "result",       # optional
            "success_values": ["success"]  # optional (list/tuple/set)
        }
        """
        service = action.get("service")
        method = action.get("method")
        if not service or not method:
            _LOGGER.warning("Invalid action definition (missing service/method): %s", action)
            return False

        params = action.get("params")
        if params is not None and not isinstance(params, dict):
            _LOGGER.warning("Invalid action definition (params must be dict): %s", action)
            return False

        call = self.build_ubus_call(service, method, params)

        success_key = action.get("success_key")

        success_values = action.get("success_values")
        if success_values is not None:
            if isinstance(success_values, (list, tuple, set)):
                success_values = {str(x) for x in success_values}
            else:
                _LOGGER.warning(
                    "Invalid action definition (success_values must be list/tuple/set): %s",
                    action,
                )
                success_values = None

        return await self.async_execute_ubus_action(
            call,
            success_key=success_key,
            success_values=success_values,
        )

    async def async_update_fast(self) -> dict[str, Any] | None:
        """Fetch only fast-changing WAN stats (rates + connected time).

        Keeps the payload small and avoids polling heavy endpoints at high frequency.
        Returns a partial data dict containing at least:
          - "wan": router_get_status payload
          - "wwandst": get_wwandst(type=4) payload (for real_time on firmwares that omit it in router_get_status)
        """

        # Ensure we have an authenticated session.
        if not self._session_id or not self._logged_in:
            await self._async_ensure_logged_in()

        batch_calls = [
            {"service": "zwrt_router.api", "method": "router_get_status"},
            {
                "service": "zwrt_data",
                "method": "get_wwandst",
                "params": {"source_module": "web", "cid": 1, "type": 4},
            },
        ]

        results = await self.async_call_ubus_batch(batch_calls)
        if not isinstance(results, list) or len(results) != 2:
            return None

        wan_res, wwandst_res = results
        wan = wan_res.get("data") or {}
        wwandst = wwandst_res.get("data") or {}

        return {
            "wan": wan,
            "wwandst": wwandst,
        }

    # --------------------------------------------------------------------
    # Public API used by the HA DataUpdateCoordinator
    # --------------------------------------------------------------------
    async def async_update_all(self) -> dict[str, Any]:
        """Fetch all relevant router data for Home Assistant in one go."""

        # One authenticated batch request (public endpoints also work with an authenticated SID)
        batch_calls = [
            {"service": "zte_nwinfo_api", "method": "nwinfo_get_netinfo"},
            {"service": "zwrt_wlan", "method": "report"},
            {"service": "zwrt_bsp.thermal", "method": "get_cpu_temp"},
            {"service": "zwrt_mc.device.manager", "method": "get_device_info"},
            {"service": "zwrt_router.api", "method": "router_get_status"},
            {"service": "uci", "method": "get", "params": {"config": "zwrt_common_info", "section": "common_config"}},
            {"service": "zwrt_led", "method": "get_ODU_switch_state", "params": {}},
            {"service": "uci", "method": "get", "params": {"config": "wireless", "section": "main_2g"}},
            {"service": "uci", "method": "get", "params": {"config": "wireless", "section": "main_5g"}},
            {
                "service": "zwrt_data",
                "method": "get_wwandst",
                "params": {"source_module": "web", "cid": 1, "type": 4},
            },
        ]

        results = await self.async_call_ubus_batch(batch_calls)
        netinfo_res, wlan_res, temp_res, dev_res, wan_res, uci_common_res, odu_led_res, uci_wifi_2g_res, uci_wifi_5g_res, wwandst_res = results

        wan = wan_res.get("data") or {}
        common_config = (uci_common_res.get("data") or {}).get("values") or {}
        odu_led = odu_led_res.get("data") or {}
        wifi_main_2g = (uci_wifi_2g_res.get("data") or {}).get("values") or {}
        wifi_main_5g = (uci_wifi_5g_res.get("data") or {}).get("values") or {}
        wwandst = wwandst_res.get("data") or {}

        netinfo = netinfo_res.get("data") or {}
        bands_summary, total_bw_mhz = self._compute_bands_and_bw(netinfo)

        wlan = wlan_res.get("data") or {}

        return {
            "netinfo": netinfo,
            "wlan": wlan,
            "wifi_main_2g": wifi_main_2g,
            "wifi_main_5g": wifi_main_5g,
            "odu_led": odu_led,
            "thermal": temp_res.get("data"),
            "device": dev_res.get("data"),
            "common_config": common_config,
            "wan": wan,
            "wwandst": wwandst,
            # derived fields
            "bands_summary": bands_summary,
            "total_bw_mhz": total_bw_mhz,
        }
