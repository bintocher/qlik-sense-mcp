# Qlik Sense MCP Server

Model Context Protocol (MCP) server for integration with Qlik Sense Enterprise APIs. Provides unified interface for Repository API and Engine API operations through MCP protocol.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [API Reference](#api-reference)
- [Architecture](#architecture)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## Overview

Qlik Sense MCP Server bridges Qlik Sense Enterprise with systems supporting Model Context Protocol. Server provides 6 essential tools for application metadata retrieval and data analysis operations.

### Key Features

- **Unified API**: Single interface for Qlik Sense Repository and Engine APIs
- **Security**: Certificate-based authentication support
- **Performance**: Optimized queries and direct API access
- **Analytics**: Advanced data analysis and hypercube creation
- **Metadata**: Comprehensive application and field information

## Features

### Available Tools

| Tool | Description | API | Status |
|------|-------------|-----|--------|
| `get_apps` | Get comprehensive list of applications with metadata | Repository | ✅ |
| `get_app_details` | Get detailed application analysis including data model | Engine | ✅ |
| `engine_get_script` | Extract load script from application | Engine | ✅ |
| `engine_get_field_values` | Get field values with frequency information | Engine | ✅ |
| `engine_get_field_statistics` | Get comprehensive field statistics | Engine | ✅ |
| `engine_create_hypercube` | Create hypercube for data analysis | Engine | ✅ |

## Installation

### Quick Start with uvx (Recommended)

The easiest way to use Qlik Sense MCP Server is with uvx:

```bash
uvx qlik-sense-mcp-server
```

This command will automatically install and run the latest version without affecting your system Python environment.

### Alternative Installation Methods

#### From PyPI
```bash
pip install qlik-sense-mcp-server
```

#### From Source (Development)
```bash
git clone https://github.com/bintocher/qlik-sense-mcp.git
cd qlik-sense-mcp
make dev
```

### System Requirements

- Python 3.12+
- Qlik Sense Enterprise
- Valid certificates for authentication
- Network access to Qlik Sense server (ports 4242, 4747)

### Setup

1. **Setup certificates**
```bash
mkdir certs
# Copy your Qlik Sense certificates to certs/ directory
```

2. **Create configuration**
```bash
cp .env.example .env
# Edit .env with your settings
```

## Configuration

### Environment Variables (.env)

```bash
# Server connection
QLIK_SERVER_URL=https://your-qlik-server.company.com
QLIK_USER_DIRECTORY=COMPANY
QLIK_USER_ID=your-username

# Certificate paths (absolute paths)
QLIK_CLIENT_CERT_PATH=/path/to/certs/client.pem
QLIK_CLIENT_KEY_PATH=/path/to/certs/client_key.pem
QLIK_CA_CERT_PATH=/path/to/certs/root.pem

# API ports (standard Qlik Sense ports)
QLIK_REPOSITORY_PORT=4242
QLIK_ENGINE_PORT=4747

# SSL settings
QLIK_VERIFY_SSL=false
```

### MCP Configuration

Create `mcp.json` file for MCP client integration:

```json
{
  "mcpServers": {
    "qlik-sense": {
      "command": "uvx",
      "args": ["qlik-sense-mcp-server"],
      "env": {
        "QLIK_SERVER_URL": "https://your-qlik-server.company.com",
        "QLIK_USER_DIRECTORY": "COMPANY",
        "QLIK_USER_ID": "your-username",
        "QLIK_CLIENT_CERT_PATH": "/path/to/certs/client.pem",
        "QLIK_CLIENT_KEY_PATH": "/path/to/certs/client_key.pem",
        "QLIK_CA_CERT_PATH": "/path/to/certs/root.pem",
        "QLIK_REPOSITORY_PORT": "4242",
        "QLIK_ENGINE_PORT": "4747",
        "QLIK_VERIFY_SSL": "false"
      },
      "disabled": false,
      "autoApprove": [
        "get_apps",
        "get_app_details",
        "engine_get_script",
        "engine_get_field_values",
        "engine_get_field_statistics",
        "engine_create_hypercube"
      ]
    }
  }
}
```

## Usage

### Start Server

```bash
# Using uvx (recommended)
uvx qlik-sense-mcp-server

# Using installed package
qlik-sense-mcp-server

# From source (development)
python -m qlik_sense_mcp_server.server
```

### Example Operations

#### Get Applications List
```python
# Via MCP client - get first 50 apps (default)
result = mcp_client.call_tool("get_apps")
print(f"Showing {result['pagination']['returned']} of {result['pagination']['total_found']} apps")

# Search for specific apps
result = mcp_client.call_tool("get_apps", {
    "name_filter": "Sales",
    "limit": 10
})

# Get more apps (pagination)
result = mcp_client.call_tool("get_apps", {
    "offset": 50,
    "limit": 50
})
```

#### Analyze Application
```python
# Get comprehensive app analysis
result = mcp_client.call_tool("get_app_details", {
    "app_id": "your-app-id"
})
print(f"App has {len(result['data_model']['tables'])} tables")
```

#### Create Data Analysis Hypercube
```python
# Create hypercube for sales analysis
result = mcp_client.call_tool("engine_create_hypercube", {
    "app_id": "your-app-id",
    "dimensions": ["Region", "Product"],
    "measures": ["Sum(Sales)", "Count(Orders)"],
    "max_rows": 1000
})
```

#### Get Field Statistics
```python
# Get detailed field statistics
result = mcp_client.call_tool("engine_get_field_statistics", {
    "app_id": "your-app-id",
    "field_name": "Sales"
})
print(f"Average: {result['avg_value']['numeric']}")
```

## API Reference

### get_apps
Retrieves comprehensive list of Qlik Sense applications with metadata, pagination and filtering support.

**Parameters:**
- `limit` (optional): Maximum number of apps to return (default: 50, max: 1000)
- `offset` (optional): Number of apps to skip for pagination (default: 0)
- `name_filter` (optional): Filter apps by name (case-insensitive partial match)
- `app_id_filter` (optional): Filter by specific app ID/GUID
- `include_unpublished` (optional): Include unpublished apps (default: true)

**Returns:** Object containing paginated apps, streams, and pagination metadata

**Example (default - first 50 apps):**
```json
{
  "apps": [...],
  "streams": [...],
  "pagination": {
    "limit": 50,
    "offset": 0,
    "returned": 50,
    "total_found": 1598,
    "has_more": true,
    "next_offset": 50
  },
  "filters": {
    "name_filter": null,
    "app_id_filter": null,
    "include_unpublished": true
  },
  "summary": {
    "total_apps": 1598,
    "published_apps": 857,
    "private_apps": 741,
    "total_streams": 40,
    "showing": "1-50 of 1598"
  }
}
```

**Example (with name filter):**
```python
# Search for apps containing "dashboard"
result = mcp_client.call_tool("get_apps", {
    "name_filter": "dashboard",
    "limit": 10
})

# Get specific app by ID
result = mcp_client.call_tool("get_apps", {
    "app_id_filter": "e2958865-2aed-4f8a-b3c7-20e6f21d275c"
})

# Get next page of results
result = mcp_client.call_tool("get_apps", {
    "limit": 50,
    "offset": 50
})
```

### get_app_details
Gets comprehensive application analysis including data model, object counts, and metadata.

**Parameters:**
- `app_id` (required): Application identifier

**Returns:** Detailed application object with data model structure

**Example:**
```json
{
  "app_metadata": {...},
  "data_model": {
    "tables": [...],
    "total_tables": 2,
    "total_fields": 45
  },
  "object_counts": {...}
}
```

### engine_get_script
Retrieves load script from application.

**Parameters:**
- `app_id` (required): Application identifier

**Returns:** Object containing script text and metadata

**Example:**
```json
{
  "qScript": "SET DateFormat='DD.MM.YYYY';\n...",
  "app_id": "app-id",
  "script_length": 2830
}
```

### engine_get_field_values
Gets field values with frequency information.

**Parameters:**
- `app_id` (required): Application identifier
- `field_name` (required): Field name
- `max_values` (optional): Maximum values to return (default: 100)
- `include_frequency` (optional): Include frequency data (default: true)

**Returns:** Field values with statistics and dimension information

**Example:**
```json
{
  "field_name": "state_name",
  "values": [
    {"value": "Найден, жив", "state": "O"},
    {"value": "Найден, погиб", "state": "O"}
  ],
  "total_values": 10
}
```

### engine_get_field_statistics
Retrieves comprehensive field statistics.

**Parameters:**
- `app_id` (required): Application identifier
- `field_name` (required): Field name

**Returns:** Statistical analysis including min, max, average, median, mode, standard deviation

**Example:**
```json
{
  "field_name": "age",
  "min_value": {"numeric": 0},
  "max_value": {"numeric": 2023},
  "avg_value": {"numeric": 40.98},
  "median_value": {"numeric": 38},
  "std_deviation": {"numeric": 24.88}
}
```

### engine_create_hypercube
Creates hypercube for data analysis.

**Parameters:**
- `app_id` (required): Application identifier
- `dimensions` (required): Array of dimension fields
- `measures` (required): Array of measure expressions
- `max_rows` (optional): Maximum rows to return (default: 1000)

**Returns:** Hypercube data with dimensions, measures, and total statistics

**Example:**
```json
{
  "hypercube_data": {
    "qDimensionInfo": [...],
    "qMeasureInfo": [...],
    "qDataPages": [...]
  },
  "total_rows": 30,
  "total_columns": 4
}
```

## Architecture

### Project Structure
```
qlik-sense-mcp/
├── qlik_sense_mcp_server/
│   ├── __init__.py
│   ├── server.py          # Main MCP server
│   ├── config.py          # Configuration management
│   ├── repository_api.py  # Repository API client (HTTP)
│   ├── engine_api.py      # Engine API client (WebSocket)
│   └── utils.py           # Utility functions
├── certs/                 # Certificates (git ignored)
│   ├── client.pem
│   ├── client_key.pem
│   └── root.pem
├── .env.example          # Configuration template
├── mcp.json.example      # MCP configuration template
├── pyproject.toml        # Project dependencies
└── README.md
```

### System Components

#### QlikSenseMCPServer
Main server class handling MCP protocol operations, tool registration, and request routing.

#### QlikRepositoryAPI
HTTP client for Repository API operations including application metadata and administrative functions.

#### QlikEngineAPI
WebSocket client for Engine API operations including data extraction, analytics, and hypercube creation.

#### QlikSenseConfig
Configuration management class handling environment variables, certificate paths, and connection settings.

## Development

### Development Environment Setup

```bash
# Setup development environment
make dev

# Show all available commands
make help

# Build package
make build
```

### Version Management

```bash
# Bump patch version and create PR
make version-patch

# Bump minor version and create PR
make version-minor

# Bump major version and create PR
make version-major
```

### Adding New Tools

1. **Add tool definition in server.py**
```python
# In tools_list
Tool(name="new_tool", description="Tool description", inputSchema={...})
```

2. **Add handler in server.py**
```python
# In handle_call_tool()
elif name == "new_tool":
    result = await asyncio.to_thread(self.api_client.new_method, arguments)
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
```

3. **Implement method in API client**
```python
# In repository_api.py or engine_api.py
def new_method(self, param: str) -> Dict[str, Any]:
    """Method implementation."""
    return result
```

## Troubleshooting

### Common Issues

#### Certificate Errors
```
SSL: CERTIFICATE_VERIFY_FAILED
```
**Solution:**
- Verify certificate paths in `.env`
- Check certificate expiration
- Set `QLIK_VERIFY_SSL=false` for testing

#### Connection Errors
```
ConnectionError: Failed to connect to Engine API
```
**Solution:**
- Verify port 4747 accessibility
- Check server URL correctness
- Verify firewall settings

#### Authentication Errors
```
401 Unauthorized
```
**Solution:**
- Verify `QLIK_USER_DIRECTORY` and `QLIK_USER_ID`
- Check user exists in Qlik Sense
- Verify user permissions

### Diagnostics

#### Test Configuration
```bash
python -c "
from qlik_sense_mcp_server.config import QlikSenseConfig
config = QlikSenseConfig.from_env()
print('Config valid:', config and hasattr(config, 'server_url'))
print('Server URL:', getattr(config, 'server_url', 'Not set'))
"
```

#### Test Repository API
```bash
python -c "
from qlik_sense_mcp_server.server import QlikSenseMCPServer
server = QlikSenseMCPServer()
print('Server initialized:', server.config_valid)
"
```

## Performance

### Optimization Recommendations

1. **Use filters** to limit data volume
2. **Limit result size** with `max_rows` parameter
3. **Use Repository API** for metadata (faster than Engine API)

### Benchmarks

| Operation | Average Time | Recommendations |
|-----------|--------------|-----------------|
| get_apps | 0.5s | Use filters |
| get_app_details | 2-5s | Analyze specific apps |
| engine_create_hypercube | 1-10s | Limit dimensions and measures |
| engine_get_field_statistics | 0.5-2s | Use for numeric fields |

## Security

### Recommendations

1. **Store certificates securely** - exclude from git
2. **Use environment variables** for sensitive data
3. **Limit user permissions** in Qlik Sense
4. **Update certificates regularly**
5. **Monitor API access**

### Access Control

Create user in QMC with minimal required permissions:
- Read applications
- Access Engine API
- View data (if needed for analysis)

## License

MIT License

Copyright (c) 2025 Stanislav Chernov

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

**Project Status**: Production Ready | 6/6 Tools Working | v1.1.1

**Installation**: `uvx qlik-sense-mcp-server`
