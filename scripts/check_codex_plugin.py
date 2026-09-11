"""Exercise the actual STDIO MCP launcher without inference or user project writes."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile


async def check(config):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    entry = json.loads(Path(config).read_text())['mcpServers']['software_factory']
    # Redirect just Factory's registry, never Codex credentials/configuration.
    with tempfile.TemporaryDirectory(prefix='factory-plugin-check-') as directory:
        args = list(entry['args'])
        if '--home' in args:
            args[args.index('--home') + 1] = directory
        else:
            args += ['--home', directory]
        async with Client(StdioServerParameters(command=entry['command'], args=args)) as client:
            result = await client.list_tools()
            names = {tool.name for tool in result.tools}
            required = {'factory_project', 'factory_message', 'factory_status', 'factory_pause', 'factory_resume'}
            if not required <= names:
                raise RuntimeError(f'Missing Factory tools: {sorted(required - names)}')
            result = await client.call_tool('factory_project', {'action': 'list'})
            payload = result.structured_content
            if not payload or not payload.get('ok') or payload['data']['total'] != 0:
                raise RuntimeError('Factory project listing failed in the isolated registry')
            print(f'MCP OK: {len(names)} tools; project listing OK; no model calls.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    args = parser.parse_args()
    asyncio.run(asyncio.wait_for(check(args.config), timeout=30))


if __name__ == '__main__':
    main()
