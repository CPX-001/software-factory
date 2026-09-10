"""Local project allowlist and durable explicit selection; no workflow policy."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3

from .workflow import Store, WorkflowError


class FactoryError(WorkflowError):
    def __init__(self, code, message, *, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


class Registry:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get('FACTORY_HOME', Path.home() / '.local/state/software-factory')).expanduser().resolve()
        self.path = self.home / 'registry.sqlite3'

    @contextmanager
    def connection(self):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('CREATE TABLE IF NOT EXISTS roots(path TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT UNIQUE NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS selections(scope TEXT PRIMARY KEY,
                          project_id TEXT NOT NULL REFERENCES projects(id))''')
            yield db

    def allow_root(self, path):
        """Trusted local setup only. This operation is deliberately absent from MCP."""
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise FactoryError('invalid_root', 'Allowed root must be a directory')
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO roots VALUES (?)', (str(path),))

    @staticmethod
    def validate_path(path):
        path = Path(path)
        if path.resolve() != path or not path.is_dir():
            raise FactoryError('project_unavailable', 'Registered project moved, disappeared or became a symlink')
        for target in (path / '.factory', path / '.factory/state.sqlite3'):
            if target.is_symlink() or target.resolve() != target:
                raise FactoryError('unsafe_project', 'Factory state must stay inside the registered project')
        return path

    def register(self, path, name=None, *, trusted=False, create=False):
        requested = Path(path).expanduser()
        if not requested.is_absolute():
            raise FactoryError('absolute_path_required', 'Choose an absolute project path once during initialization')
        path = requested.resolve()
        with self.connection() as db:
            roots = [Path(r[0]) for r in db.execute('SELECT path FROM roots')]
        if not trusted and not any(path.is_relative_to(root) for root in roots):
            raise FactoryError('project_outside_allowed_roots', 'Project initialization is outside locally authorized roots')
        if create:
            path.mkdir(parents=True, exist_ok=True)
        self.validate_path(path)
        Store(path).initialize()
        identifier = 'p_' + hashlib.sha256(str(path).encode()).hexdigest()[:16]
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO projects VALUES (?, ?, ?)', (identifier, (name or path.name)[:120], str(path)))
        return self.resolve(identifier)

    def projects(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM projects ORDER BY name,id')]

    def select(self, identifier, scope='default'):
        project = self.resolve(identifier, scope)
        with self.connection() as db:
            db.execute('INSERT INTO selections VALUES (?, ?) ON CONFLICT(scope) DO UPDATE SET project_id=excluded.project_id',
                       (scope, project['id']))
        return project

    def resolve(self, identifier=None, scope='default'):
        with self.connection() as db:
            if identifier:
                row = db.execute('SELECT * FROM projects WHERE id=?', (identifier,)).fetchone()
                if not row:
                    raise FactoryError('project_not_registered', 'Use a registered project ID, not a path or command')
            else:
                row = db.execute('''SELECT p.* FROM projects p JOIN selections s ON p.id=s.project_id
                                    WHERE s.scope=?''', (scope,)).fetchone()
                if not row:
                    rows = db.execute('SELECT * FROM projects LIMIT 2').fetchall()
                    if len(rows) != 1:
                        raise FactoryError('project_selection_required', 'Select a registered project explicitly or initialize one',
                                           details={'registered_count': db.execute('SELECT count(*) FROM projects').fetchone()[0]})
                    row = rows[0]
        self.validate_path(row['path'])
        return dict(row)
