import os
import json
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

mcp = FastMCP("SuccessFactors")


def _sf_get(entity: str, params: dict) -> dict:
    base_url = os.environ.get("SF_BASE_URL", "").rstrip("/")
    username = os.environ.get("SF_USERNAME", "")
    password = os.environ.get("SF_PASSWORD", "")
    if not base_url or not username or not password:
        raise ValueError(
            "SF_BASE_URL, SF_USERNAME, and SF_PASSWORD must be set in environment"
        )
    params["$format"] = "json"
    url = f"{base_url}/{entity}"
    response = httpx.get(url, params=params, auth=(username, password), timeout=30)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Debug tool (temporary)
# ---------------------------------------------------------------------------


@mcp.tool()
def sf_debug() -> str:
    """Check environment and .env file state (no credentials exposed)."""
    env_path = Path(__file__).parent / ".env"
    return json.dumps({
        "SF_BASE_URL_set": bool(os.environ.get("SF_BASE_URL")),
        "SF_USERNAME_set": bool(os.environ.get("SF_USERNAME")),
        "SF_PASSWORD_set": bool(os.environ.get("SF_PASSWORD")),
        "dotenv_path": str(env_path),
        "dotenv_exists": env_path.exists(),
        "SF_BASE_URL_value": os.environ.get("SF_BASE_URL", "NOT SET"),
    }, indent=2)


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


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        port = int(os.environ.get("PORT", 8080))
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        mcp.run()
