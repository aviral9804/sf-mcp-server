# MCP SuccessFactors Server

MCP server exposing read-only tools for SAP SuccessFactors OData APIs: **User**, **EmpEmployment**, and **EmpJob**.

## Setup

### 1. Install dependencies

```bash
cd C:\Users\avimukesh\Documents\mcp-successfactors
pip install -r requirements.txt
```

### 2. Configure credentials

Copy `.env.example` to `.env` and fill in your values:

```bash
copy .env.example .env
```

Edit `.env`:
```
SF_BASE_URL=https://api4.successfactors.com/odata/v2
SF_USERNAME=admin@YOUR_COMPANY_ID
SF_PASSWORD=your_password
```

> **Note:** The data center in the URL varies — common values are `api4`, `api8`, `api12`, `api15`. Check your SuccessFactors instance URL.

### 3. Add to Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "successfactors": {
      "command": "python",
      "args": ["C:\\Users\\avimukesh\\Documents\\mcp-successfactors\\server.py"],
      "env": {
        "SF_BASE_URL": "https://api4.successfactors.com/odata/v2",
        "SF_USERNAME": "admin@COMPANY_ID",
        "SF_PASSWORD": "your_password"
      }
    }
  }
}
```

### 4. Add to Claude Code (this CLI)

Run from this directory:
```bash
claude mcp add successfactors python server.py
```

Then set the env vars in your `.env` file or pass them via `claude mcp add --env`.

---

## Deploy to Cloud Foundry (BTP)

The server switches to SSE/HTTP transport automatically when `MCP_TRANSPORT=sse` is set — this is pre-configured in `manifest.yml`. CF injects the `PORT` env var and the server binds to it.

### 1. Target your CF space

```bash
cf api https://api.cf.<region>.hana.ondemand.com
cf login
cf target -o <your-org> -s <your-space>
```

### 2. Push the app

```bash
cf push
```

The app name will be `mcp-successfactors` (as defined in `manifest.yml`).

### 3. Set SuccessFactors credentials

Never commit real credentials to `manifest.yml`. Set them after push:

```bash
cf set-env mcp-successfactors SF_BASE_URL https://api4.successfactors.com/odata/v2
cf set-env mcp-successfactors SF_USERNAME admin@COMPANY_ID
cf set-env mcp-successfactors SF_PASSWORD your_password
cf restart mcp-successfactors
```

### 4. Verify

```bash
cf app mcp-successfactors   # check status is "running"
cf logs mcp-successfactors --recent
```

The SSE endpoint will be at:
```
https://mcp-successfactors.cfapps.<region>.hana.ondemand.com/sse
```

---

## BTP Destination & Joule Studio

Once the app is running on CF, register it as a BTP Destination so Joule Studio can connect.

### Create the destination in BTP Cockpit

1. Go to **BTP Cockpit → Connectivity → Destinations → New Destination**
2. Fill in:

| Field | Value |
|-------|-------|
| Name | `mcp-successfactors` |
| Type | `HTTP` |
| URL | `https://mcp-successfactors.cfapps.<region>.hana.ondemand.com` |
| Proxy Type | `Internet` |
| Authentication | `NoAuthentication` |

3. Add these **Additional Properties**:

| Key | Value |
|-----|-------|
| `HTML5.DynamicDestination` | `true` |
| `mcp.transport` | `sse` |
| `mcp.path` | `/sse` |

4. Save and use **Check Connection** to verify the CF app is reachable.

### Configure in Joule Studio

In Joule Studio, add the MCP server using the BTP Destination name (`mcp-successfactors`). The exact steps depend on your Joule Studio version — refer to the SAP Joule Studio documentation under **Tools / MCP Servers** for the current configuration UI.

> **Note:** SAP's Joule MCP integration is evolving. If the additional destination properties above don't match your Joule Studio version, check the SAP Help Portal for the latest required destination properties for MCP tool connections.

---

## Available Tools

| Tool | Description |
|------|-------------|
| `sf_get_user` | Get a single User by userId |
| `sf_list_users` | List/search Users with OData filter |
| `sf_get_emp_employment` | Get EmpEmployment records for a userId |
| `sf_list_emp_employment` | List EmpEmployment records with OData filter |
| `sf_get_emp_job` | Get EmpJob records for a userId |
| `sf_list_emp_job` | List EmpJob records with OData filter |
| `sf_get_employee_time` | Get leave/absence records for a userId (supports date range) |
| `sf_list_employee_time` | List EmployeeTime records with OData filter |
| `sf_get_time_account_balance` | Get leave account balances for a userId |

All tools support `select` (choose fields) and `expand` (include related entities). List tools support `filter`, `top`, `skip`, and `orderby`.

### Example prompts

- *"Get the user profile for john.doe"*
- *"List all users in the Finance department"*
- *"Show employment history for user jane.smith"*
- *"Find EmpJob records where department is Engineering, ordered by start date descending"*
