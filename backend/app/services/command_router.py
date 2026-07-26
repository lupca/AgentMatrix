import re
from typing import Tuple, Optional

COMMANDS = {
    '/pm': 'create_task',
    '/dispatch': 'dispatch_task',
    '/verdict': 'verdict',
    '/status': 'get_status',
    '/help': 'show_help',
}

class CommandRouter:
    def __init__(self, db_session):
        self.db = db_session
    
    def parse(self, message: str) -> Tuple[Optional[str], str]:
        message = message.strip()
        if not message.startswith('/'):
            return None, message
        parts = message.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ''
        if cmd in COMMANDS:
            return COMMANDS[cmd], args
        return None, message
    
    async def execute(self, command: str, args: str, session_id: str) -> dict:
        handler = getattr(self, f'_handle_{command}', None)
        if not handler:
            return {'error': f'Unknown command: {command}'}
        return await handler(args, session_id)
    
    async def _handle_show_help(self, args: str, session_id: str) -> dict:
        return {'commands': list(COMMANDS.keys())}
