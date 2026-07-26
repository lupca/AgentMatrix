import re
from typing import Tuple, Optional
from app.db.models import Task, Session as SessionModel

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

    async def _handle_get_status(self, args: str, session_id: str) -> dict:
        if not self.db:
            return {'error': 'Database session not available'}
        
        target_id = args.strip() if args else None
        
        if target_id:
            task = self.db.query(Task).filter(Task.id == target_id).first()
            if not task:
                task = self.db.query(Task).filter(Task.id.ilike(f"%{target_id}%")).first()
            if task:
                return {
                    'status': 'success',
                    'task': {
                        'id': task.id,
                        'title': task.title,
                        'project': task.project,
                        'status': task.status,
                        'current_gate': task.current_gate,
                        'executor': task.executor,
                        'reviewer': task.reviewer,
                        'mode': task.mode,
                        'awaiting_approval': task.awaiting_approval,
                        'error': task.error,
                    }
                }
            return {'error': f"Task '{target_id}' not found"}
        
        if session_id:
            task = self.db.query(Task).filter(Task.session_id == session_id).first()
            if not task:
                db_session = self.db.query(SessionModel).filter(
                    (SessionModel.id == session_id) | (SessionModel.thread_id == session_id)
                ).first()
                if db_session and db_session.task_id:
                    task = self.db.query(Task).filter(Task.id == db_session.task_id).first()
            
            if task:
                return {
                    'status': 'success',
                    'task': {
                        'id': task.id,
                        'title': task.title,
                        'project': task.project,
                        'status': task.status,
                        'current_gate': task.current_gate,
                        'executor': task.executor,
                        'reviewer': task.reviewer,
                        'mode': task.mode,
                        'awaiting_approval': task.awaiting_approval,
                        'error': task.error,
                    }
                }
        
        tasks = self.db.query(Task).order_by(Task.updated_at.desc()).limit(10).all()
        return {
            'status': 'success',
            'tasks': [
                {
                    'id': t.id,
                    'title': t.title,
                    'project': t.project,
                    'status': t.status,
                    'current_gate': t.current_gate,
                    'executor': t.executor,
                    'reviewer': t.reviewer,
                }
                for t in tasks
            ]
        }
