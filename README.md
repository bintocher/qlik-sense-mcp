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

Qlik Sense MCP Server bridges Qlik Sense Enterprise with systems supporting Model Context Protocol. Server provides 21 tools for applications, data, users, and analytics operations.

### Key Features

- **Unified API**: Single interface for all Qlik Sense APIs
- **Security**: Certificate-based authentication support
- **Performance**: Optimized queries and response handling
- **Flexibility**: Multiple data export formats
- **Analytics**: Advanced data analysis tools

## Features

### Repository API (Fully Working)
| Command | Description | Status |
|---------|-------------|--------|
| `get_apps` | Retrieve list of applications | ✅ |
| `get_app_details` | Get detailed application information | ✅ |
| `get_app_metadata` | Get application metadata via REST API | ✅ |
| `get_users` | Retrieve list of users | ✅ |
| `get_streams` | Get list of streams | ✅ |
| `get_tasks` | Retrieve list of tasks | ✅ |
| `start_task` | Execute task | ✅ |
| `get_data_connections` | Get data connections | ✅ |
| `get_extensions` | Retrieve extensions | ✅ |
| `get_content_libraries` | Get content libraries | ✅ |

### Engine API (Fully Working)
| Command | Description | Status |
|---------|-------------|--------|
| `engine_get_doc_list` | List documents via Engine API | ✅ |
| `engine_open_app` | Open application via Engine API | ✅ |
| `engine_get_script` | Get load script from application | ✅ |
| `engine_get_fields` | Retrieve application fields | ✅ |
| `engine_get_sheets` | Get application sheets | ✅ |
| `engine_get_table_data` | Extract data from tables | ✅ |
| `engine_get_field_values` | Get field values with frequency | ✅ |
| `engine_get_field_statistics` | Get comprehensive field statistics | ✅ |
| `engine_get_data_model` | Get complete data model | ✅ |
| `engine_create_hypercube` | Create hypercube for analysis | ✅ |
| `engine_create_data_export` | Export data in multiple formats | ✅ |

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
- Network access to Qlik Sense server

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
QLIK_PROXY_PORT=4243
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
        "get_app_metadata",
        "get_users",
        "get_streams",
        "get_tasks",
        "get_data_connections",
        "get_extensions",
        "get_content_libraries",
        "engine_get_doc_list",
        "engine_open_app",
        "engine_get_script",
        "engine_get_fields",
        "engine_get_sheets",
        "engine_get_table_data",
        "engine_get_field_values",
        "engine_get_field_statistics",
        "engine_get_data_model",
        "engine_create_hypercube",
        "engine_create_data_export"
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
# Via MCP client
result = mcp_client.call_tool("get_apps")
print(f"Found {len(result)} applications")
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

#### Export Data
```python
# Export data in CSV format
result = mcp_client.call_tool("engine_create_data_export", {
    "app_id": "your-app-id",
    "table_name": "Sales",
    "format_type": "csv",
    "max_rows": 10000
})
```

## API Reference

### Repository API Functions

#### get_apps
Retrieves list of all Qlik Sense applications.

**Parameters:**
- `filter` (optional): Filter query for application search

**Returns:** Array of application objects with metadata

#### get_app_details
Gets detailed information about specific application.

**Parameters:**
- `app_id` (required): Application identifier

**Returns:** Application object with complete metadata

#### get_app_metadata
Retrieves comprehensive application metadata including data model.

**Parameters:**
- `app_id` (required): Application identifier

**Returns:** Object containing app overview, data model summary, sheets information

#### get_users
Retrieves list of Qlik Sense users.

**Parameters:**
- `filter` (optional): Filter query for user search

**Returns:** Array of user objects

#### get_streams
Gets list of application streams.

**Parameters:** None

**Returns:** Array of stream objects

#### get_tasks
Retrieves list of tasks (reload, external program).

**Parameters:**
- `task_type` (optional): Type filter ("reload", "external", "all")

**Returns:** Array of task objects with execution history

#### start_task
Executes specified task.

**Parameters:**
- `task_id` (required): Task identifier

**Returns:** Execution result object

#### get_data_connections
Gets list of data connections.

**Parameters:**
- `filter` (optional): Filter query for connection search

**Returns:** Array of data connection objects

#### get_extensions
Retrieves list of Qlik Sense extensions.

**Parameters:** None

**Returns:** Array of extension objects

#### get_content_libraries
Gets list of content libraries.

**Parameters:** None

**Returns:** Array of content library objects

### Engine API Functions

#### engine_get_doc_list
Lists available documents via Engine API.

**Parameters:** None

**Returns:** Array of document objects with metadata

#### engine_open_app
Opens application via Engine API for further operations.

**Parameters:**
- `app_id` (required): Application identifier

**Returns:** Application handle object for subsequent operations

#### engine_get_script
Retrieves load script from application.

**Parameters:**
- `app_id` (required): Application identifier

**Returns:** Object containing script text and metadata

#### engine_get_fields
Gets list of fields from application.

**Parameters:**
- `app_id` (required): Application identifier

**Returns:** Array of field objects with metadata and statistics

#### engine_get_sheets
Retrieves application sheets.

**Parameters:**
- `app_id` (required): Application identifier

**Returns:** Array of sheet objects with metadata

#### engine_get_table_data
Extracts data from application tables.

**Parameters:**
- `app_id` (required): Application identifier
- `table_name` (optional): Specific table name
- `max_rows` (optional): Maximum rows to return (default: 1000)

**Returns:** Table data with headers and row information

#### engine_get_field_values
Gets field values with frequency information.

**Parameters:**
- `app_id` (required): Application identifier
- `field_name` (required): Field name
- `max_values` (optional): Maximum values to return (default: 100)
- `include_frequency` (optional): Include frequency data (default: true)

**Returns:** Field values with frequency and metadata

#### engine_get_field_statistics
Retrieves comprehensive field statistics.

**Parameters:**
- `app_id` (required): Application identifier
- `field_name` (required): Field name

**Returns:** Statistical analysis including min, max, average, median, mode, standard deviation

#### engine_get_data_model
Gets complete data model with tables and associations.

**Parameters:**
- `app_id` (required): Application identifier

**Returns:** Data model structure with relationships

#### engine_create_hypercube
Creates hypercube for data analysis.

**Parameters:**
- `app_id` (required): Application identifier
- `dimensions` (required): Array of dimension fields
- `measures` (required): Array of measure expressions
- `max_rows` (optional): Maximum rows to return (default: 1000)

**Returns:** Hypercube data with dimensions and measures

#### engine_create_data_export
Exports data in various formats.

**Parameters:**
- `app_id` (required): Application identifier
- `table_name` (optional): Table name for export
- `fields` (optional): Specific fields to export
- `format_type` (optional): Export format ("json", "csv", "simple")
- `max_rows` (optional): Maximum rows to export (default: 10000)
- `filters` (optional): Field filters for data selection

**Returns:** Exported data in specified format

## Architecture

### Project Structure
```
qlik-sense-mcp/
├── qlik_sense_mcp_server/
│   ├── __init__.py
│   ├── server.py          # Main MCP server
│   ├── config.py          # Configuration management
│   ├── repository_api.py  # Repository API client
│   └── engine_api.py      # Engine API client (WebSocket)
├── certs/                 # Certificates (git ignored)
│   ├── client.pem
│   ├── client_key.pem
│   └── root.pem
├── .env.example          # Configuration template
├── .env                  # Your configuration
├── mcp.json.example      # MCP configuration template
├── pyproject.toml        # Project dependencies
└── README.md
```

### System Components

#### QlikSenseMCPServer
Main server class handling MCP protocol operations, tool registration, and request routing.

#### QlikRepositoryAPI
HTTP client for Repository API operations including applications, users, tasks, and metadata management.

#### QlikEngineAPI
WebSocket client for Engine API operations including data extraction, analytics, and hypercube creation.

#### QlikSenseConfig
Configuration management class handling environment variables, certificate paths, and connection settings.

## Development

### Development Environment Setup

The project includes a Makefile with common development tasks:

```bash
# Setup development environment
make dev

# Show all available commands
make help

# Build package
make build
```

### Version Management and Releases

Use Makefile commands for version management:

```bash
# Bump patch version and create PR
make version-patch

# Bump minor version and create PR
make version-minor

# Bump major version and create PR
make version-major
```

This will automatically:
1. Bump the version in `pyproject.toml`
2. Create a new branch
3. Commit changes
4. Push branch and create PR

### Publishing Process

1. **Merge PR** with version bump
2. **Create and push tag** to trigger automatic PyPI publication:
   ```bash
   git tag v1.0.1
   git push origin v1.0.1
   ```
3. **GitHub Actions** will automatically build and publish to PyPI

### Clean Git History

If you need to start with a clean git history:

```bash
make git-clean
```

**Warning**: This completely removes git history!

### Adding New Functions

1. **Add tool definition in server.py**
```python
# In handle_list_tools()
{"name": "new_tool", "description": "Tool description", "inputSchema": {...}}
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
    # Implementation code
    return result
```

### Code Standards

The project uses standard Python conventions. Build and test the package:

```bash
make build   # Build package
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

#### Test Repository API
```bash
python -c "
from qlik_sense_mcp_server.config import QlikSenseConfig
from qlik_sense_mcp_server.repository_api import QlikRepositoryAPI
config = QlikSenseConfig.from_env()
api = QlikRepositoryAPI(config)
print('Apps:', len(api.get_apps()))
"
```

#### Test Engine API
```bash
python -c "
from qlik_sense_mcp_server.config import QlikSenseConfig
from qlik_sense_mcp_server.engine_api import QlikEngineAPI
config = QlikSenseConfig.from_env()
api = QlikEngineAPI(config)
api.connect()
print('Docs:', len(api.get_doc_list()))
api.disconnect()
"
```

## Performance

### Optimization Recommendations

1. **Use filters** to limit data volume
2. **Cache results** for frequently used queries
3. **Limit result size** with `max_rows` parameter
4. **Use Repository API** for metadata (faster than Engine API)

### Benchmarks

| Operation | Average Time | Recommendations |
|-----------|--------------|-----------------|
| get_apps | 0.5s | Use filters |
| get_app_metadata | 2-5s | Cache results |
| engine_create_hypercube | 1-10s | Limit size |
| engine_create_data_export | 5-30s | Use pagination |

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
- Execute tasks (if needed)
- Access Engine API

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

**Project Status**: Production Ready | 21/21 Commands Working | v1.0.0

**Installation**: `uvx qlik-sense-mcp-server`
