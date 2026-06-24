import os
import json
import re
import time
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

mcp = FastMCP("SuccessFactors", host="0.0.0.0")

# In-memory cache for the Destination Service OAuth token so we don't
# fetch a new one on every SF API call. Refreshed 60s before expiry.
_token_cache: dict = {}


# ---------------------------------------------------------------------------
# BTP Destination Service helpers
# ---------------------------------------------------------------------------

def _dest_creds() -> dict:
    """
    Read Destination Service credentials from VCAP_SERVICES.
    CF injects this at runtime when the app is bound to the service.
    Contains: clientid, clientsecret, url (token endpoint), uri (API base).
    """
    vcap = json.loads(os.environ.get("VCAP_SERVICES", "{}"))
    instances = vcap.get("destination", [])
    if not instances:
        raise RuntimeError("Destination service not bound — check VCAP_SERVICES")
    return instances[0]["credentials"]


def _dest_token() -> str:
    """
    Fetch a short-lived OAuth token from the Destination Service using
    client_credentials grant. Cached in memory until 60s before expiry.
    """
    cached = _token_cache.get("dest")
    if cached and cached["exp"] > time.time() + 60:
        return cached["value"]

    creds = _dest_creds()
    resp = httpx.post(
        f"{creds['url']}/oauth/token",
        data={"grant_type": "client_credentials"},
        auth=(creds["clientid"], creds["clientsecret"]),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["dest"] = {
        "value": data["access_token"],
        "exp": time.time() + int(data.get("expires_in", 3600)),
    }
    return data["access_token"]


def _fetch_destination(name: str) -> dict:
    """
    Look up a named BTP Destination via the Destination Service REST API.
    Response includes the target URL and a pre-resolved auth header
    (Basic or Bearer) that can be forwarded directly to the target system.
    """
    creds = _dest_creds()
    resp = httpx.get(
        f"{creds['uri']}/destination-configuration/v1/destinations/{name}",
        headers={"Authorization": f"Bearer {_dest_token()}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _sf_get(entity: str, params: dict) -> dict:
    """
    GET request to SuccessFactors OData API.
    - On BTP/CF (VCAP_SERVICES present): resolves URL + auth via HRFF_Dev destination.
    - Locally: falls back to SF_BASE_URL / SF_USERNAME / SF_PASSWORD from .env.
    """
    params["$format"] = "json"

    if os.environ.get("VCAP_SERVICES"):
        dest = _fetch_destination("HRFF_Dev")
        base_url = dest["destinationConfiguration"]["URL"].rstrip("/")
        # authTokens[0] is the pre-resolved auth header — forward it as-is
        auth_header = dest["authTokens"][0]["http_header"]["value"]
        url = f"{base_url}/{entity}"
        response = httpx.get(url, params=params, headers={"Authorization": auth_header}, timeout=30)
    else:
        base_url = os.environ.get("SF_BASE_URL", "").rstrip("/")
        username = os.environ.get("SF_USERNAME", "")
        password = os.environ.get("SF_PASSWORD", "")
        if not base_url or not username or not password:
            raise ValueError("SF_BASE_URL, SF_USERNAME, and SF_PASSWORD must be set in environment")
        url = f"{base_url}/{entity}"
        response = httpx.get(url, params=params, auth=(username, password), timeout=30)

    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# XSUAA helpers (OAuth proxy)
# ---------------------------------------------------------------------------

def _xsuaa_creds() -> dict:
    """Read XSUAA credentials from VCAP_SERVICES (bound service sf_mcp-xsuaa)."""
    vcap = json.loads(os.environ.get("VCAP_SERVICES", "{}"))
    instances = vcap.get("xsuaa", [])
    if not instances:
        raise RuntimeError("XSUAA service not bound — check VCAP_SERVICES")
    for inst in instances:
        if inst.get("name") == "sf_mcp-xsuaa":
            return inst["credentials"]
    return instances[0]["credentials"]


# ---------------------------------------------------------------------------
# OAuth proxy routes — required for Claude.ai MCP custom connector
# These proxy the OAuth flow to the bound XSUAA service instance.
# Joule and other BTP callers are unaffected (they hit /sse directly).
# ---------------------------------------------------------------------------

@mcp.custom_route("/", methods=["GET"])
async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok", "service": "mcp-successfactors"})


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource(request: Request) -> Response:
    """RFC 9728 — tells clients which auth server protects this resource."""
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
    })


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_metadata(request: Request) -> Response:
    """RFC 8414 metadata — tells Claude.ai where XSUAA's OAuth endpoints are."""
    creds = _xsuaa_creds()
    url = creds["url"].rstrip("/")
    return JSONResponse({
        "issuer": url,
        "authorization_endpoint": f"{url}/oauth/authorize",
        "token_endpoint": f"{url}/oauth/token",
        "jwks_uri": f"{url}/token_keys",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
    })


@mcp.custom_route("/.well-known/openid-configuration", methods=["GET"])
async def openid_configuration(request: Request) -> Response:
    """OpenID Connect discovery — same metadata, alternate well-known path."""
    creds = _xsuaa_creds()
    url = creds["url"].rstrip("/")
    return JSONResponse({
        "issuer": url,
        "authorization_endpoint": f"{url}/oauth/authorize",
        "token_endpoint": f"{url}/oauth/token",
        "jwks_uri": f"{url}/token_keys",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    })


@mcp.custom_route("/authorize", methods=["GET"])
async def oauth_authorize(request: Request) -> Response:
    """Redirect the browser to XSUAA's authorization endpoint."""
    creds = _xsuaa_creds()
    xsuaa_url = f"{creds['url'].rstrip('/')}/oauth/authorize"
    return RedirectResponse(f"{xsuaa_url}?{request.url.query}")


@mcp.custom_route("/token", methods=["POST"])
async def oauth_token(request: Request) -> Response:
    """Proxy the authorization-code → token exchange to XSUAA."""
    creds = _xsuaa_creds()
    xsuaa_token_url = f"{creds['url'].rstrip('/')}/oauth/token"

    form_data = await request.form()
    # Forward Authorization header if present (Basic client_id:secret)
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    if auth := request.headers.get("Authorization"):
        headers["Authorization"] = auth

    async with httpx.AsyncClient() as client:
        resp = await client.post(xsuaa_token_url, data=dict(form_data), headers=headers, timeout=30)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


# ---------------------------------------------------------------------------
# Debug tool (temporary)
# ---------------------------------------------------------------------------


@mcp.tool()
def sf_debug() -> str:
    """Check environment and SF connection state (no credentials exposed)."""
    env_path = Path(__file__).parent / ".env"
    vcap = json.loads(os.environ.get("VCAP_SERVICES", "{}"))

    result: dict = {
        "mode": "CF/VCAP_SERVICES" if os.environ.get("VCAP_SERVICES") else "local/.env",
        "VCAP_SERVICES_set": bool(os.environ.get("VCAP_SERVICES")),
        "destination_service_bound": bool(vcap.get("destination")),
        "xsuaa_service_bound": bool(vcap.get("xsuaa")),
        "SF_BASE_URL_set": bool(os.environ.get("SF_BASE_URL")),
        "SF_USERNAME_set": bool(os.environ.get("SF_USERNAME")),
        "SF_PASSWORD_set": bool(os.environ.get("SF_PASSWORD")),
        "dotenv_exists": env_path.exists(),
    }

    if vcap.get("destination"):
        try:
            dest = _fetch_destination("HRFF_Dev")
            cfg = dest.get("destinationConfiguration", {})
            result["HRFF_Dev_url"] = cfg.get("URL", "NOT FOUND")
            result["HRFF_Dev_auth_type"] = cfg.get("Authentication", "NOT FOUND")
            result["HRFF_Dev_auth_token_available"] = bool(dest.get("authTokens"))
        except Exception as e:
            result["HRFF_Dev_error"] = str(e)

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# User tools
# ---------------------------------------------------------------------------


@mcp.tool()
def sf_get_user(
    user_id: str,
    select: Optional[str] = None,
    expand: Optional[str] = None,
) -> str:
    """Get a SuccessFactors User record by userId.

    Args:
        user_id: The userId of the user (e.g. "john.doe")
        select: Comma-separated fields to return (e.g. "userId,firstName,lastName,email,department")
        expand: Navigation properties to expand (e.g. "empInfo")
    """
    params: dict = {}
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    result = _sf_get(f"User('{user_id}')", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def sf_list_users(
    filter: Optional[str] = None,
    select: Optional[str] = None,
    expand: Optional[str] = None,
    orderby: Optional[str] = None,
    top: int = 20,
    skip: int = 0,
) -> str:
    """List / search SuccessFactors User records.

    Args:
        filter: OData filter expression (e.g. "department eq 'Engineering'" or "lastName eq 'Smith'")
        select: Comma-separated fields to return (e.g. "userId,firstName,lastName,email")
        expand: Navigation properties to expand
        orderby: Sort field with direction (e.g. "lastName asc")
        top: Maximum records to return — capped at 100 (default 20)
        skip: Records to skip for pagination (default 0)
    """
    params: dict = {"$top": min(top, 100), "$skip": skip}
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    if orderby:
        params["$orderby"] = orderby
    result = _sf_get("User", params)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# EmpEmployment tools
# ---------------------------------------------------------------------------


@mcp.tool()
def sf_get_emp_employment(
    user_id: str,
    select: Optional[str] = None,
    expand: Optional[str] = None,
) -> str:
    """Get EmpEmployment records for a specific user.

    Args:
        user_id: The userId to look up employment records for
        select: Comma-separated fields to return (e.g. "userId,startDate,endDate,employmentType")
        expand: Navigation properties to expand
    """
    params: dict = {"$filter": f"userId eq '{user_id}'"}
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    result = _sf_get("EmpEmployment", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def sf_list_emp_employment(
    filter: Optional[str] = None,
    select: Optional[str] = None,
    expand: Optional[str] = None,
    top: int = 20,
    skip: int = 0,
) -> str:
    """List SuccessFactors EmpEmployment records with optional OData filtering.

    Args:
        filter: OData filter expression (e.g. "userId eq 'john.doe'" or "employmentType eq 'Regular'")
        select: Comma-separated fields to return
        expand: Navigation properties to expand
        top: Maximum records — capped at 100 (default 20)
        skip: Records to skip for pagination (default 0)
    """
    params: dict = {"$top": min(top, 100), "$skip": skip}
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    result = _sf_get("EmpEmployment", params)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# EmpJob tools
# ---------------------------------------------------------------------------


@mcp.tool()
def sf_get_emp_job(
    user_id: str,
    select: Optional[str] = None,
    expand: Optional[str] = None,
) -> str:
    """Get EmpJob records for a specific user.

    Args:
        user_id: The userId to look up job records for
        select: Comma-separated fields to return (e.g. "userId,startDate,position,jobCode,department,costCenter")
        expand: Navigation properties to expand (e.g. "jobCodeNav,positionNav")
    """
    params: dict = {"$filter": f"userId eq '{user_id}'"}
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    result = _sf_get("EmpJob", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def sf_list_emp_job(
    filter: Optional[str] = None,
    select: Optional[str] = None,
    expand: Optional[str] = None,
    orderby: Optional[str] = None,
    top: int = 20,
    skip: int = 0,
) -> str:
    """List SuccessFactors EmpJob records with optional OData filtering.

    Args:
        filter: OData filter expression (e.g. "department eq 'Finance' and startDate gt datetime'2024-01-01T00:00:00'")
        select: Comma-separated fields to return
        expand: Navigation properties to expand
        orderby: Sort field with direction (e.g. "startDate desc")
        top: Maximum records — capped at 100 (default 20)
        skip: Records to skip for pagination (default 0)
    """
    params: dict = {"$top": min(top, 100), "$skip": skip}
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    if orderby:
        params["$orderby"] = orderby
    result = _sf_get("EmpJob", params)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Employee Time / Leave tools
# ---------------------------------------------------------------------------


@mcp.tool()
def sf_get_employee_time(
    user_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    select: Optional[str] = None,
    expand: Optional[str] = None,
) -> str:
    """Get leave/absence records for a specific employee.

    Args:
        user_id: The userId to look up leave records for
        start_date: Filter records on or after this date (format: YYYY-MM-DD)
        end_date: Filter records on or before this date (format: YYYY-MM-DD)
        select: Comma-separated fields to return (e.g. "userId,timeType,startDate,endDate,quantityInDays,approvalStatus")
        expand: Navigation properties to expand (e.g. "timeTypeNav")
    """
    filters = [f"userId eq '{user_id}'"]
    if start_date:
        filters.append(f"startDate ge datetime'{start_date}T00:00:00'")
    if end_date:
        filters.append(f"startDate le datetime'{end_date}T00:00:00'")
    params: dict = {"$filter": " and ".join(filters)}
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    result = _sf_get("EmployeeTime", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def sf_list_employee_time(
    filter: Optional[str] = None,
    select: Optional[str] = None,
    expand: Optional[str] = None,
    orderby: Optional[str] = None,
    top: int = 20,
    skip: int = 0,
) -> str:
    """List SuccessFactors EmployeeTime records with optional OData filtering.

    Args:
        filter: OData filter expression (e.g. "userId eq 'john.doe' and timeType eq 'VACATION'")
        select: Comma-separated fields to return
        expand: Navigation properties to expand
        orderby: Sort field with direction (e.g. "startDate desc")
        top: Maximum records — capped at 100 (default 20)
        skip: Records to skip for pagination (default 0)
    """
    params: dict = {"$top": min(top, 100), "$skip": skip}
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    if orderby:
        params["$orderby"] = orderby
    result = _sf_get("EmployeeTime", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def sf_get_time_account_balance(
    user_id: str,
    select: Optional[str] = None,
    expand: Optional[str] = None,
) -> str:
    """Get leave account balances for a specific employee (remaining vacation, sick days, etc.).

    Args:
        user_id: The userId to look up time account balances for
        select: Comma-separated fields to return (e.g. "userId,timeAccountType,balance,bookingEndDate")
        expand: Navigation properties to expand (e.g. "timeAccountTypeNav")
    """
    params: dict = {"$filter": f"userId eq '{user_id}'"}
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    result = _sf_get("TimeAccount", params)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Org-entity validation tool
# ---------------------------------------------------------------------------


@mcp.tool()
def sf_validate_org_entities(
    business_unit_name: str,
    department_name: str,
    location_name: str,
) -> str:
    """Validate that a Business Unit, Department, and Location all exist and are active
    in SAP SuccessFactors EC, using human-readable names as provided by the HRBP.

    Returns each entity's matching record(s) including externalCode for downstream use.
    If multiple matches are found for any entity, all are returned so the agent can ask
    the HRBP to disambiguate. Call this before any position creation or update work.

    Args:
        business_unit_name: Human-readable name of the Business Unit (e.g. "Corporate Finance")
        department_name: Human-readable name of the Department (e.g. "Accounts Payable")
        location_name: Human-readable name of the Location (e.g. "New York - 30 Rockefeller")
    """

    def _escape(value: str) -> str:
        # Escape single quotes in OData string literals
        return value.replace("'", "''")

    def _search_fo(entity: str, name: str, select: str, name_field: str = "name_defaultValue") -> dict:
        escaped = _escape(name)
        name_lower = name.lower()

        # Try OData server-side filter first (exact match, then substring)
        params: dict = {
            "$filter": f"status eq 'A' and {name_field} eq '{escaped}'",
            "$select": select,
            "$top": "10",
        }
        try:
            data = _sf_get(entity, params)
            records = data.get("d", {}).get("results", [])
            if not records:
                params["$filter"] = f"status eq 'A' and substringof('{escaped}', {name_field})"
                data = _sf_get(entity, params)
                records = data.get("d", {}).get("results", [])
            return {"found": bool(records), "match_count": len(records), "records": records}
        except httpx.HTTPStatusError as exc:
            sf_error = exc.response.text[:500] if exc.response.text else str(exc)
            if exc.response.status_code != 400:
                return {"found": False, "match_count": 0, "records": [], "error": sf_error}
            # 400 — fall through to client-side match, but capture the SF error detail
            sf_400_detail = sf_error
        except Exception as exc:
            return {"found": False, "match_count": 0, "records": [], "error": str(exc)}

        # Client-side fallback: fetch all active records and match by name in Python
        try:
            data = _sf_get(entity, {"$filter": "status eq 'A'", "$select": select, "$top": "500"})
            all_records = data.get("d", {}).get("results", [])
            exact = [r for r in all_records if r.get(name_field, "").lower() == name_lower]
            records = exact or [r for r in all_records if name_lower in r.get(name_field, "").lower()]
            return {"found": bool(records), "match_count": len(records), "records": records}
        except httpx.HTTPStatusError as exc:
            sf_error = exc.response.text[:500] if exc.response.text else str(exc)
            return {"found": False, "match_count": 0, "records": [], "error": f"filter_error: {sf_400_detail} | fallback_error: {sf_error}"}
        except Exception as exc:
            return {"found": False, "match_count": 0, "records": [], "error": f"filter_error: {sf_400_detail} | fallback_error: {str(exc)}"}

    def _resolve_status(result: dict) -> str:
        if "error" in result:
            return "error"
        if not result["found"]:
            return "not_found"
        if result["match_count"] > 1:
            return "multiple_matches"
        return "ok"
    
    def _top_level_code(result: dict) -> str | None:
        """Pull externalCode to top level when there's exactly one match."""
        if result["match_count"] == 1:
            return result["records"][0].get("externalCode")
        return None

    bu = _search_fo(
        "FOBusinessUnit",
        business_unit_name,
        "externalCode,name_defaultValue,status,startDate,endDate",
    )
    dept = _search_fo(
        "FODepartment",
        department_name,
        "externalCode,name_defaultValue,status,startDate,endDate,costCenter,cust_Division",
    )
    loc = _search_fo(
        "FOLocation",
        location_name,
        "externalCode,name,status,startDate,endDate",
         name_field="name"
    )

    output = {
    "all_valid": all(_resolve_status(r) == "ok" for r in [bu, dept, loc]),
    "business_unit": {
        "queried_name":  business_unit_name,
        "status":        _resolve_status(bu),
        "externalCode":  _top_level_code(bu),   # ← agent uses this directly
        **bu
    },
    "department": {
        "queried_name":  department_name,
        "status":        _resolve_status(dept),
        "externalCode":  _top_level_code(dept),
        "costCenter":    dept["records"][0].get("costCenter") if dept["match_count"] == 1 else None,
        "division":      dept["records"][0].get("cust_Division") if dept["match_count"] == 1 else None,
        **dept
    },
    "location": {
        "queried_name":  location_name,
        "status":        _resolve_status(loc),
        "externalCode":  _top_level_code(loc),
        **loc
    },
}
    return json.dumps(output, indent=2)

@mcp.tool()
def ec_scan_vacant_positions(
    dept_id: str,
    location: str,
) -> str:
    """
    Returns all active positions in a department and location, split into
    vacant and occupied lists using the position's own vacant flag.
    Always call this before creating any new positions — the HRBP must
    review vacants and decide whether to repurpose them first.

    Args:
        dept_id: externalCode of the department (from sf_validate_org_entities)
        location: Location externalCode OR human-readable location name.
                  Tried in order: exact code match → exact name match →
                  partial name match, all filtered server-side.
    """
    loc_escaped = location.replace("'", "''")
    base_filter = f"department eq '{dept_id}' and positionCriticality ne 'I'"
    select = (
        "code,externalName_defaultValue,jobCode,vacant,"
        "department,businessUnit,costCenter,location,parentPosition"
    )

    attempts = []
    page_size = 100

    def _query(location_filter: str) -> list | None:
        """Returns all matching records (paginated), None on 400, raises on other errors."""
        combined_filter = f"{base_filter} and {location_filter}"
        all_records: list = []
        skip = 0
        while True:
            params = {
                "$filter": combined_filter,
                "$select": select,
                "$top": str(page_size),
                "$skip": str(skip),
                "$expand": "parentPosition",
            }
            try:
                data = _sf_get("Position", params)
                raw = data.get("d", {}).get("results", [])
                all_records.extend(
                    [{k: v for k, v in p.items() if k != "__metadata"} for p in raw]
                )
                if len(raw) < page_size:
                    break
                skip += page_size
            except httpx.HTTPStatusError as exc:
                attempts.append({
                    "filter": params["$filter"],
                    "http_status": exc.response.status_code,
                    "sf_error": exc.response.text[:500],
                })
                if exc.response.status_code == 400:
                    return None
                raise
        return all_records

    # 1. Exact code match
    positions = _query(f"location eq '{loc_escaped}'")

    # 2. Exact name match via navigation property
    if positions is None or positions == []:
        positions = _query(f"locationNav/name eq '{loc_escaped}'")

    # 3. Partial name match
    if positions is None or positions == []:
        positions = _query(f"substringof('{loc_escaped}', locationNav/name)")

    if positions is None:
        return json.dumps({"error": "all_queries_failed", "attempts": attempts}, indent=2)

    vacant   = [p for p in positions if p.get("vacant") is True]
    occupied = [p for p in positions if p.get("vacant") is not True]

    # ------------------------------------------------------------------
    # Resolve parent position code from each vacant position.
    # $expand=parentPosition asks SF to return the full parent object
    # inline, but some instances still return a deferred link:
    #   {"__deferred": {"uri": "...Position('CODE')"}}
    # _extract handles all three shapes: expanded object, deferred link,
    # or a plain string code.
    # ------------------------------------------------------------------
    def _extract_parent_code(pos: dict) -> str | None:
        pp = pos.get("parentPosition")
        if not pp or not isinstance(pp, dict):
            return pp if isinstance(pp, str) and pp else None
        # Expanded inline — SF returned the full Position object
        if "code" in pp:
            return pp["code"]
        # Deferred link — parse the code out of the URI string
        if "__deferred" in pp:
            m = re.search(r"Position\('([^']+)'\)", pp["__deferred"].get("uri", ""))
            return m.group(1) if m else None
        return None

    parent_codes: list[str] = []
    for pos in vacant:
        code = _extract_parent_code(pos)
        if code:
            pos["parentPositionCode"] = code
            if code not in parent_codes:
                parent_codes.append(code)

    manager_map: dict = {}
    if parent_codes:
        or_parts = " or ".join(f"position eq '{c}'" for c in parent_codes[:10])
        try:
            emp_data = _sf_get("EmpJob", {
                "$filter": f"({or_parts}) and endDate eq datetime'9999-12-31T00:00:00'",
                "$select": "userId,position",
                "$top": "50",
            })
            for rec in emp_data.get("d", {}).get("results", []):
                pc = rec.get("position", "")
                manager_map.setdefault(pc, []).append(rec.get("userId"))
        except Exception:
            # Fallback: fetch without endDate filter and keep 9999 records in Python
            try:
                emp_data = _sf_get("EmpJob", {
                    "$filter": f"({or_parts})",
                    "$select": "userId,position,endDate",
                    "$top": "100",
                })
                for rec in emp_data.get("d", {}).get("results", []):
                    pc = rec.get("position", "")
                    end = str(rec.get("endDate", "") or "")
                    if "9999" in end or not end:
                        manager_map.setdefault(pc, []).append(rec.get("userId"))
            except Exception as exc:
                manager_map["_lookup_error"] = str(exc)

    for pos in vacant:
        parent_code = pos.get("parentPositionCode")
        if parent_code:
            pos["manager_userIds"] = manager_map.get(parent_code, [])

    return json.dumps({
        "total":          len(positions),
        "vacant":         vacant,
        "occupied_count": len(occupied),
        **({"debug_attempts": attempts} if attempts else {}),
    }, indent=2)


@mcp.tool()
def sf_update_position(
    position_code: str,
    effective_start_date: str,
    position_title: str = None,
    job_code: str = None,
) -> str:
    """Update the position title and/or job code of a position in SAP SuccessFactors.
    At least one of position_title or job_code must be provided.

    Args:
        position_code: externalCode of the position to update (e.g. '40000358')
        effective_start_date: Effective date for the new record, YYYY-MM-DD (e.g. '2026-06-16')
        position_title: New position title to set (e.g. 'Data Scientist'). Optional.
        job_code: Job code to assign to the position (e.g. 'JC_1001'). Optional.
    """
    if not position_title and not job_code:
        return json.dumps({"success": False, "message": "At least one of position_title or job_code must be provided."})

    if os.environ.get("VCAP_SERVICES"):
        dest = _fetch_destination("HRFF_Dev")
        base_url = dest["destinationConfiguration"]["URL"].rstrip("/")
        auth_header = dest["authTokens"][0]["http_header"]["value"]
    else:
        base_url = os.environ.get("SF_BASE_URL", "").rstrip("/")
        username = os.environ.get("SF_USERNAME", "")
        password = os.environ.get("SF_PASSWORD", "")
        if not base_url or not username or not password:
            raise ValueError("SF_BASE_URL, SF_USERNAME, and SF_PASSWORD must be set in environment")
        import base64
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        auth_header = f"Basic {credentials}"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": auth_header,
    }

    # Fetch CSRF token
    csrf_resp = httpx.get(
        f"{base_url}/Position",
        headers={**headers, "x-csrf-token": "fetch"},
        params={"$top": "1", "$format": "json"},
        timeout=15,
    )
    csrf_token = csrf_resp.headers.get("x-csrf-token", "")
    if not csrf_token:
        return json.dumps({"success": False, "message": "CSRF token not returned by SF"})

    headers["x-csrf-token"] = csrf_token

    # Convert YYYY-MM-DD to /Date(epoch_ms)/ format required by SF OData v2
    from datetime import datetime, timezone
    epoch_ms = int(datetime.strptime(effective_start_date, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc)
                   .timestamp() * 1000)

    # Build upsert payload with only the fields being updated
    payload = {
        "__metadata": {
            "uri": f"Position(code='{position_code}',effectiveStartDate=datetime'{effective_start_date}T00:00:00')",
            "type": "SFOData.Position"
        },
        "code": position_code,
        "effectiveStartDate": f"/Date({epoch_ms})/",
    }

    if position_title:
        payload["externalName_defaultValue"] = position_title
        payload["externalName_en_US"] = position_title

    if job_code:
        payload["jobCode"] = job_code

    upsert_resp = httpx.post(
        f"{base_url}/upsert",
        headers=headers,
        params={"purgeType": "incremental", "$format": "json"},
        json=payload,
        timeout=15,
    )

    try:
        return json.dumps(upsert_resp.json())
    except Exception:
        return json.dumps({
            "status_code": upsert_resp.status_code,
            "message": upsert_resp.text
        })


@mcp.tool()
def sf_create_position(
    effective_start_date: str,
    position_title: str,
    company: str,
    business_unit: str,
    division: str,
    department: str,
    cost_center: str,
    job_code: str,
    location: str,
    hiring_manager_user_id: str,
    standard_hours: float = 40.0,
    target_fte: float = 1.0,
) -> str:
    """Create a brand-new Position in SAP SuccessFactors EC.

    An 8-digit position code is auto-generated in the range 50000000–59999999
    (reserved for agent-created positions; avoids collision with manually
    created codes which are typically in the 4xxxxxxx range).
    The position is created as vacant=true, multipleIncumbentsAllowed=false.

    Args:
        effective_start_date:     Effective date for the position, YYYY-MM-DD (e.g. '2026-01-01').
        position_title:           Human-readable position title (e.g. 'Data Analyst').
        company:                  Company code (e.g. 'US01').
        business_unit:            Business Unit externalCode (e.g. '10001').
        division:                 Division externalCode (e.g. '20001').
        department:               Department externalCode (e.g. '30000054').
        cost_center:              Cost Center code (e.g. '3000987654').
        job_code:                 Job Code externalCode (e.g. 'Accountant').
        location:                 Location externalCode (e.g. 'US01').
        hiring_manager_user_id:   userId of the hiring manager (e.g. '6000002').
        standard_hours:           Weekly standard hours (default: 40).
        target_fte:               Target FTE headcount (default: 1).
    """
    import random

    # --- Auto-generate 8-digit position code (50000000–59999999) ---
    ts_part = int(time.time()) % 9000000
    rand_part = random.randint(0, 99)
    position_code = str(50000000 + (ts_part * 100 + rand_part) % 9999999)

    # --- Auth + CSRF (same pattern as sf_update_position) ---
    if os.environ.get("VCAP_SERVICES"):
        dest = _fetch_destination("HRFF_Dev")
        base_url = dest["destinationConfiguration"]["URL"].rstrip("/")
        auth_header = dest["authTokens"][0]["http_header"]["value"]
    else:
        base_url = os.environ.get("SF_BASE_URL", "").rstrip("/")
        username = os.environ.get("SF_USERNAME", "")
        password = os.environ.get("SF_PASSWORD", "")
        if not base_url or not username or not password:
            raise ValueError("SF_BASE_URL, SF_USERNAME, and SF_PASSWORD must be set in environment")
        import base64
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        auth_header = f"Basic {credentials}"

    csrf_resp = httpx.get(
        f"{base_url}/Position",
        headers={"Authorization": auth_header, "x-csrf-token": "fetch"},
        params={"$top": "1", "$format": "json"},
        timeout=15,
    )
    csrf_token = csrf_resp.headers.get("x-csrf-token", "")
    if not csrf_token:
        return json.dumps({"success": False, "position_code": position_code, "message": "CSRF token not returned by SF"})

    # --- Epoch date conversion ---
    from datetime import datetime, timezone
    epoch_ms = int(datetime.strptime(effective_start_date, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc)
                   .timestamp() * 1000)

    payload = {
        "__metadata": {
            "uri": f"Position(code='{position_code}',effectiveStartDate=datetime'{effective_start_date}T00:00:00')",
            "type": "SFOData.Position",
        },
        "code": position_code,
        "effectiveStartDate": f"/Date({epoch_ms})/",
        "externalName_defaultValue": position_title,
        "externalName_en_US": position_title,
        "company": company,
        "businessUnit": business_unit,
        "division": division,
        "department": department,
        "costCenter": cost_center,
        "jobCode": job_code,
        "location": location,
        "cust_hiringmanager": hiring_manager_user_id,
        "standardHours": standard_hours,
        "targetFTE": target_fte,
        "vacant": True,
        "multipleIncumbentsAllowed": False,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": auth_header,
        "x-csrf-token": csrf_token,
    }

    upsert_resp = httpx.post(
        f"{base_url}/upsert",
        headers=headers,
        params={"purgeType": "incremental", "$format": "json"},
        json=payload,
        timeout=15,
    )

    try:
        result = upsert_resp.json()
        if isinstance(result, dict):
            result["position_code"] = position_code
        return json.dumps(result)
    except Exception:
        return json.dumps({
            "status_code": upsert_resp.status_code,
            "position_code": position_code,
            "message": upsert_resp.text,
        })

if __name__ == "__main__":
    import uvicorn
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        port = int(os.environ.get("PORT", 8080))
        uvicorn.run(
            mcp.streamable_http_app(),
            host="0.0.0.0",
            port=port,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    else:
        mcp.run()


